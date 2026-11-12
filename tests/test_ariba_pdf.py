"""``agents.ariba_pdf``: page count and signature facts of a supplier's PDF.

Every PDF is made in the test (``tests/ariba_pdf_helpers.py``). The reader
parses a file somebody else wrote: it must never raise, and its work is
bounded whatever the bytes say.
"""

from __future__ import annotations

import time

import pytest

from agents import ariba_pdf
from agents.multipart_form import count_pdf_pages
from tests.ariba_pdf_helpers import (
    CA_NAME,
    SIGNER,
    pkcs7_detached,
    signed_pdf,
    unsigned_pdf,
    xlsx_like,
)

# The hand-ordered certificate bag of the helper is not a sorted DER SET;
# cryptography reads it as BER with a warning, as it would a real file's.
pytestmark = pytest.mark.filterwarnings(
    "ignore:PKCS#7 certificates could not be parsed as DER")

KEYS = {"pages", "pages_upper_bound", "digital_signature", "signatures",
        "unreadable_signatures"}
NOTHING = {"pages": None, "pages_upper_bound": None, "digital_signature": False,
           "signatures": [], "unreadable_signatures": 0}


def _sig_object(hexed: bytes, prefix: bytes = b"%PDF-1.7\n", *, number: int = 1,
                sig_type: bytes = b"/Type /Sig ") -> bytes:
    """``prefix`` plus one signature dictionary whose ByteRange leaves out
    exactly its own ``/Contents`` and ends at the end of the result."""
    head = (prefix + b"%d 0 obj << " % number + sig_type
            + b"/ByteRange [0 0000000000 0000000000 0000000000] /Contents ")
    start = len(head)
    end = start + len(hexed) + 2
    tail = b" >> endobj\n"
    head = head.replace(b"[0 0000000000 0000000000 0000000000]",
                        b"[0 %010d %010d %010d]" % (start, end, len(tail)))
    return head + b"<" + hexed + b">" + tail


def test_an_unsigned_pdf_has_pages_and_no_signature():
    facts = ariba_pdf.inspect(unsigned_pdf(pages=3))
    assert facts == {"pages": 3, "pages_upper_bound": 3, "digital_signature": False,
                     "signatures": [], "unreadable_signatures": 0}


def test_a_signed_pdf_names_the_signer_the_time_and_whole_file_coverage():
    facts = ariba_pdf.inspect(signed_pdf())
    assert facts["digital_signature"] is True and facts["unreadable_signatures"] == 0
    assert facts["signatures"] == [{"signer": SIGNER, "signed_at": "2026-11-12T09:30:00+01:00",
                                    "covers_whole_file": True, "readable": True}]


def test_the_page_count_does_not_count_the_page_a_signature_rewrote():
    data = signed_pdf(unsigned_pdf(pages=2))
    # The plain scan counts the rewritten page object twice; the page tree
    # does not, and neither does the bound (same object number = one page).
    assert count_pdf_pages(data) == 3
    facts = ariba_pdf.inspect(data)
    assert (facts["pages"], facts["pages_upper_bound"]) == (2, 2)


def test_a_decoy_page_tree_count_does_not_lower_the_bound():
    data = unsigned_pdf(pages=25)
    decoy = data + b"\n900 0 obj\n<< /Type /Pages /Count 1 /Kids [] >>\nendobj\n"
    real = data.replace(b"/Count 25", b"/Count 01")
    for hostile in (decoy, real):
        facts = ariba_pdf.inspect(hostile)
        assert facts["pages"] in (1, 25)
        assert facts["pages_upper_bound"] == 25


def test_the_signer_is_the_certificate_the_signer_info_names():
    (sig,) = ariba_pdf.inspect(signed_pdf(with_ca=True))["signatures"]
    assert sig["signer"] == SIGNER != CA_NAME
    (sig,) = ariba_pdf.inspect(signed_pdf(with_ca=False))["signatures"]
    assert sig["signer"] == SIGNER
    # Another end-entity certificate first in the bag: the SignerInfo decides.
    (sig,) = ariba_pdf.inspect(signed_pdf(decoy="Example Decoy"))["signatures"]
    assert sig["signer"] == SIGNER


def test_bytes_added_after_signing_are_not_covered():
    data = signed_pdf() + b"\n% appended after signing\n"
    (sig,) = ariba_pdf.inspect(data)["signatures"]
    assert sig["covers_whole_file"] is False and sig["signer"] == SIGNER


def test_a_signature_that_cannot_be_parsed_is_listed_unreadable_and_not_counted():
    facts = ariba_pdf.inspect(signed_pdf(contents=b"\x30\x82\xff\xffgarbage"))
    assert facts["digital_signature"] is False
    assert facts["unreadable_signatures"] == 1
    assert facts["signatures"] == [{"signer": None, "signed_at": "2026-11-12T09:30:00+01:00",
                                    "covers_whole_file": True, "readable": False}]


def test_one_readable_signature_is_enough_and_the_other_is_counted_unreadable():
    data = _sig_object(b"3082ffff00", signed_pdf(), number=500)
    facts = ariba_pdf.inspect(data)
    assert facts["digital_signature"] is True
    assert [s["readable"] for s in facts["signatures"]] == [True, False]
    assert facts["unreadable_signatures"] == 1


@pytest.mark.parametrize("stamp,expected", [
    (None, None),
    (b"D:2026", "2026-01-01T00:00:00"),
    (b"D:20261112093000Z", "2026-11-12T09:30:00+00:00"),
    (b"D:20261112093000-05'30'", "2026-11-12T09:30:00-05:30"),
    (b"D:20261399999999Z", None),
    (b"yesterday", None),
])
def test_signed_at_is_iso_or_null(stamp, expected):
    (sig,) = ariba_pdf.inspect(signed_pdf(signed_at=stamp))["signatures"]
    assert sig["signed_at"] == expected


@pytest.mark.parametrize("sig_type", [b"/Type /DocTimeStamp ", b"/Type /Annot "])
def test_a_dictionary_of_another_type_is_no_signature(sig_type):
    facts = ariba_pdf.inspect(signed_pdf(sig_type=sig_type))
    assert facts["digital_signature"] is False and facts["signatures"] == []


def test_a_signature_dictionary_without_type_but_with_contents_counts():
    assert ariba_pdf.inspect(signed_pdf(sig_type=b""))["digital_signature"] is True


@pytest.mark.parametrize("text", [
    b"/ByteRange [0 1 2 3]",
    b"/Type /Sig /ByteRange [0 1 2 3]",
    b"/Type /Sig /ByteRange [0 10 20 5] /Contents <3082>",
])
def test_a_byte_range_without_its_contents_is_no_signature(text):
    data = unsigned_pdf() + b"\n77 0 obj << " + text + b" >> endobj\n"
    facts = ariba_pdf.inspect(data)
    assert facts["digital_signature"] is False and facts["signatures"] == []
    assert facts["unreadable_signatures"] == 0


def test_at_most_twenty_signatures_are_read():
    data = unsigned_pdf()
    sig = pkcs7_detached(b"x").hex().encode()
    for n in range(30):
        data = _sig_object(sig, data, number=200 + n)
    facts = ariba_pdf.inspect(data)
    assert len(facts["signatures"]) == ariba_pdf.MAX_SIGNATURES == 20
    assert all(s["readable"] and s["signer"] == SIGNER for s in facts["signatures"])
    assert [s["covers_whole_file"] for s in facts["signatures"]] == [False] * 20


def test_the_same_signature_twice_is_one():
    data = signed_pdf()
    start = data.index(b"99 0 obj")
    again = data[start:data.index(b"endobj", start) + 6]
    facts = ariba_pdf.inspect(data + b"\n" + again)
    assert len(facts["signatures"]) == 1


@pytest.mark.parametrize("data", [
    b"",
    b"garbage" * 1000,
    xlsx_like(),
    b"%PDF-1.7\n" + b"/ByteRange" * 50_000,
    b"%PDF-1.7\n/ByteRange [" + b" " * 100_000,
    b"%PDF-1.7\n/ByteRange [0 9999999999 9999999999 1] /Contents <00>",
    b"%PDF-1.7\n/ByteRange [0 12 14 0] /Contents <zz>",
    b"%PDF-1.7\n/Type /Pages /Count 99999999999999",
    b"%PDF-1.7\n" + b"/Type /Pages" * 20_000,
    b"%PDF-1.7\n" + b"1 0 obj /Type /Page " * 20_000,
    b"%PDF-1.7\n/M (D:" + b"9" * 100_000,
    _sig_object(b"30800000"),
    _sig_object(b"3084ffffffff"),
    _sig_object(b"a0" * 50),
])
def test_hostile_input_never_raises_and_stays_bounded(data):
    started = time.perf_counter()
    facts = ariba_pdf.inspect(data)
    assert set(facts) == KEYS
    assert facts["pages"] is None or 1 <= facts["pages"]
    assert time.perf_counter() - started < 2


@pytest.mark.parametrize("value", [None, "text", 42, bytearray(b"%PDF-1.4")])
def test_input_that_is_not_bytes_is_no_pdf(value):
    assert ariba_pdf.inspect(value) == NOTHING


def test_a_file_over_the_cap_is_not_scanned(monkeypatch):
    monkeypatch.setattr(ariba_pdf, "MAX_BYTES", 100)
    assert ariba_pdf.inspect(signed_pdf()) == NOTHING


def test_contents_over_one_megabyte_of_hex_are_not_parsed():
    signature = pkcs7_detached(b"x").hex().encode()
    (sig,) = ariba_pdf.inspect(_sig_object(signature.ljust(100_000, b"0")))["signatures"]
    assert sig == {"signer": SIGNER, "signed_at": None, "covers_whole_file": True,
                   "readable": True}
    over = signature.ljust(ariba_pdf.MAX_CONTENTS_HEX + 2, b"0")
    (sig,) = ariba_pdf.inspect(_sig_object(over))["signatures"]
    assert sig["signer"] is None and sig["readable"] is False


def test_is_pdf_reads_the_header():
    assert ariba_pdf.is_pdf(unsigned_pdf())
    assert ariba_pdf.is_pdf(b"\xef\xbb\xbf  %PDF-1.4\n")
    assert not ariba_pdf.is_pdf(xlsx_like())
    assert not ariba_pdf.is_pdf(b"x" * 2000 + b"%PDF-1.4")
    assert not ariba_pdf.is_pdf("%PDF-1.4")
