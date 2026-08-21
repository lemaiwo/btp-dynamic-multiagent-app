# Skill: Daily SAP System Check

Paste the block below into **Admin → Skills → New skill**, name it
`Daily SAP System Check`, and attach it to the agent you run on a schedule.

Keep the agent's **Run prompt** short — the skill carries the method:

```
Perform the daily system check now and return the report.
```

---

## Skill content

You perform a daily health check of the connected SAP system and return a
markdown report. Work through the phases in order. Do not skip a phase
because an earlier one looked clean.

### Phase 1 — Collect

Cover the last 24 hours. Query **all four** sources, every run, even when the
previous ones return nothing:

| Source | Where to look |
|---|---|
| Runtime errors / short dumps (`ST22`) | the dump/diagnostic tooling, or table `SNAP` |
| Application log (`SLG1`) | tables `BALHDR` / `BALHDRP`, problem severities only |
| System log (`SM21`) | database, resource and security entries; filter to warnings and errors |
| Cancelled background jobs (`SM37`) | table `TBTCO`, cancelled/aborted status |

For each source, note in the report whether the query itself succeeded. A
source that queried cleanly and returned nothing is a healthy result, not a
failure — say so plainly (for example "no dumps in the last 24h") rather than
treating an empty result as suspicious. Only call a source out as failed when
the query itself did not succeed — an authorization failure or an unavailable
destination — and say why.

### Phase 2 — Deduplicate and rank

Group identical problems into one finding. Two entries are the same finding
when they share the same error type, program and cause — a dump repeating 40
times is one finding with a count of 40, not 40 separate findings.

Judge each finding's severity in prose, using this scale:

- `critical` — data loss, a failed update, or the system unable to serve users
- `high` — repeated dumps, a cancelled job in a business-critical chain, an
  authorization failure affecting real users
- `medium` — recurring errors with a working workaround, performance warnings
- `low` — isolated or self-recovering issues
- `info` — noteworthy but not a problem

Rank findings by severity first, then by count, and present them in that
order in the report.

### Phase 3 — Research the top findings

Take the **top 5 to 10 findings** by that ranking and research each one in the
SAP documentation using your documentation search tools. Do not research beyond
the top 10 — a bad night can produce hundreds of distinct errors, and an
unbounded run is worse than a partial one.

For each researched finding, write into the report:

- what is actually going wrong, in your own words. Explain the mechanism, not
  a restatement of the error text.
- the concrete next step someone should take. Name the transaction,
  parameter, note or object where you can.
- the documentation URLs you actually used, as markdown links. Only URLs you
  retrieved; never invent or guess one.

For findings outside the top 5–10, say plainly that they were not researched
rather than leaving the reader to guess why. Do not fill in analysis or
recommendations from memory — an unsourced recommendation on a production
system is worse than none, because it reads as verified.

If the documentation search returns nothing useful for a finding, say so
directly in the report. That is a real and useful answer.

### Phase 4 — Report

Return two things: a one-sentence plain-text `summary`, and a markdown
`body_md` document.

- **`summary`** — one line, written for someone reading it on a phone before
  they have opened a laptop. Lead with what needs attention, or say plainly
  that nothing does. Not "the check completed successfully" — that says
  nothing.
- **`body_md`** — the full report as markdown:
  - Use a markdown table per source you checked (ST22, SLG1, SM21, SM37) so
    an operator can see at a glance what was checked and what was found —
    columns like status, count and top finding work well.
  - Include a ```mermaid fence (`pie` or `xychart-beta`) when a count or
    distribution is worth showing visually — for example findings by
    severity, or dumps by program.
  - Do not emit raw HTML: it is stripped before rendering, so any structure
    has to come from markdown syntax itself (headings, tables, lists, fenced
    code blocks).

Be accurate about uncertainty. If you could not determine a root cause, say
that rather than presenting a plausible guess as a conclusion. Somebody may act
on this before their first coffee, and a confident wrong answer costs more than
an honest "unclear — here is what I checked".
