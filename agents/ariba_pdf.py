"""Facts about a supplier's PDF for ``builtin:ariba``'s ``read_document``.

:func:`inspect` answers the page count and whether the file carries a digital
signature, with the signer's common name, the signing time the signature
dictionary states and whether its ``/ByteRange`` reaches the end of the file.
**Detected, never validated**: no certificate chain, no revocation, no check
that the hash in the PKCS#7 matches the bytes. A self-signed certificate with
any name passes; the tool's note tells the model so.

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

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

from agents.multipart_form import count_pdf_pages

MAX_BYTES = 10 * 1024 * 1024
MAX_SIGNATURES = 20
MAX_BYTERANGE_SCAN = 200
MAX_CONTENTS_HEX = 1024 * 1024
MAX_PAGE_NODES = 1_000
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
_HEX_SPACE = b" \t\r\n\f\x00"


def _nothing() -> dict[str, Any]:
    return {"pages": None, "digital_signature": False, "signatures": []}


def is_pdf(data: Any) -> bool:
    """``%PDF-`` within the first 1024 bytes (the type by content, never by
    the name or the declared media type)."""
    return isinstance(data, bytes) and data.find(b"%PDF-", 0, HEADER_WINDOW) >= 0


def inspect(data: Any) -> dict[str, Any]:
    """``{pages, digital_signature, signatures: [{signer, signed_at,
    covers_whole_file}]}`` of ``data``; never raises.

    ``pages`` is ``None`` when it cannot be told (or for input that is not
    bytes or is over ``MAX_BYTES``, which is not scanned at all)."""
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        return _nothing()
    facts = _nothing()
    try:
        facts["pages"] = _pages(data)
    except Exception:  # noqa: BLE001 -- a hostile file is "cannot tell", never an error
        facts["pages"] = None
    try:
        facts["signatures"] = _signatures(data)
    except Exception:  # noqa: BLE001
        facts["signatures"] = []
    facts["digital_signature"] = bool(facts["signatures"])
    return facts


# -- pages ---------------------------------------------------------------------

def _pages(data: bytes) -> int | None:
    """The page tree's ``/Count`` when its nodes are plain objects (the root
    holds the largest), else the invoke API's conservative scan.

    Why the tree first: signing appends a revision that rewrites the page
    holding the signature widget, so a scan for ``/Type /Page`` objects counts
    that page twice; the tree's count does not change."""
    best = 0
    for seen, match in enumerate(_PAGES_RE.finditer(data)):
        if seen >= MAX_PAGE_NODES:
            break
        start, end = _object_bounds(data, match.start(), match.end())
        for count in _COUNT_RE.finditer(data, start, end):
            best = max(best, int(count.group(1)))
    if best:
        return best
    return count_pdf_pages(data) or None


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
        if gap is not None and ranges in seen:
            continue  # the same dictionary again (a later revision repeats it)
        text = _dictionary(data, at, match.end(), gap)
        types = {m.group(1) for m in _TYPE_RE.finditer(text)}
        if b"DocTimeStamp" in types and b"Sig" not in types:
            continue  # a document timestamp: nobody signed
        if gap is not None:
            seen.add(ranges)
        found.append({
            "signer": _guarded(_signer, data, gap) if gap is not None else None,
            "signed_at": _guarded(_signed_at, text),
            "covers_whole_file": gap is not None and ranges[2] + ranges[3] == len(data),
        })
    return found


def _guarded(read: Any, *args: Any) -> Any:
    try:
        return read(*args)
    except Exception:  # noqa: BLE001 -- one broken signature is signer: None
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


def _signer(data: bytes, gap: tuple[int, int]) -> str | None:
    """The common name of the first certificate that is not a CA (else of the
    first) in the PKCS#7 of ``/Contents``."""
    hexed = data[gap[0] + 1:gap[1] - 1]
    if len(hexed) > MAX_CONTENTS_HEX:
        return None
    hexed = hexed.translate(None, _HEX_SPACE)
    if len(hexed) % 2:
        hexed += b"0"  # PDF: an odd count of hex digits ends in an implied 0
    der = _der(bytes.fromhex(hexed.decode("ascii")))
    if der is None:
        return None
    certificates = pkcs7.load_der_pkcs7_certificates(der)
    if not certificates:
        return None
    chosen = next((c for c in certificates if not _is_ca(c)), certificates[0])
    names = chosen.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    value = names[0].value if names else None
    return value[:MAX_SIGNER_CHARS] if isinstance(value, str) and value else None


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
    except Exception:  # noqa: BLE001 -- no or unreadable extension: not a CA
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
