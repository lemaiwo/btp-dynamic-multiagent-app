"""SAP security notes, discovered through the public NVD API.

SAP is its own CNA, so ``sourceIdentifier=cna@sap.com`` selects exactly the
CVEs SAP assigned -- 1696 of them -- and every recent one carries a
``me.sap.com/notes/{number}`` reference. That makes NVD a usable, public,
unauthenticated index of SAP security notes with their CVSS scores, which
SAP itself publishes only behind an S-user login.

Two things this module does NOT do, both deliberate:

* It does not filter server-side by severity. NVD's ``cvssV3Severity``
  parameter returns 0 results when combined with ``sourceIdentifier`` -- the
  two filters do not compose -- so the threshold is applied here.
* It does not treat NVD as a mirror of SAP's monthly patch day. SAP
  re-releases notes on later patch days without NVD re-publishing the CVE,
  so a publication-date window silently loses notes. The toolset returns the
  whole backlog by default and lets the caller narrow it.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
from pydantic_ai.toolsets import FunctionToolset

from agents.lookback import parse_lookback

logger = logging.getLogger(__name__)

BUILTIN_SAPNOTES_URL = "builtin:sapnotes"

NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"

# Pinned, never configurable. This is the definition of "an SAP-assigned
# CVE", not a preference -- a different value makes the toolset meaningless,
# and exposing it would let an admin silently break it from the UI.
SAP_CNA_SOURCE = "cna@sap.com"

DEFAULT_MIN_SCORE = 9.0
# NVD's own ceiling for this endpoint. The full SAP set is ~1696 records, so
# one request covers it.
NVD_MAX_PAGE = 2000
DEFAULT_MAX_NOTES = 500

# Both URL forms SAP has used. The 6-digit floor rejects incidental numbers
# in a path; every real note number is at least seven digits today, and the
# oldest are six.
_NOTE_RE = re.compile(r"notes/(\d{6,})")

# Checked in order. NVD and SAP both score these records and neither is
# guaranteed present, so the first available wins rather than one provider
# being assumed.
_METRIC_KEYS = ("cvssMetricV31", "cvssMetricV40", "cvssMetricV30")


def extract_note_number(references: Any) -> str | None:
    """The SAP note number from a CVE's references, or None."""
    for ref in references or []:
        if not isinstance(ref, dict):
            continue
        match = _NOTE_RE.search(str(ref.get("url") or ""))
        if match:
            return match.group(1)
    return None


def extract_score(metrics: Any) -> float | None:
    """The CVSS base score from a CVE's metrics, or None when unscored."""
    if not isinstance(metrics, dict):
        return None
    for key in _METRIC_KEYS:
        entries = metrics.get(key) or []
        if not entries or not isinstance(entries[0], dict):
            continue
        data = entries[0].get("cvssData") or {}
        try:
            return float(data["baseScore"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def extract_description(descriptions: Any) -> str:
    """The English description, or the first available, or empty."""
    entries = [d for d in (descriptions or []) if isinstance(d, dict)]
    for entry in entries:
        if entry.get("lang") == "en":
            return str(entry.get("value") or "").strip()
    return str(entries[0].get("value") or "").strip() if entries else ""


class SapNotesClient:
    """Thin wrapper over the one NVD endpoint this app uses.

    Takes an ``httpx.AsyncClient`` so tests can inject a mock transport.

    ``min_score`` is the CVSS threshold; 9.0 selects exactly what SAP calls
    HotNews. ``lookback_minutes`` is an optional ceiling and is normally
    unset -- see the module docstring for why a publication window loses
    notes.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        min_score: float = DEFAULT_MIN_SCORE,
        lookback_minutes: int | None = None,
    ) -> None:
        self._http = http
        self.min_score = float(min_score)
        self.lookback_minutes = lookback_minutes

    def _params(self, limit: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "sourceIdentifier": SAP_CNA_SOURCE,
            "resultsPerPage": min(max(limit, 1), NVD_MAX_PAGE),
        }
        if self.lookback_minutes:
            # Imported here so the module has no import-time clock dependency.
            from datetime import datetime, timedelta, timezone

            end = datetime.now(timezone.utc)
            start = end - timedelta(minutes=self.lookback_minutes)
            fmt = "%Y-%m-%dT%H:%M:%S.000"
            params["pubStartDate"] = start.strftime(fmt)
            params["pubEndDate"] = end.strftime(fmt)
        return params

    async def list_critical_notes(self, limit: int = DEFAULT_MAX_NOTES) -> dict[str, Any]:
        """Notes at or above the score threshold, highest score first.

        Returns the notes plus counts of what was skipped. The counts are
        part of the contract, not diagnostics: a digest that silently drops
        records reads as complete when it is not.
        """
        headers = {}
        api_key = os.environ.get("NVD_API_KEY", "").strip()
        if api_key:
            # Raises the anonymous rate limit from 5 to 50 requests/30s.
            headers["apiKey"] = api_key

        response = await self._http.get(NVD_URL, params=self._params(limit), headers=headers)
        response.raise_for_status()
        payload = response.json()

        by_note: dict[str, dict[str, Any]] = {}
        skipped_no_score = 0
        skipped_no_note = 0

        for item in payload.get("vulnerabilities") or []:
            cve = (item or {}).get("cve") or {}
            score = extract_score(cve.get("metrics"))
            if score is None:
                skipped_no_score += 1
                continue
            if score < self.min_score:
                continue
            note = extract_note_number(cve.get("references"))
            if not note:
                skipped_no_note += 1
                continue
            entry = {
                "note": note,
                "cve": str(cve.get("id") or ""),
                "score": score,
                "published": str(cve.get("published") or "")[:10],
                "description": extract_description(cve.get("descriptions")),
            }
            # One note can carry several CVEs. Keep the worst score: the
            # digest ranks by severity and the lower one would understate it.
            existing = by_note.get(note)
            if existing is None or score > existing["score"]:
                by_note[note] = entry

        notes = sorted(by_note.values(), key=lambda n: (-n["score"], n["note"]))
        return {
            "notes": notes,
            "count": len(notes),
            "min_score": self.min_score,
            "skipped_no_score": skipped_no_score,
            "skipped_no_note": skipped_no_note,
        }


def sapnotes_toolset(
    oauth: dict[str, Any],
    *,
    http: httpx.AsyncClient | None = None,
    server_key: str = BUILTIN_SAPNOTES_URL,
    auth_mode: str | None = None,
    min_score: float | None = None,
    lookback: str | None = None,
) -> FunctionToolset:
    """The SAP notes toolset for one agent, ready for ``Agent(toolsets=...)``.

    ``server_key`` and ``auth_mode`` come from
    :func:`agents.builtins.build_builtin_toolset` in production; ``auth_mode``
    is accepted and ignored because NVD is public and there is no caller
    identity to act as. The rest default to the matching values in ``oauth``
    and are overridable so tests can set them without a config block.
    """
    raw_score = min_score if min_score is not None else oauth.get("min_score")
    try:
        resolved_score = DEFAULT_MIN_SCORE if raw_score in (None, "") else float(raw_score)
    except (TypeError, ValueError):
        raise ValueError(
            f"{server_key}: invalid min_score {raw_score!r}; use a number between 0 and 10"
        ) from None
    if not 0.0 <= resolved_score <= 10.0:
        raise ValueError(f"{server_key}: min_score must be between 0 and 10, got {raw_score!r}")

    # Parsed at build time so a typo stops the registry rebuild with a clear
    # message rather than surfacing mid-run as an empty note list.
    window = parse_lookback(lookback if lookback is not None else oauth.get("lookback"))

    session = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    client = SapNotesClient(session, min_score=resolved_score, lookback_minutes=window)
    toolset = FunctionToolset()
    # Exposed for `agents.registry`, which closes `http_client` on the old
    # build's servers after a reload.
    toolset.http_client = session  # type: ignore[attr-defined]

    @toolset.tool
    async def list_critical_notes(limit: int = DEFAULT_MAX_NOTES) -> dict[str, Any]:
        """List SAP security notes at or above the configured CVSS threshold.

        Returns every such note SAP has ever published, newest and highest
        severity first -- not only this month's. SAP re-releases notes on
        later patch days without the underlying CVE being re-published, so a
        month-scoped list silently misses them.

        The result carries `skipped_no_score` and `skipped_no_note` counts.
        Report them; they are the difference between "no other notes exist"
        and "some could not be read".

        Args:
            limit: Maximum notes to return.
        """
        return await client.list_critical_notes(limit)

    return toolset
