"""The report an API-triggered agent run produces.

Two fields, because two things consume a run: the runs list wants one
scannable line, and the run page wants the whole document. The document is
markdown so the agent's prompt — not a schema — decides what a report
contains. pydantic-ai validates this as the agent's output_type and retries
the model on a mismatch, so the shape is enforced rather than hoped for.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RunReport(BaseModel):
    """A run's report: one line for the list, one markdown document for the page."""

    summary: str = Field(
        description=(
            "One sentence, plain text, no markdown. Shown in the runs table "
            "and used as the notification subject."
        )
    )
    body_md: str = Field(
        description=(
            "The full report as GitHub-flavoured markdown. Use headings and "
            "tables; put tabular data in a markdown table rather than prose. "
            "For a chart or diagram, emit a ```mermaid fenced block "
            "(pie, xychart-beta, flowchart, sequenceDiagram). Do not emit raw "
            "HTML: it is stripped before rendering."
        )
    )
