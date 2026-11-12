"""PDF files for the ``builtin:ariba`` tests, all made here.

``unsigned_pdf`` draws pages with reportlab; ``signed_pdf`` appends, as an
incremental update the way signing tools do, a rewritten page object and a
``/Type /Sig`` dictionary whose ``/Contents`` is a detached PKCS#7 made with a
self-signed certificate generated per call. Nothing is read from disk.
"""

from __future__ import annotations

import datetime as dt
import io

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
from reportlab.pdfgen import canvas

PAGE_TEXT = "SYNTHETIC-OFFER-TEXT-7f3a"
SIGNER = "Example Signer Alpha"
CA_NAME = "Example Test CA"
PLACEHOLDER_HEX = 16_384


def unsigned_pdf(pages: int = 1, text: str = PAGE_TEXT) -> bytes:
    out = io.BytesIO()
    pdf = canvas.Canvas(out, pageCompression=0)
    for n in range(pages):
        pdf.drawString(72, 720, f"{text} page {n + 1}")
        pdf.showPage()
    pdf.save()
    return out.getvalue()


def _certificate(common_name: str, *, ca: bool, issuer=None, issuer_key=None):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject if issuer is not None else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(issuer_key or key, hashes.SHA256())
    )
    return cert, key


def pkcs7_detached(data: bytes, signer: str = SIGNER, *, with_ca: bool = True) -> bytes:
    """A detached PKCS#7 over ``data``; with ``with_ca`` the CA certificate is
    put FIRST in the bag, so the reader must skip it to find the signer."""
    ca_cert, ca_key = _certificate(CA_NAME, ca=True)
    cert, key = _certificate(signer, ca=False, issuer=ca_cert, issuer_key=ca_key)
    builder = pkcs7.PKCS7SignatureBuilder().set_data(data)
    if with_ca:
        builder = builder.add_certificate(ca_cert)
    builder = builder.add_signer(cert, key, hashes.SHA256())
    return builder.sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature])


def signed_pdf(base: bytes | None = None, *, signer: str = SIGNER,
               signed_at: bytes | None = b"D:20261112093000+01'00'",
               with_ca: bool = True, contents: bytes | None = None,
               rewrite_page: bool = True, sig_type: bytes = b"/Type /Sig ") -> bytes:
    """``base`` plus one signature revision. ``contents`` replaces the PKCS#7
    (raw bytes hex-encoded into the placeholder) for broken-signature tests."""
    doc = base if base is not None else unsigned_pdf()
    update = b"\n"
    if rewrite_page:
        # Signing rewrites the page that holds the signature widget: the
        # same object number again, in the new revision.
        update += (b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                   b"/Annots [98 0 R] >>\nendobj\n")
    stamp = b"/M (" + signed_at + b") " if signed_at is not None else b""
    update += (b"99 0 obj\n<< " + sig_type + b"/Filter /Adobe.PPKLite "
               b"/SubFilter /adbe.pkcs7.detached " + stamp
               + b"/ByteRange [0000000000 0000000000 0000000000 0000000000] "
               + b"/Contents <" + b"0" * PLACEHOLDER_HEX + b"> >>\nendobj\n"
               + b"trailer\n<< /Size 100 /Root 1 0 R >>\n%%EOF\n")
    doc = doc + update
    start = doc.rindex(b"/Contents <") + len(b"/Contents ")
    end = start + PLACEHOLDER_HEX + 2
    ranges = (0, start, end, len(doc) - end)
    filled = b"[" + b" ".join(b"%010d" % n for n in ranges) + b"]"
    doc = doc.replace(b"[0000000000 0000000000 0000000000 0000000000]", filled, 1)
    assert len(filled) == len(b"[0000000000 0000000000 0000000000 0000000000]")
    signature = contents if contents is not None else pkcs7_detached(
        doc[:start] + doc[end:], signer, with_ca=with_ca)
    hexed = signature.hex().encode().ljust(PLACEHOLDER_HEX, b"0")
    assert len(hexed) == PLACEHOLDER_HEX
    return doc[:start + 1] + hexed + doc[end - 1:]


def xlsx_like() -> bytes:
    return b"PK\x03\x04" + b"\x00" * 200 + b"[Content_Types].xml xl/workbook.xml"
