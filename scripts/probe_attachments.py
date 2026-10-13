"""Go/no-go probe: does the configured model read an image and a PDF?

Answers, before the invoke API is built, whether a file given to the agent as
``BinaryContent`` reaches the model through SAP AI Core unchanged:

  1. Can a model be built for the name (``AICORE_MODEL`` or ``--model``)?
  2. Does the model read a PNG? (a generated image: one red square on white)
  3. Does the model read a PDF? (a generated one-page PDF holding a code)
  4. Does it read both in one prompt, as the invoke API will send them?

Each step asks the model a question only the file can answer and checks the
answer, so a route that silently drops the file part fails the step rather
than passing on a guess. A Claude name goes through AI Core's Bedrock proxy
(``agents.shared._build_bedrock_model``), anything else through the
OpenAI-compatible one; the probe reports which.

It touches no database and uses the app's own model factory, so what passes
here is what the agent will get. Run it with the app's ``.env`` (``AICORE_*``):

    .venv/bin/python scripts/probe_attachments.py [--model NAME] [--pdf FILE]
        [--image FILE]

Your own ``--pdf`` / ``--image`` are sent as given, and you judge the printed
answer yourself (the check is skipped). Prints the model name, the route,
sizes, durations and the model's answers; never a credential. Exit code 0 when
every step passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import struct
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv is in requirements.txt
    pass

PDF_CODE = "7391-KX"
SQUARE_COLOUR = "red"


# ---------------------------------------------------------------------------
# Generated test files (no PIL, no reportlab: the probe adds no dependency)
# ---------------------------------------------------------------------------
def make_png(size: int = 240) -> bytes:
    """A white PNG with one red square in the middle."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    lo, hi = size // 3, 2 * size // 3
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter: none
        for x in range(size):
            inside = lo <= x < hi and lo <= y < hi
            rows += b"\xff\x00\x00" if inside else b"\xff\xff\xff"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def make_pdf(code: str = PDF_CODE) -> bytes:
    """A one-page PDF that says ``Receipt code: <code>`` in Helvetica."""
    text = f"BT /F1 24 Tf 72 700 Td (Receipt code: {code}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def media_type_of(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".pdf": "application/pdf",
    }.get(ext, "application/octet-stream")


async def ask(model, parts: list, question: str) -> tuple[str, float]:
    from pydantic_ai import Agent

    agent = Agent(
        model,
        instructions=(
            "You are a probe. Answer the question from the attached file(s) "
            "only. If you received no file, or cannot read it, answer exactly: "
            "NO FILE."
        ),
    )
    started = time.monotonic()
    result = await agent.run([*parts, question])
    return str(result.output).strip(), time.monotonic() - started


def report(label: str, ok: bool | None, answer: str, seconds: float) -> None:
    verdict = "PASS" if ok else ("INFO" if ok is None else "FAIL")
    shown = answer.replace("\n", " ")
    if len(shown) > 200:
        shown = shown[:200] + "..."
    print(f"{verdict}  {label} ({seconds:.1f}s): {shown!r}")


async def main(args: argparse.Namespace) -> int:
    try:
        from pydantic_ai import BinaryContent

        from agents.shared import _is_anthropic, default_model_name, get_model
    except Exception as e:  # noqa: BLE001 - say what is missing, nothing more
        print(f"FAIL  imports: {type(e).__name__} (run inside the app's venv)")
        return 1

    name = args.model or default_model_name()
    route = "bedrock (AI Core Amazon proxy)" if _is_anthropic(name) else "openai-compatible"
    try:
        model = get_model(name)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  model {name!r} via {route}: {type(e).__name__}")
        return 1
    print(f"PASS  model {name!r} built, route: {route}")

    failed = False

    # --- image -------------------------------------------------------------
    if args.image:
        path = Path(args.image)
        image = BinaryContent(path.read_bytes(), media_type=media_type_of(path))
        image_check = None
        image_question = "Describe what this image shows in one sentence."
    else:
        image = BinaryContent(make_png(), media_type="image/png")
        image_check = SQUARE_COLOUR
        image_question = (
            "What colour is the square in this image? Answer with the colour "
            "word only."
        )
    print(f"      image: {image.media_type}, {len(image.data)} bytes")
    try:
        answer, took = await ask(model, [image], image_question)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  image: {type(e).__name__}: {str(e)[:300]}")
        failed = True
        answer, took = "", 0.0
    else:
        ok = None if image_check is None else image_check in answer.lower()
        report("image", ok, answer, took)
        failed |= ok is False

    # --- pdf ---------------------------------------------------------------
    if args.pdf:
        path = Path(args.pdf)
        pdf = BinaryContent(path.read_bytes(), media_type="application/pdf")
        pdf_check = None
        pdf_question = "Summarise this document in one sentence."
    else:
        pdf = BinaryContent(make_pdf(), media_type="application/pdf")
        pdf_check = PDF_CODE
        pdf_question = (
            "What is the receipt code printed in this document? Answer with "
            "the code only."
        )
    print(f"      pdf: {len(pdf.data)} bytes")
    try:
        answer, took = await ask(model, [pdf], pdf_question)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  pdf: {type(e).__name__}: {str(e)[:300]}")
        failed = True
    else:
        ok = None if pdf_check is None else pdf_check in answer
        report("pdf", ok, answer, took)
        failed |= ok is False

    # --- both in one prompt (what the invoke API sends) --------------------
    if image_check and pdf_check:
        try:
            answer, took = await ask(
                model,
                [image, pdf],
                "Answer on one line as '<colour of the square>; <receipt code>'.",
            )
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  both: {type(e).__name__}: {str(e)[:300]}")
            failed = True
        else:
            ok = SQUARE_COLOUR in answer.lower() and PDF_CODE in answer
            report("both", ok, answer, took)
            failed |= not ok

    print("RESULT", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", help="deployment name (default: AICORE_MODEL)")
    parser.add_argument("--image", help="your own PNG/JPEG instead of the generated one")
    parser.add_argument("--pdf", help="your own PDF instead of the generated one")
    if os.environ.get("AICORE_MODEL") is None and "--model" not in sys.argv:
        print("note: AICORE_MODEL is not set; the default model name is used")
    sys.exit(asyncio.run(main(parser.parse_args())))
