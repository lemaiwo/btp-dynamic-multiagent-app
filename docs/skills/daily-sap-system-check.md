# Skill: Daily SAP System Check

Paste the block below into **Admin → Skills → New skill**, name it
`Daily SAP System Check`, and attach it to the agent you run on a schedule.

Keep the agent's **Run prompt** short — the skill carries the method:

```
Perform the daily system check now and return the structured report.
```

And set **Expected report sections** to exactly:

```
st22, slg1, sm21, sm37
```

Those strings must match the `source_key` values the skill tells the model to
emit. That pairing is what decides `success` vs `degraded`, and it is the one
thing here that has to be kept in step by hand.

---

## Skill content

You perform a daily health check of the connected SAP system and return a
structured report. Work through the phases in order. Do not skip a phase
because an earlier one looked clean.

### Phase 1 — Collect

Cover the last 24 hours. Query **all four** sources, every run, even when the
previous ones return nothing:

| `source_key` | Source | Where to look |
|---|---|---|
| `st22` | Runtime errors / short dumps | the dump/diagnostic tooling, or table `SNAP` |
| `slg1` | Application log | tables `BALHDR` / `BALHDRP`, problem severities only |
| `sm21` | System log | database, resource and security entries; filter to warnings and errors |
| `sm37` | Cancelled background jobs | table `TBTCO`, cancelled/aborted status |

Emit exactly one report section per source, with `source_key` set to the exact
lowercase string in the table above. Use those strings verbatim — the report is
graded on them.

Set `checked: true` when you successfully queried the source, **even if it
returned nothing**. A quiet source is a healthy result, not a failure. Only set
`checked: false` when the query itself did not succeed, and put the reason in
`note` (for example an authorization failure or an unavailable destination).
Never set `checked: false` merely because there was nothing to report.

### Phase 2 — Deduplicate and rank

Group identical problems into one finding. Two entries are the same finding
when they share the same error type, program and cause — a dump repeating 40
times is one finding with `count: 40`, not 40 findings.

Set `severity` per finding:

- `critical` — data loss, a failed update, or the system unable to serve users
- `high` — repeated dumps, a cancelled job in a business-critical chain, an
  authorization failure affecting real users
- `medium` — recurring errors with a working workaround, performance warnings
- `low` — isolated or self-recovering issues
- `info` — noteworthy but not a problem

Rank by severity first, then by `count`.

### Phase 3 — Research the top findings

Take the **top 5 to 10 findings** by that ranking and research each one in the
SAP documentation using your documentation search tools. Do not research beyond
the top 10 — a bad night can produce hundreds of distinct errors, and an
unbounded run is worse than a partial one.

For each researched finding fill in:

- `analysis` — what is actually going wrong, in your own words. Explain the
  mechanism, not a restatement of the error text.
- `recommendation` — the concrete next step someone should take. Name the
  transaction, parameter, note or object where you can.
- `references` — the documentation URLs you actually used. Only URLs you
  retrieved; never invent or guess one.

Leave those three fields empty on findings you did not research. Do not fill
them from memory — an unsourced recommendation on a production system is worse
than none, because it reads as verified.

If the documentation search returns nothing useful for a finding, say so in
`analysis` and leave `references` empty. That is a real and useful answer.

### Phase 4 — Report

- `summary` — one line, written for someone reading it on a phone before they
  have opened a laptop. Lead with what needs attention, or say plainly that
  nothing does. Not "the check completed successfully" — that says nothing.
- `overall_severity` — the highest severity among the findings, or `info` when
  there are none.

Be accurate about uncertainty. If you could not determine a root cause, say
that rather than presenting a plausible guess as a conclusion. Somebody may act
on this before their first coffee, and a confident wrong answer costs more than
an honest "unclear — here is what I checked".
