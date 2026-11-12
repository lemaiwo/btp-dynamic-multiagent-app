"""Facts about a supplier's PDF for ``builtin:ariba``'s ``read_document``.

:func:`inspect` answers the page count and whether the file carries a digital
signature, with the signer's common name, the signing time the signature
dictionary states and whether its ``/ByteRange`` reaches the end of the file.
**Detected, never validated**: no certificate chain, no revocation, no check
that the hash in the PKCS#7 matches the bytes. A self-signed certificate with
any name passes; the tool's note tells the model so.

**What counts as a signature**: a dictionary of ``/Type /Sig`` (or of no
``/Type``) whose ``/ByteRange`` ``[0 a b c]`` leaves out exactly its own
``/Contents <hex>``; any other ``/ByteRange`` text is ignored. Such a
signature is ``readable`` when its ``/Contents`` parses as PKCS#7 with at
least one certificate, and ``digital_signature`` is true only when at least
one is readable; the others are counted in ``unreadable_signatures``. The
signer is the certificate the first SignerInfo names (issuer and serial
number); when that cannot be read (a subject key identifier, an odd encoding),
the first certificate that is not a CA, else the first.

**Pages**: ``pages`` is what the page tree says (or the scan, below);
``pages_upper_bound`` is the larger of the tree and a scan of the page
objects (one per object number, plus those in object streams), so a decoy
tree with a low ``/Count`` cannot pass a page cap; ``None`` when the scan
cannot tell.

The file is written by a supplier, so this reader is defensive by design:

- it never raises (every failure becomes ``None`` / ``[]``), and a failure
  inside one signature gives that signature ``signer: None``;
- the work is bounded: nothing over ``MAX_BYTES`` is scanned, at most
  ``MAX_SIGNATURES`` signature dictionaries are read (at most
  ``MAX_BYTERANGE_SCAN`` ``/ByteRange`` occurrences looked at), a
  ``/Contents`` over ``MAX_CONTENTS_HEX`` hex digits is not decoded, every
  dictionary is read within ``WINDOW`` bytes, and every pattern is anchored
  with bounded repeats (``bytes.find`` walks the file; no pattern that can
  backtrack runs over the whole of it).

CPU work on untrusted bytes: call it through ``asyncio.to_thread``. Standard
library plus ``cryptography`` (installed through ``pyjwt[crypto]``), and the
invoke API's page counter (:func:`agents.multipart_form.count_pdf_pages`) as
the fallback when the page tree is not visible in plain objects.
"""

from __future__ import annotations

import asyncio
import re
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

from agents.multipart_form import count_pdf_pages

# What a reader of somebody else's file may raise is "cannot tell", even a
# BaseException: cryptography's Rust core panics as pyo3's PanicException,
# which `except Exception` does not catch. These four still pass.
_PASS_THROUGH = (KeyboardInterrupt, SystemExit, asyncio.CancelledError, GeneratorExit)

MAX_BYTES = 10 * 1024 * 1024
MAX_SIGNATURES = 20
MAX_BYTERANGE_SCAN = 200
MAX_CONTENTS_HEX = 1024 * 1024
MAX_PAGE_NODES = 1_000
MAX_PAGE_OBJECTS = 10_000
MAX_SIGNER_CHARS = 256
WINDOW = 4096
# PDF 1.7, 7.5.2: the header may follow up to 1024 bytes of other data.
HEADER_WINDOW = 1024

_BYTERANGE_RE = re.compile(
    rb"\s{0,16}\[\s{0,16}(\d{1,12})\s{1,16}(\d{1,12})\s{1,16}(\d{1,12})\s{1,16}(\d{1,12})"
    rb"\s{0,16}\]")
_TYPE_RE = re.compile(rb"/Type\s{0,16}/([A-Za-z]{1,32})")
_M_RE = re.compile(
    rb"/M\s{0,16}\(D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    rb"(?:(Z)(?:00'?00'?)?|([+-])(\d{2})'?(?:(\d{2})'?)?)?\)")
_PAGES_RE = re.compile(rb"/Type\s{0,16}/Pages(?![A-Za-z0-9])")
_COUNT_RE = re.compile(rb"/Count\s{1,16}(\d{1,7})(?![0-9])")
# The pattern of agents.multipart_form.count_pdf_pages (plain objects only here).
_PAGE_RE = re.compile(rb"/Type\s*/Page(?![A-Za-z0-9])")
_OBJ_HEADER_RE = re.compile(rb"(\d{1,10})\s{1,8}\d{1,5}\s{1,8}\Z")
_HEX_SPACE = b" \t\r\n\f\x00"


def _nothing() -> dict[str, Any]:
    return {"pages": None, "pages_upper_bound": None, "digital_signature": False,
            "signatures": [], "unreadable_signatures": 0}


def is_pdf(data: Any) -> bool:
    """``%PDF-`` within the first 1024 bytes (the type by content, never by
    the name or the declared media type)."""
    return isinstance(data, bytes) and data.find(b"%PDF-", 0, HEADER_WINDOW) >= 0


def inspect(data: Any) -> dict[str, Any]:
    """``{pages, pages_upper_bound, digital_signature, signatures: [{signer,
    signed_at, covers_whole_file, readable}], unreadable_signatures}`` of
    ``data``; never raises.

    ``pages`` is ``None`` when it cannot be told (or for input that is not
    bytes or is over ``MAX_BYTES``, which is not scanned at all)."""
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        return _nothing()
    facts = _nothing()
    try:
        facts["pages"], facts["pages_upper_bound"] = _pages(data)
    except BaseException as exc:  # a hostile file is "cannot tell", never an error
        if isinstance(exc, _PASS_THROUGH):
            raise
        facts["pages"] = facts["pages_upper_bound"] = None
    try:
        signatures = _signatures(data)
    except BaseException as exc:
        if isinstance(exc, _PASS_THROUGH):
            raise
        signatures = []
    facts["signatures"] = signatures
    facts["digital_signature"] = any(s["readable"] for s in signatures)
    facts["unreadable_signatures"] = sum(1 for s in signatures if not s["readable"])
    return facts


# -- pages ---------------------------------------------------------------------

def _pages(data: bytes) -> tuple[int | None, int | None]:
    """``(pages, upper bound)``: the page tree's ``/Count`` when its nodes are
    plain objects (the root holds the largest), else the scan; the bound is
    the larger of the two, ``None`` when the scan cannot tell.

    Why the tree first and why the scan counts object numbers: signing
    appends a revision that rewrites the page holding the signature widget,
    so a count of ``/Type /Page`` texts counts that page twice; the tree's
    count does not change, and one object number is one live page."""
    tree = 0
    for seen, match in enumerate(_PAGES_RE.finditer(data)):
        if seen >= MAX_PAGE_NODES:
            break
        start, end = _object_bounds(data, match.start(), match.end())
        for count in _COUNT_RE.finditer(data, start, end):
            tree = max(tree, int(count.group(1)))
    scan = _scanned_pages(data)
    pages = tree or scan or None
    bound = max(tree, scan) if scan is not None else None
    return pages, bound or None


def _scanned_pages(data: bytes) -> int | None:
    """Page objects: plain ones by distinct object number, plus those the
    invoke API's scan finds in object streams; ``None`` when it cannot tell."""
    total = count_pdf_pages(data)
    if total == 0:
        return None
    plain = 0
    numbers: set[bytes | int] = set()
    for match in _PAGE_RE.finditer(data):
        plain += 1
        if plain > MAX_PAGE_OBJECTS:
            return total  # too many to tell apart: every one counts
        at = data.rfind(b"obj", max(0, match.start() - WINDOW), match.start())
        header = (_OBJ_HEADER_RE.search(data, max(0, at - 32), at)
                  if at >= 3 and data[at - 3:at] != b"end" else None)
        numbers.add(header.group(1) if header else match.start())
    return len(numbers) + max(0, total - plain)


def _object_bounds(data: bytes, lo: int, hi: int) -> tuple[int, int]:
    """From after the last ``obj`` before ``lo`` to the next ``endobj`` after
    ``hi``, each at most ``WINDOW`` bytes away."""
    start = data.rfind(b"obj", max(0, lo - WINDOW), lo)
    start = start + 3 if start >= 0 else max(0, lo - WINDOW)
    end = data.find(b"endobj", hi, hi + WINDOW)
    end = end if end >= 0 else min(len(data), hi + WINDOW)
    return start, end


# -- signatures ----------------------------------------------------------------

def _signatures(data: bytes) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[int, ...]] = set()
    pos = 0
    for _ in range(MAX_BYTERANGE_SCAN):
        if len(found) >= MAX_SIGNATURES:
            break
        at = data.find(b"/ByteRange", pos)
        if at < 0:
            break
        pos = at + len(b"/ByteRange")
        match = _BYTERANGE_RE.match(data, pos)
        if match is None:
            continue
        ranges = tuple(int(n) for n in match.groups())
        gap = _gap(data, ranges, at)
        if gap is None or ranges in seen:
            # Not a signature's own /Contents, or the same dictionary again
            # (a later revision repeats it).
            continue
        text = _dictionary(data, at, match.end(), gap)
        types = {m.group(1) for m in _TYPE_RE.finditer(text)}
        if types and b"Sig" not in types:
            continue  # a document timestamp, an annotation: nobody signed
        seen.add(ranges)
        readable, signer = _guarded(_signer, data, gap) or (False, None)
        found.append({
            "signer": signer,
            "signed_at": _guarded(_signed_at, text),
            "covers_whole_file": ranges[2] + ranges[3] == len(data),
            "readable": readable,
        })
    return found


def _guarded(read: Any, *args: Any) -> Any:
    try:
        return read(*args)
    except BaseException as exc:  # one broken signature is signer: None
        if isinstance(exc, _PASS_THROUGH):
            raise
        return None


def _gap(data: bytes, ranges: tuple[int, ...], at: int) -> tuple[int, int] | None:
    """Where ``/Contents <hex>`` is, as the ByteRange leaves it out: ``[0, a,
    b, c]`` with ``data[a] == "<"`` and ``data[b - 1] == ">"``, near the
    ``/ByteRange`` (a gap elsewhere is not this dictionary's), else ``None``."""
    first, length, second, rest = ranges
    if first != 0 or length <= 0 or second <= length or second + rest > len(data):
        return None
    if second - length > MAX_CONTENTS_HEX + WINDOW:
        return None
    if not (length - WINDOW <= at <= second + WINDOW):
        return None
    if data[length:length + 1] != b"<" or data[second - 1:second] != b">":
        return None
    return length, second


def _dictionary(data: bytes, at: int, after: int, gap: tuple[int, int] | None) -> bytes:
    """The text of the signature dictionary around ``/ByteRange``, without the
    ``/Contents`` value (which may be a megabyte of hex)."""
    lo, hi = (min(at, gap[0]), max(after, gap[1])) if gap else (at, after)
    start, end = _object_bounds(data, lo, hi)
    middle = data[lo:gap[0]] + data[gap[1]:hi] if gap else data[lo:hi]
    return data[start:lo] + middle[:2 * WINDOW] + data[hi:end]


def _signer(data: bytes, gap: tuple[int, int]) -> tuple[bool, str | None]:
    """``(readable, common name)`` of the PKCS#7 in ``/Contents``: readable
    when it parses with at least one certificate; the name is the subject CN
    of the certificate the SignerInfo names, else of the first that is not a
    CA, else of the first."""
    hexed = data[gap[0] + 1:gap[1] - 1]
    if len(hexed) > MAX_CONTENTS_HEX:
        return False, None
    hexed = hexed.translate(None, _HEX_SPACE)
    if len(hexed) % 2:
        hexed += b"0"  # PDF: an odd count of hex digits ends in an implied 0
    der = _der(bytes.fromhex(hexed.decode("ascii")))
    if der is None:
        return False, None
    with warnings.catch_warnings():
        # An unsorted certificate bag (common in real files) is read as BER
        # with a UserWarning per file: not ours to print.
        warnings.filterwarnings(
            "ignore", message="PKCS#7 certificates could not be parsed as DER",
            category=UserWarning)
        certificates = pkcs7.load_der_pkcs7_certificates(der)
    if not certificates:
        return False, None
    named = _guarded(_signer_id, der)
    chosen = next((c for c in certificates if named and _is_named(c, *named)), None)
    if chosen is None:
        chosen = next((c for c in certificates if not _is_ca(c)), certificates[0])
    names = chosen.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    value = names[0].value if names else None
    return True, value[:MAX_SIGNER_CHARS] if isinstance(value, str) and value else None


def _tlv(buf: bytes, pos: int, end: int) -> tuple[int, int, int]:
    """``(tag, value start, value end)`` of the DER element at ``pos``;
    ``ValueError`` when it does not fit (an indefinite length included)."""
    if pos + 2 > end:
        raise ValueError("short")
    first = buf[pos + 1]
    if first < 0x80:
        start, length = pos + 2, first
    else:
        size = first & 0x7F
        if not 1 <= size <= 4:
            raise ValueError("length")
        start = pos + 2 + size
        length = int.from_bytes(buf[pos + 2:start], "big")
    if start + length > end:
        raise ValueError("long")
    return buf[pos], start, start + length


def _children(buf: bytes, start: int, end: int) -> list[tuple[int, int, int, int]]:
    """``(tag, element start, value start, end)`` of each element inside."""
    out = []
    while start < end and len(out) < 64:
        tag, value, stop = _tlv(buf, start, end)
        out.append((tag, start, value, stop))
        start = stop
    return out


def _signer_id(der: bytes) -> tuple[bytes, int] | None:
    """Issuer (DER) and serial number of the first SignerInfo (RFC 5652:
    ContentInfo -> [0] SignedData -> signerInfos, its last element); ``None``
    for a subject key identifier."""
    _, start, end = _tlv(der, 0, len(der))
    tag, _, start, end = _children(der, start, end)[1]
    if tag != 0xA0:
        return None
    _, start, end = _tlv(der, start, end)
    tag, _, start, end = _children(der, start, end)[-1]
    if tag != 0x31:
        return None
    _, start, end = _tlv(der, start, end)
    tag, _, start, end = _children(der, start, end)[1]
    if tag != 0x30:
        return None
    issuer, serial = _children(der, start, end)[:2]
    return der[issuer[1]:issuer[3]], int.from_bytes(der[serial[2]:serial[3]], "big",
                                                    signed=True)


def _is_named(certificate: x509.Certificate, issuer: bytes, serial: int) -> bool:
    try:
        return (certificate.serial_number == serial
                and certificate.issuer.public_bytes() == issuer)
    except BaseException as exc:
        if isinstance(exc, _PASS_THROUGH):
            raise
        return False


def _der(raw: bytes) -> bytes | None:
    """The DER SEQUENCE at the start of ``raw``: ``/Contents`` is padded with
    zeros after it, which a DER reader refuses as trailing data."""
    if len(raw) < 2 or raw[0] != 0x30:
        return None
    if raw[1] < 0x80:
        total = 2 + raw[1]
    elif raw[1] == 0x80:
        return raw  # indefinite length (BER): let the reader decide
    else:
        size = raw[1] & 0x7F
        if size > 4 or len(raw) < 2 + size:
            return None
        total = 2 + size + int.from_bytes(raw[2:2 + size], "big")
    return raw[:total] if total <= len(raw) else None


def _is_ca(certificate: x509.Certificate) -> bool:
    try:
        return certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except BaseException as exc:  # no or unreadable extension: not a CA
        if isinstance(exc, _PASS_THROUGH):
            raise
        return False


def _signed_at(text: bytes) -> str | None:
    """``/M (D:YYYYMMDDHHmmSSOHH'mm')`` as ISO 8601; a missing part is its
    lowest value, no offset is a time without zone; anything else ``None``."""
    match = _M_RE.search(text)
    if match is None:
        return None
    year, month, day, hour, minute, second, zulu, sign, off_h, off_m = match.groups()
    zone = None
    if zulu:
        zone = timezone.utc
    elif sign:
        offset = timedelta(hours=int(off_h), minutes=int(off_m or b"0"))
        zone = timezone(-offset if sign == b"-" else offset)
    moment = datetime(int(year), int(month or b"1"), int(day or b"1"), int(hour or b"0"),
                      int(minute or b"0"), int(second or b"0"), tzinfo=zone)
    return moment.isoformat()
