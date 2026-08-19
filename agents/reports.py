"""Structured report an API-triggered agent run produces.

The report is data, not prose: the run detail page, the notification summary
and any later trending all read this one structure. pydantic-ai validates it
as the agent's output_type and retries the model on a mismatch, so the shape
is enforced rather than hoped for.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "info"]


class Finding(BaseModel):
    title: str
    severity: Severity
    count: int = 1
    affected: list[str] = Field(default_factory=list)
    detail: str
    analysis: str | None = None
    recommendation: str | None = None
    references: list[str] = Field(default_factory=list)


class ReportSection(BaseModel):
    source_key: str
    title: str
    # Whether the source was actually queried. A checked source with zero
    # findings is a healthy result; an unchecked one is a gap in the run.
    checked: bool
    findings: list[Finding] = Field(default_factory=list)
    note: str | None = None


class RunReport(BaseModel):
    summary: str
    overall_severity: Severity
    sections: list[ReportSection] = Field(default_factory=list)


def missing_sections(report: RunReport, expected: list[str]) -> list[str]:
    """Expected source_keys the report did not actually check.

    A section with no findings is complete — most days nothing is wrong. Only
    a section that is absent, or present with checked=False, counts as missing.
    """
    checked = {s.source_key for s in report.sections if s.checked}
    return [key for key in expected if key not in checked]
