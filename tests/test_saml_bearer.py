"""The locally signed SAML bearer assertion (``agents/saml_bearer.py``).

The assertion is written directly in canonical form. These tests check that
claim independently: the document is parsed and canonicalised with the
standard library (C14N 2.0 of a subtree that declares only the prefixes it
uses, which is what exclusive C14N 1.0 writes for it), and digest and signature are verified
against what that produced, with the public key of the certificate in the
document.
"""

from __future__ import annotations

import base64
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from agents import saml_bearer
from tests import local_stack_helpers as helpers

NS = {"saml2": "urn:oasis:names:tc:SAML:2.0:assertion",
      "ds": "http://www.w3.org/2000/09/xmldsig#"}
# So ET.tostring keeps the document's prefixes (they are part of what is signed).
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)


@pytest.fixture(scope="module")
def issuer():
    key = helpers.rsa_key()
    return saml_bearer.SamlIssuer.from_pem(
        "agent-local-saml-idp", helpers.key_pem(key), helpers.cert_pem(key)
    )


def build(issuer, **overrides):
    values = {"name_id": "dev@example.test", "audience": "S4H_001",
              "recipient": "https://sap.example.test:44300"}
    values.update(overrides)
    return saml_bearer.build_assertion(issuer, **values)


def verify(xml: str) -> None:
    """Digest and signature of ``xml``, re-derived with the standard library."""
    root = ET.fromstring(xml)
    signature = root.find("ds:Signature", NS)
    assert signature is not None
    digest_value = signature.findtext("ds:SignedInfo/ds:Reference/ds:DigestValue", namespaces=NS)
    reference = signature.find("ds:SignedInfo/ds:Reference", NS).get("URI")
    assert reference == "#" + root.get("ID")

    # Signed bytes: SignedInfo, canonicalised as the subtree it is.
    signed_info = ET.tostring(signature.find("ds:SignedInfo", NS), encoding="unicode")
    canonical_info = ET.canonicalize(signed_info)
    certificate = x509.load_der_x509_certificate(base64.b64decode(
        signature.findtext("ds:KeyInfo/ds:X509Data/ds:X509Certificate", namespaces=NS)
    ))
    certificate.public_key().verify(
        base64.b64decode(signature.findtext("ds:SignatureValue", namespaces=NS)),
        canonical_info.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )

    # Digested bytes: the assertion without its signature, canonicalised.
    root.remove(signature)
    canonical = ET.canonicalize(ET.tostring(root, encoding="unicode"))
    assert base64.b64encode(hashlib.sha256(canonical.encode("utf-8")).digest()).decode() == digest_value


def test_signature_and_digest_verify_independently(issuer):
    verify(build(issuer).xml)


@pytest.mark.parametrize(
    "name_id",
    ["o'brien+tag@example.test", 'a"b<c>&d@example.test', "ünïcødé@example.test", "DEVELOPER"],
)
def test_awkward_values_survive_canonical_form(issuer, name_id):
    assertion = build(issuer, name_id=name_id, audience='A&B "<x>"',
                      recipient='https://sap.example.test/?a=1&b="2"')
    verify(assertion.xml)
    root = ET.fromstring(assertion.xml)
    assert root.findtext("saml2:Subject/saml2:NameID", namespaces=NS) == name_id
    assert root.findtext(".//saml2:Audience", namespaces=NS) == 'A&B "<x>"'


def test_tampering_breaks_the_verification(issuer):
    xml = build(issuer).xml
    forged = xml.replace("dev@example.test", "admin@example.test")
    with pytest.raises(AssertionError):
        verify(forged)
    signed_info_forged = re.sub(r"<ds:DigestValue>[^<]+", "<ds:DigestValue>AAAA", xml)
    with pytest.raises(InvalidSignature):
        verify(signed_info_forged)


def test_layout_matches_what_the_abap_system_was_shown(issuer):
    now = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
    assertion = build(issuer, now=now, ttl_seconds=300)
    root = ET.fromstring(assertion.xml)
    children = [child.tag.split("}")[1] for child in root]
    assert children == ["Issuer", "Signature", "Subject", "Conditions", "AuthnStatement"]
    assert root.get("Version") == "2.0" and root.get("IssueInstant") == "2026-11-15T12:00:00Z"
    assert root.findtext("saml2:Issuer", namespaces=NS) == "agent-local-saml-idp"
    name_id = root.find("saml2:Subject/saml2:NameID", NS)
    assert name_id.get("Format") == saml_bearer.NAMEID_FORMATS["email"]
    confirmation = root.find("saml2:Subject/saml2:SubjectConfirmation", NS)
    assert confirmation.get("Method") == "urn:oasis:names:tc:SAML:2.0:cm:bearer"
    data = confirmation.find("saml2:SubjectConfirmationData", NS)
    assert data.get("Recipient") == "https://sap.example.test:44300"
    assert data.get("NotOnOrAfter") == "2026-11-15T12:05:00Z"
    conditions = root.find("saml2:Conditions", NS)
    assert conditions.get("NotBefore") == "2026-11-15T11:59:00Z"
    assert conditions.get("NotOnOrAfter") == "2026-11-15T12:05:00Z"
    assert root.findtext(".//saml2:AuthnContextClassRef", namespaces=NS) == (
        saml_bearer.AUTHN_CONTEXT_PREVIOUS_SESSION
    )
    assert assertion.authorization == "SAML2.0 " + base64.b64encode(
        assertion.xml.encode()
    ).decode()


def test_every_assertion_has_its_own_id(issuer):
    first, second = ET.fromstring(build(issuer).xml), ET.fromstring(build(issuer).xml)
    assert first.get("ID") != second.get("ID")


def test_unspecified_format_and_ttl_bounds(issuer):
    root = ET.fromstring(build(issuer, name_id="DEVELOPER", name_id_format="unspecified").xml)
    assert root.find("saml2:Subject/saml2:NameID", NS).get("Format") == (
        saml_bearer.NAMEID_FORMATS["unspecified"]
    )
    assert saml_bearer.clamp_ttl(1) == 30 and saml_bearer.clamp_ttl(99999) == 3600
    assert saml_bearer.clamp_ttl(None) == 300
    with pytest.raises(saml_bearer.SamlBearerError):
        saml_bearer.nameid_format("persistent")


@pytest.mark.parametrize("bad", ["", "   ", "a\nb", "a\x00b", "a\tb", "x" * 2000, "a\ud800b"])
def test_values_xml_cannot_carry_are_refused_without_being_quoted(issuer, bad):
    with pytest.raises(saml_bearer.SamlBearerError) as refused:
        build(issuer, name_id=bad)
    if bad.strip():
        assert bad not in str(refused.value)


def test_issuer_refuses_unusable_key_material():
    key = helpers.rsa_key()
    with pytest.raises(saml_bearer.SamlBearerError, match="key_file"):
        saml_bearer.SamlIssuer.from_pem("idp", b"not a key", helpers.cert_pem(key))
    with pytest.raises(saml_bearer.SamlBearerError, match="cert_file"):
        saml_bearer.SamlIssuer.from_pem("idp", helpers.key_pem(key), b"not a certificate")
    with pytest.raises(saml_bearer.SamlBearerError, match="does not belong"):
        saml_bearer.SamlIssuer.from_pem(
            "idp", helpers.key_pem(key), helpers.cert_pem(helpers.rsa_key())
        )
    with pytest.raises(saml_bearer.SamlBearerError, match="2048"):
        small = helpers.rsa_key(1024)
        saml_bearer.SamlIssuer.from_pem("idp", helpers.key_pem(small), helpers.cert_pem(small))


def test_metadata_names_issuer_and_certificate(issuer):
    root = ET.fromstring(saml_bearer.idp_metadata(issuer))
    assert root.get("entityID") == "agent-local-saml-idp"
    assert issuer.certificate_b64 in saml_bearer.idp_metadata(issuer)
    assert "PRIVATE" not in repr(issuer)
