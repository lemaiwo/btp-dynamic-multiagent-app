"""The refusals of the InA built-in: one closed list of codes, one error class.

Every failure of an InA call reaches the model as ``{"error": {code,
message, hint?}}`` with ``code`` from :data:`ERROR_CODES` (design addendum
(k), part 2 point 2). The list is closed on purpose: an agent's instructions
and the tool layer branch on the code, and a code that appears only at run
time would be one nobody wrote a rule for. :class:`InaError` refuses any
other code when it is made, so a typo is a test failure, not a new code.

``message`` is a fixed text written here, or -- for ``sap_error`` only --
the InA ``Messages[].Text`` of SAP's answer, cleaned, URL-masked and cut at
500 characters. Nothing else of an answer (no body, header, URL, token or
exception text) is ever put into an error.
"""

from __future__ import annotations

ERROR_CODES: tuple[str, ...] = (
    "destination_error",
    "proxy_refused",
    "user_token_required",
    "unauthorized",
    "forbidden",
    "csrf_failed",
    "query_unknown",
    "variable_required",
    "invalid_filter",
    "result_too_large",
    "sap_error",
    "unreachable",
    "timeout",
    "not_json",
)


class InaError(Exception):
    """A refusal or a failed InA call, in the shape the tools hand to the model.

    ``status`` is the HTTP status when there was one (for the log and the
    admin test call), and ``admin_text`` the fixed text of a destination
    that was refused as configured (``destination_auth.DestinationRefused``:
    it names the destination and the rule, for the admin Describe and Test
    answers). Neither is part of :meth:`to_dict`, so neither reaches the
    model.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        status: int | None = None,
        admin_text: str | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            # The code only: it is a constant of this package, never input.
            raise ValueError(f"not an InA error code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.status = status
        self.admin_text = admin_text

    def to_dict(self) -> dict[str, str]:
        out = {"code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


__all__ = ["ERROR_CODES", "InaError"]
