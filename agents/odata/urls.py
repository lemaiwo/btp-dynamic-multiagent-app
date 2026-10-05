"""URL confinement for the OData built-in.

The host of every OData call comes from a BTP destination, and the
destination's credential (or the signed-in user's identity) travels with the
request. Anything an admin or a model can type is therefore only ever a
*path below that host*; these helpers refuse whatever could turn it into
another host or another endpoint.

Refusals never repeat the value: a path field is a place where a URL with a
token in its query gets pasted by mistake, and the message ends up in a 422.
"""

from __future__ import annotations

MAX_SERVICE_PATH = 512


def confine_service_path(value: str) -> str:
    """The service root below the destination's host, or ``ValueError``.

    A service path is stored once per catalogue service and every request of
    that service is built on it, so it is held to a stricter shape than a
    one-off request path: absolute, no query or fragment (``sap-client``
    belongs in the destination), no ``.``/``..`` segment, no empty segment,
    no trailing ``/`` (entity set names are appended with one). ``%`` and
    whitespace are refused as well: a real service root needs neither, and
    an encoded ``%2e%2e`` is a ``..`` to the server that decodes it.
    """
    if not isinstance(value, str):
        raise ValueError("service_path must be a string")
    if not value:
        raise ValueError("service_path is required")
    if len(value) > MAX_SERVICE_PATH:
        raise ValueError(f"service_path must be at most {MAX_SERVICE_PATH} characters")
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value):
        raise ValueError("service_path must not contain control characters")
    if any(ch.isspace() for ch in value):
        raise ValueError("service_path must not contain whitespace")
    if "\\" in value:
        raise ValueError("service_path must not contain a backslash")
    if "://" in value:
        raise ValueError("service_path is a path, not a URL; the host comes from the destination")
    if not value.startswith("/"):
        raise ValueError("service_path must start with '/'")
    if "?" in value or "#" in value:
        raise ValueError(
            "service_path must not contain '?' or '#'; query parameters belong in the destination"
        )
    if "%" in value:
        raise ValueError("service_path must not contain '%'")
    if value.endswith("/"):
        raise ValueError("service_path must not end with '/'")
    if "//" in value:
        raise ValueError("service_path must not contain '//'")
    if ".." in value or any(segment == "." for segment in value.split("/")):
        raise ValueError("service_path must not contain '.' or '..' segments")
    return value
