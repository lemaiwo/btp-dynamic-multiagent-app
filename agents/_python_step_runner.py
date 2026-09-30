"""Subprocess entry point for the ``python`` workflow step kind.

Started by :mod:`agents.step_kinds` as ``python -I -S -E <this file>`` with a
JSON payload on stdin::

    {"code": "...", "text": "...", "item": {...} | null,
     "sources": {"name": "text", ...}, "json": <parsed previous text> | null}

The code runs with the globals ``text``, ``item``, ``sources`` and
``json_data`` plus a fixed set of standard-library modules already imported.
Whatever it leaves in ``output`` is written back to stdout as one JSON line::

    {"ok": true, "output": "<str>", "prints": "<captured stdout>"}
    {"ok": false, "error": "<traceback tail>"}

What the sandbox does
---------------------
* ``__import__`` is replaced with one that admits only :data:`ALLOWED_MODULES`
  (and their listed submodules). ``os``, ``sys``, ``subprocess``, ``socket``,
  ``ctypes``, ``importlib``, ``builtins``, ``pathlib``, ``shutil`` and every
  other module are refused with ``ImportError``.
* ``open``, ``input``, ``breakpoint``, ``exit``, ``quit`` and ``help`` are
  removed from the builtins the code sees.
* ``print`` output is captured (capped) and returned separately, so the code
  cannot forge the result line.
* The parent process runs it with an empty environment, a throwaway working
  directory, an address-space and CPU limit, and kills it on timeout.

What it does not guarantee
--------------------------
This is admin-authored code inside the admin trust boundary: whoever can save
a workflow can already point agents at arbitrary MCP servers and read their
output. The sandbox exists to stop *mistakes* -- an accidental ``open()`` of
the app's files, a runaway loop, a memory blow-up -- and to keep the
subprocess from inheriting ``VCAP_SERVICES`` or any other secret. It is not a
security boundary against a hostile admin: the pre-imported modules reach
``sys`` transitively (``typing.sys``), and a determined author can get to the
interpreter through them. There is no network only because ``socket`` and
``urllib.request`` are not importable, not because egress is blocked.
"""

from __future__ import annotations

import builtins
import io
import json
import sys
import traceback

# Modules the code may import (by top-level name or full dotted name). Every
# one of them is imported below *before* __import__ is replaced, so the code
# can also use them without importing.
ALLOWED_MODULES = (
    "json", "re", "math", "datetime", "statistics", "collections",
    "collections.abc", "itertools", "string", "textwrap", "base64", "hashlib",
    "uuid", "decimal", "fractions", "functools", "operator", "typing",
    "dataclasses", "urllib", "urllib.parse",
)

REMOVED_BUILTINS = ("open", "input", "breakpoint", "exit", "quit", "help")

# Cap on the returned output and on captured prints, in characters. The parent
# enforces its own byte cap too; this one keeps the result line bounded even
# if the code builds a gigantic string.
OUTPUT_CAP = 64 * 1024
PRINT_CAP = 8 * 1024
TRACEBACK_TAIL = 2000


def _preimport() -> dict[str, object]:
    modules: dict[str, object] = {}
    for name in ALLOWED_MODULES:
        top = name.split(".", 1)[0]
        __import__(name)
        modules[top] = sys.modules[top]
    return modules


def _make_import(real_import):
    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0:
            raise ImportError("relative imports are not available in a python step")
        if name not in ALLOWED_MODULES:
            raise ImportError(
                f"module {name!r} is not available in a python step; allowed: "
                + ", ".join(ALLOWED_MODULES)
            )
        # ``from urllib import request`` names an allowed package but asks for
        # a submodule that is not on the list. Checked *before* the real import
        # runs, because __import__ with a fromlist loads the submodule as a
        # side effect and it would then hang off the package for good.
        module = sys.modules.get(name)
        for attr in fromlist or ():
            full = f"{name}.{attr}"
            if attr == "*" or full in ALLOWED_MODULES:
                continue
            present = getattr(module, attr, None) if module is not None else None
            if present is None or type(present).__name__ == "module":
                raise ImportError(f"module {full!r} is not available in a python step")
        return real_import(name, globals, locals, fromlist, level)

    return guarded_import


def _safe_builtins() -> dict[str, object]:
    safe = dict(vars(builtins))
    for name in REMOVED_BUILTINS:
        safe.pop(name, None)
    safe["__import__"] = _make_import(builtins.__import__)
    return safe


def _serialise(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


def main() -> int:
    real_stdout = sys.stdout
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as e:  # noqa: BLE001
        real_stdout.write(json.dumps({"ok": False, "error": f"bad payload: {e}"}))
        return 1

    modules = _preimport()
    code = str(payload.get("code") or "")
    scope: dict[str, object] = {
        "__name__": "__step__",
        "__builtins__": _safe_builtins(),
        "text": payload.get("text") or "",
        "item": payload.get("item"),
        "sources": payload.get("sources") or {},
        "json_data": payload.get("json"),
        **modules,
    }
    captured = io.StringIO()
    sys.stdout = captured
    sys.stdin = io.StringIO("")
    try:
        compiled = compile(code, "<python step>", "exec")
        exec(compiled, scope)  # noqa: S102 - admin-authored code, see module docstring
        if "output" not in scope:
            raise NameError("the code must assign a value to `output`")
        output = _serialise(scope["output"])
    except BaseException as e:  # noqa: BLE001 - SystemExit and KeyboardInterrupt too
        sys.stdout = real_stdout
        # Drop this runner's own frames: the author wants to see their line,
        # not the exec() call that reached it.
        frames = [f for f in traceback.extract_tb(e.__traceback__)
                  if f.filename != __file__]
        tail = "Traceback (most recent call last):\n" + "".join(
            traceback.format_list(frames)
        ) + "".join(traceback.format_exception_only(type(e), e))
        if len(tail) > TRACEBACK_TAIL:
            tail = "…" + tail[-TRACEBACK_TAIL:]
        real_stdout.write(json.dumps({"ok": False, "error": tail}))
        return 1
    finally:
        sys.stdout = real_stdout

    truncated = False
    if len(output) > OUTPUT_CAP:
        output = output[:OUTPUT_CAP]
        truncated = True
    prints = captured.getvalue()[:PRINT_CAP]
    real_stdout.write(json.dumps(
        {"ok": True, "output": output, "prints": prints, "truncated": truncated},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
