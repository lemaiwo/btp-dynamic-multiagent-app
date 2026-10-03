"""abapGit-style workspace paths for ABAP objects (``src/<TYPE>/<name>.<ext>``)."""

from __future__ import annotations

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
