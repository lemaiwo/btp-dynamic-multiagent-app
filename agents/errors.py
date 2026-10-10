"""Exception text that may leave the log: to a model, a tool card, a run row.

Why this exists: an exception's text is written for a developer and often
carries the request it failed on. ``httpx.HTTPStatusError`` embeds the URL AS
SENT, i.e. after ``DestinationAuth`` appended the destination's
``URL.queries.*`` (a credential can be one) or with the ``?token=`` of a
configured MCP URL. That text used to reach the model, the chat tool card
and ``job_runs`` / ``workflow_runs`` rows verbatim.

What stays: the class name and the wording of the message, because admins
debug a failed run from them. What goes: every URL (``[url]``), every query
string (``?[masked]``), ``user:password@`` userinfo, ``Authorization`` /
``Bearer`` values, ``token=`` / ``password=``-style pairs and JWTs
(``[masked]``). A host with a port alone (``db:5432``) or a time
(``10:25``) is deliberately NOT treated as a URL. The log keeps the full
text; use these helpers only for what leaves it.
"""

from __future__ import annotations

import re

MAX_MESSAGE_CHARS = 400
_MAX_GROUP_CHARS = 2000

# Applied in this order: a URL swallows its own query and userinfo first.
_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    # scheme://anything up to whitespace, a quote or an angle bracket
    (re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]*"), "[url]"),
    # scheme-relative //host/... (not the // of a scheme, handled above)
    (re.compile(r"(?<![:\w/])//[A-Za-z0-9][^\s'\"<>]*"), "[url]"),
    # header-style credentials: "Authorization: Bearer x", "authorization=x"
    (
        re.compile(
            r"(?i)\b((?:proxy-)?authorization|x-user-token|cookie|set-cookie)"
            r"(\s*[:=]\s*)(?:(?:bearer|basic|negotiate)\s+)?[^\s'\",;]+"
        ),
        r"\1\2[masked]",
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]{8,}=*"), r"\1 [masked]"),
    # a JWT anywhere
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), "[masked]"),
    # key=value pairs whose key names a secret
    (
        re.compile(
            r"(?i)\b(access_token|refresh_token|id_token|token|client_secret|secret"
            r"|password|passwd|pwd|api_?key|apikey|sig|signature|sap-password)"
            r"=[^\s&'\"]*"
        ),
        r"\1=[masked]",
    ),
    # any remaining query string: "?a=b&c=d" or "?<secret>=1"
    (re.compile(r"\?[^\s'\"?]*=[^\s'\"]*"), "?[masked]"),
    # userinfo without a scheme: "user:pass@host"
    (re.compile(r"[^\s:/@'\"]+:[^\s/@'\"]+@(?=[A-Za-z0-9])"), "[masked]@"),
)


def mask_text(text: str) -> str:
    """``text`` with URLs, query strings and credentials replaced; not cut."""
    for pattern, replacement in _MASKS:
        text = pattern.sub(replacement, text)
    return text


def exception_class(exc: BaseException) -> str:
    """Only the class name: for a place where not even a masked message may go."""
    return type(exc).__name__


def describe_exception(exc: BaseException, *, limit: int = MAX_MESSAGE_CHARS) -> str:
    """``"<ClassName>: <masked message>"``, the message cut at ``limit``.

    An ``ExceptionGroup`` (MCP's and anyio's task groups raise them) is
    unwrapped into its members, joined by ``"; "``, so the class that names
    the real failure is shown rather than ``ExceptionGroup``.
    """
    if isinstance(exc, BaseExceptionGroup):
        text = "; ".join(describe_exception(e, limit=limit) for e in exc.exceptions)
        return text[:_MAX_GROUP_CHARS]
    message = mask_text(str(exc))
    if len(message) > limit:
        message = message[:limit] + "..."
    name = exception_class(exc)
    return f"{name}: {message}" if message else name
