"""abapGit-style workspace paths for ABAP objects (``src/<TYPE>/<name>.<ext>``)."""

from __future__ import annotations

import re

MAX_PATH_LENGTH = 200

EXT_BY_TYPE: dict[str, str] = {
    "CLAS": "clas.abap",
    "INTF": "intf.abap",
    "PROG": "prog.abap",
    "DDLS": "ddls.asddls",
    "BDEF": "bdef.asbdef",
    "DCLS": "dcls.asdcls",
    "DDLX": "ddlx.asddlxs",
    "SRVD": "srvd.srvdsrv",
    "FUNC": "func.abap",
    # An include is a program to abapGit; the directory tells them apart.
    "INCL": "prog.abap",
}


def _file_name(name: str) -> str:
    return name.strip().lower().replace("/", "#")


def path_for(type: str, name: str, include: str | None = None) -> str:
    """Workspace path of an object; ``include`` is a local class section."""
    t = (type or "").strip().upper()
    if t not in EXT_BY_TYPE:
        raise ValueError(f"unsupported object type {type!r}")
    if not (name or "").strip():
        raise ValueError("an object name is required")
    base, _, last = EXT_BY_TYPE[t].partition(".")
    ext = f"{base}.{include}.{last}" if include else EXT_BY_TYPE[t]
    return f"src/{t}/{_file_name(name)}.{ext}"


def object_for(path: str) -> tuple[str, str, str | None] | None:
    """(type, NAME upper, include) for an object path, else None (scratch file)."""
    parts = (path or "").split("/")
    if len(parts) != 3 or parts[0] != "src" or parts[1] not in EXT_BY_TYPE:
        return None
    t, fname = parts[1], parts[2]
    base = EXT_BY_TYPE[t].partition(".")[0]
    tail = "." + EXT_BY_TYPE[t]
    include = None
    if fname.endswith(tail):
        stem = fname[: -len(tail)]
    else:
        ext_last = EXT_BY_TYPE[t].rsplit(".", 1)[1]
        marker = f".{base}."
        if not fname.endswith("." + ext_last) or marker not in fname:
            return None
        stem, _, rest = fname.partition(marker)
        include = rest[: -(len(ext_last) + 1)]
        if not include:
            return None
    if not stem:
        return None
    return t, stem.replace("#", "/").upper(), include


def clean_workspace_path(path: str) -> str:
    """Same checks as the deep scratchpad (`agents.deep._clean_path`) plus
    no absolute paths and no ``..`` segments."""
    p = (path or "").strip()
    if not p:
        raise ValueError("a file path is required.")
    if len(p) > MAX_PATH_LENGTH:
        raise ValueError(f"file paths may be at most {MAX_PATH_LENGTH} characters.")
    if any(ch in p for ch in ("\x00", "\n", "\r")):
        raise ValueError("a file path may not contain newlines or NUL bytes.")
    if p.startswith("/") or p.startswith("\\") or (len(p) > 1 and p[1] == ":"):
        raise ValueError("absolute paths are not allowed.")
    if ".." in p.replace("\\", "/").split("/"):
        raise ValueError("'..' segments are not allowed.")
    return p


# --- program/include of a runtime error -> workspace object -------------------

# A class pool names its parts ``<class padded with = to 30><suffix>``.
_POOL_NAME_LENGTH = 30
_POOL_SUFFIX = re.compile(r"CP|CU|CO|CI|CCDEF|CCIMP|CCMAC|CCAU|CM[0-9A-Z]{3}")
# Suffix -> the SAPRead ``include`` of the class-local section (ARC-1's names).
_CLASS_SECTIONS = {
    "CCDEF": "definitions",
    "CCIMP": "implementations",
    "CCMAC": "macros",
    "CCAU": "testclasses",
}
_OBJECT_NAME = re.compile(r"[A-Z0-9_/$]{1,40}")
_CLASS_NAME = re.compile(r"[A-Z0-9_/]{1,30}")


def _upper(value: object) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def class_include(name: object) -> tuple[str, str] | None:
    """``(class, suffix)`` when ``name`` is a part of a class pool
    (``ZCL_X=====...=CM001`` -> ``("ZCL_X", "CM001")``), else ``None``.

    The padding decides: a name is a pool part when it carries ``=`` before
    the suffix, or is longer than 30 characters (a class name of exactly
    30). ``ZREPORT_CP`` is a program.
    """
    text = _upper(name)
    if "=" in text:
        cut = text.rindex("=") + 1
    elif len(text) > _POOL_NAME_LENGTH:
        cut = _POOL_NAME_LENGTH
    else:
        return None
    cls, suffix = text[:cut].rstrip("="), text[cut:]
    if not _CLASS_NAME.fullmatch(cls) or not _POOL_SUFFIX.fullmatch(suffix):
        return None
    return cls, suffix


def resolve_include(
    program: object, include: object
) -> tuple[str, str, str | None, bool] | None:
    """Where the source of a dump's ``program``/``include`` can be read.

    Returns ``(type, name, class_section, line_is_exact)`` for ``SAPRead``
    and ``path_for``, or ``None`` when there is nothing to open. The line of
    a runtime error counts inside its include, so ``line_is_exact`` is true
    only when that include is the source being opened:

    * class pool, local section (``CCDEF``/``CCIMP``/``CCMAC``/``CCAU``):
      the class with that section, exact;
    * class pool, method include (``CMnnn``) or a section of the class
      definition (``CU``/``CO``/``CI``): the class, **not** exact -- the
      full class source is assembled from those includes;
    * class pool itself (``CP``, or no include): the class, exact;
    * ``include`` equal to ``program`` (or empty): the program, exact --
      unless it is a function pool (``SAPL...``), whose frame holds no code;
    * any other include: that include, exact.

    Every name must be a plain ABAP object name; anything else (a system
    include in ``<...>``, blanks, a path) gives ``None``.
    """
    prog, incl = _upper(program), _upper(include)
    if not prog or (include is not None and not isinstance(include, str)):
        return None
    part = class_include(incl or prog)
    if part is not None:
        cls, suffix = part
        if suffix in _CLASS_SECTIONS:
            return "CLAS", cls, _CLASS_SECTIONS[suffix], True
        return "CLAS", cls, None, suffix == "CP"
    if not incl or incl == prog:
        if prog.startswith("SAPL") or not _OBJECT_NAME.fullmatch(prog):
            return None
        return "PROG", prog, None, True
    if not _OBJECT_NAME.fullmatch(incl):
        return None
    return "INCL", incl, None, True
