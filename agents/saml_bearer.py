"""A locally signed SAML 2.0 bearer assertion, for principal propagation to
an on-premise ABAP system without BTP.

On BTP a ``SAMLAssertion`` destination has the subaccount sign an assertion
for the signed-in user, and the ABAP system trusts the subaccount as a SAML
identity provider. A self-hosted deployment has no subaccount, so this app
plays that part: after the user's OIDC token was validated, the user's name
(the e-mail address by default) goes into a short-lived assertion signed
with this deployment's own key, and the request carries it as
``Authorization: SAML2.0 <base64>``. The ABAP system trusts the certificate
in transaction SAML2 under *Trusted Providers -> SAML Bearer Assertion
Providers* and maps the NameID to its user. The same mechanism, and the same
assertion layout, as ARC-1's ``proto/saml-bearer-pp`` branch, which was run
against an ABAP Platform 2025 system.

No XML library signs here (``signxml`` needs ``lxml``, which must stay out
of this app: see ``tests/test_sharepoint_requirements.py``). The assertion is
written directly in its exclusive-canonical form (attributes in canonical
order, no empty-element tags, canonical escaping), so the bytes that are
hashed and signed are the bytes a verifier gets by canonicalising the
document. ``tests/test_saml_bearer.py`` re-derives both with the standard
library's canonicaliser and verifies the signature with the certificate.
Values that canonical XML cannot carry unchanged (control characters) are
refused, never repaired.

Nothing here logs: the caller logs who an assertion was made for, never the
assertion.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

NAMEID_FORMATS = {
    "email": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
    "unspecified": "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
}
AUTHN_CONTEXT_PREVIOUS_SESSION = "urn:oasis:names:tc:SAML:2.0:ac:classes:PreviousSession"
DEFAULT_TTL_SECONDS = 300
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 3600
CLOCK_SKEW_SECONDS = 60
MAX_VALUE_CHARS = 1024

_NS_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
_NS_DS = "http://www.w3.org/2000/09/xmldsig#"
_EXC_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"
_ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
_RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
_SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"


class SamlBearerError(ValueError):
    """The issuer settings or a value cannot be used. The text names the
    setting and the rule, never a value."""


def nameid_format(value: str) -> str:
    """The NameID format URN for ``email`` / ``unspecified`` or one of the
    two URNs themselves."""
    text = (value or "email").strip()
    if text in NAMEID_FORMATS:
        return NAMEID_FORMATS[text]
    if text in NAMEID_FORMATS.values():
        return text
    raise SamlBearerError("nameIdFormat: 'email' or 'unspecified'")


def clamp_ttl(value: object) -> int:
    try:
        ttl = int(str(value).strip()) if value not in (None, "") else DEFAULT_TTL_SECONDS
    except ValueError:
        raise SamlBearerError("ttl_seconds: a whole number of seconds") from None
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, ttl))


def _checked(label: str, value: str) -> str:
    """``value`` as XML text may carry it unchanged: not empty, bounded, no
    control characters, no lone surrogates, nothing outside XML's range."""
    if not isinstance(value, str) or not value.strip():
        raise SamlBearerError(f"{label}: must not be empty")
    if len(value) > MAX_VALUE_CHARS:
        raise SamlBearerError(f"{label}: too long")
    for char in value:
        code = ord(char)
        if code < 0x20 or code == 0x7F or 0xD800 <= code <= 0xDFFF or code in (0xFFFE, 0xFFFF):
            raise SamlBearerError(f"{label}: holds a character XML cannot carry")
    return value


def _text(value: str) -> str:
    """Canonical escaping of a text node."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(value: str) -> str:
    """Canonical escaping of an attribute value."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def _instant(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SamlIssuer:
    """This deployment as a SAML identity provider: entity ID and key pair."""

    issuer: str
    private_key: rsa.RSAPrivateKey = field(repr=False)
    certificate_b64: str = field(repr=False)

    @classmethod
    def from_pem(cls, issuer: str, key_pem: bytes, cert_pem: bytes) -> "SamlIssuer":
        try:
            key = serialization.load_pem_private_key(key_pem, password=None)
        except Exception:  # noqa: BLE001 - the text may quote key material
            raise SamlBearerError("key_file: not an unencrypted PEM private key") from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise SamlBearerError("key_file: an RSA key is required (RSA-SHA256 signatures)")
        if key.key_size < 2048:
            raise SamlBearerError("key_file: the RSA key must have at least 2048 bits")
        try:
            cert = x509.load_pem_x509_certificate(cert_pem)
        except Exception:  # noqa: BLE001
            raise SamlBearerError("cert_file: not a PEM certificate") from None
        if cert.public_key().public_numbers() != key.public_key().public_numbers():  # type: ignore[union-attr]
            raise SamlBearerError("cert_file: the certificate does not belong to the key")
        der = cert.public_bytes(serialization.Encoding.DER)
        return cls(
            issuer=_checked("issuer", issuer),
            private_key=key,
            certificate_b64=base64.b64encode(der).decode("ascii"),
        )


@dataclass(frozen=True)
class SamlAssertion:
    xml: str = field(repr=False)
    not_on_or_after: datetime

    @property
    def authorization(self) -> str:
        """The ``Authorization`` header value an ABAP system reads it from."""
        return "SAML2.0 " + base64.b64encode(self.xml.encode("utf-8")).decode("ascii")

    @property
    def token(self) -> str:
        return base64.b64encode(self.xml.encode("utf-8")).decode("ascii")


def build_assertion(
    issuer: SamlIssuer,
    *,
    name_id: str,
    audience: str,
    recipient: str,
    name_id_format: str = "email",
    authn_context: str = AUTHN_CONTEXT_PREVIOUS_SESSION,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> SamlAssertion:
    """A signed bearer assertion for ``name_id``, valid ``ttl_seconds``."""
    moment = now or datetime.now(timezone.utc)
    ttl = clamp_ttl(ttl_seconds)
    assertion_id = "_" + secrets.token_hex(16)
    not_before = moment - timedelta(seconds=CLOCK_SKEW_SECONDS)
    not_on_or_after = moment + timedelta(seconds=ttl)
    fmt = nameid_format(name_id_format)

    head = (
        f'<saml2:Assertion xmlns:saml2="{_NS_SAML}" ID="{assertion_id}" '
        f'IssueInstant="{_instant(moment)}" Version="2.0">'
        f"<saml2:Issuer>{_text(_checked('issuer', issuer.issuer))}</saml2:Issuer>"
    )
    tail = (
        "<saml2:Subject>"
        f'<saml2:NameID Format="{fmt}">{_text(_checked("name id", name_id))}</saml2:NameID>'
        '<saml2:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml2:SubjectConfirmationData NotOnOrAfter="{_instant(not_on_or_after)}" '
        f'Recipient="{_attr(_checked("recipient", recipient))}"></saml2:SubjectConfirmationData>'
        "</saml2:SubjectConfirmation>"
        "</saml2:Subject>"
        f'<saml2:Conditions NotBefore="{_instant(not_before)}" '
        f'NotOnOrAfter="{_instant(not_on_or_after)}">'
        "<saml2:AudienceRestriction>"
        f"<saml2:Audience>{_text(_checked('audience', audience))}</saml2:Audience>"
        "</saml2:AudienceRestriction>"
        "</saml2:Conditions>"
        f'<saml2:AuthnStatement AuthnInstant="{_instant(moment)}" SessionIndex="{assertion_id}">'
        "<saml2:AuthnContext><saml2:AuthnContextClassRef>"
        f"{_text(_checked('authnContextClassRef', authn_context))}"
        "</saml2:AuthnContextClassRef></saml2:AuthnContext>"
        "</saml2:AuthnStatement>"
        "</saml2:Assertion>"
    )
    # Enveloped signature: the digest is over the assertion without it.
    digest = base64.b64encode(hashlib.sha256((head + tail).encode("utf-8")).digest()).decode()

    def signed_info(namespace: str) -> str:
        return (
            f"<ds:SignedInfo{namespace}>"
            f'<ds:CanonicalizationMethod Algorithm="{_EXC_C14N}"></ds:CanonicalizationMethod>'
            f'<ds:SignatureMethod Algorithm="{_RSA_SHA256}"></ds:SignatureMethod>'
            f'<ds:Reference URI="#{assertion_id}">'
            "<ds:Transforms>"
            f'<ds:Transform Algorithm="{_ENVELOPED}"></ds:Transform>'
            f'<ds:Transform Algorithm="{_EXC_C14N}"></ds:Transform>'
            "</ds:Transforms>"
            f'<ds:DigestMethod Algorithm="{_SHA256}"></ds:DigestMethod>'
            f"<ds:DigestValue>{digest}</ds:DigestValue>"
            "</ds:Reference>"
            "</ds:SignedInfo>"
        )

    # Canonicalised on its own, SignedInfo declares the prefix it uses; in
    # the document the declaration is on ds:Signature.
    signature = issuer.private_key.sign(
        signed_info(f' xmlns:ds="{_NS_DS}"').encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    block = (
        f'<ds:Signature xmlns:ds="{_NS_DS}">'
        f"{signed_info('')}"
        f"<ds:SignatureValue>{base64.b64encode(signature).decode()}</ds:SignatureValue>"
        "<ds:KeyInfo><ds:X509Data>"
        f"<ds:X509Certificate>{issuer.certificate_b64}</ds:X509Certificate>"
        "</ds:X509Data></ds:KeyInfo>"
        "</ds:Signature>"
    )
    # The SAML schema wants ds:Signature directly after Issuer.
    return SamlAssertion(xml=head + block + tail, not_on_or_after=not_on_or_after)


def idp_metadata(issuer: SamlIssuer) -> str:
    """The metadata file to upload in transaction SAML2 (entity ID and
    signing certificate; the sign-on endpoints are placeholders that are
    never called)."""
    location = "http://localhost/saml/sso"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f'xmlns:ds="{_NS_DS}" entityID="{_attr(issuer.issuer)}">\n'
        '  <md:IDPSSODescriptor WantAuthnRequestsSigned="false" '
        'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">\n'
        '    <md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data>'
        f"<ds:X509Certificate>{issuer.certificate_b64}</ds:X509Certificate>"
        "</ds:X509Data></ds:KeyInfo></md:KeyDescriptor>\n"
        f"    <md:NameIDFormat>{NAMEID_FORMATS['email']}</md:NameIDFormat>\n"
        f"    <md:NameIDFormat>{NAMEID_FORMATS['unspecified']}</md:NameIDFormat>\n"
        '    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
        f'Location="{location}"/>\n'
        '    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location="{location}"/>\n'
        "  </md:IDPSSODescriptor>\n"
        "</md:EntityDescriptor>\n"
    )
