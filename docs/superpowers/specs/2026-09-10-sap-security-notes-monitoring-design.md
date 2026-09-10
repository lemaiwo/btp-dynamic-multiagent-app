# Monthly SAP security note monitoring — design

**Date:** 2026-09-10
**Status:** Draft, awaiting review

## Goal

Once a month, produce a single email telling the team which SAP security
notes scoring CVSS 9.0–10.0 are not yet implemented on our SAP systems.

The report covers **every such note, not only the current month's**. SAP
re-releases notes on later patch days, so "this month's notes" is not a
stable set (see Evidence). The question worth answering is "are we exposed
today", which is answered by the systems, not by a publication date.

## Non-goals

- **No use-case-specific storage.** The app provides agents, instructions
  and connections. It does not get a `sap_security_notes` table. Whether a
  note is implemented is derived from the SAP system on every run, not
  recorded here.
- No remediation. The report is triage input for Basis, not an action.
- No compliance claim. See Honesty requirement.

## Evidence

Everything below was measured against live systems on 2026-09-10, not
assumed.

### NVD is a usable discovery source

`sourceIdentifier=cna@sap.com` selects CVEs assigned by SAP as its own CNA
— 1696 records all-time. Public, unauthenticated, no API key required.

For September 2026 it returned 17 CVEs, all published 2026-09-08 (patch
day), every one carrying a `me.sap.com/notes/{number}` reference and a
CVSS score. Four scored ≥9.0.

All-time, filtering `baseScore >= 9.0` yields 138 CVEs collapsing to **129
distinct notes**, growing roughly 23/year. Data quality across all 1696:
145 (8.5%) carry no note number, 133 (7.8%) carry no CVSS score — almost
all old records. In the recent 120-day window, 79 of 81 had note links.

### NVD is not a mirror of SAP's patch day

Security vendors reported **seven** HotNews notes for September; NVD's
September window yields four. Traced to cause: note **3771065** (CVSS
10.0) appears on September's patch day but exists in NVD as
`CVE-2026-58231` published **2026-08-11**. SAP re-released the note; NVD
did not re-publish the CVE.

Switching to `lastModStartDate` recovers only some (five instead of four).
Updating an SAP *note* does not reliably touch the *CVE* record.

**This is why the design tracks the full backlog rather than a window.**

### NVD API constraints

- `cvssV3Severity=CRITICAL` returns 53 results for September alone, but
  **0** when combined with `sourceIdentifier`. The two filters do not
  compose. Filter `baseScore >= 9.0` client-side.
- Scores come from two providers — 937 records scored by `nvd@nist.gov`,
  715 by `cna@sap.com`. Read whichever metric is present; do not assume
  one. Check `cvssMetricV31`, then `V40`, then `V30`.
- Structured CPE product data is present on only 97 of 138 criticals and
  absent on all three most recent — enrichment lags. The English
  description always names the product, so product classification is an
  LLM task, not a lookup.
- Rate limit: 5 requests / 30s anonymous, 50 with a free API key.
- Max 120-day span per date-filtered query. The backlog query is not
  date-filtered, so this does not apply to the main call.

### Only about half the notes are checkable against an ABAP system

Classifying the 138 criticals by product family: ~73 ABAP-stack, ~29
clearly not, ~36 needing judgment. The 2026 criticals include
`@sap/cds-mtxs` (an NPM library), SAP GUI for Java, Commerce Cloud and
NetWeaver Java — none of which live in `CVERS` or `SNOTE`.

### The system-side check works and is cheap

Validated against SI4/100 via ARC-1:

- `CVERS` returns all 122 software components with SP levels.
- `CWBNTHEAD` accepts an IN-list — 7 note numbers, one query, 11ms.
  Columns: `NUMM, VERSNO, THEMK, INSTA, MNUMM, MYEAR, INCOMPLETE`.
- `CWBNTCUST` likewise. Columns: `NUMM, NTSTATUS, PRSTATUS, CWBUSER,
  REQID, IMPL_PROGRESS`.

Checking 129 notes against one system is two or three queries, not 129.
This is the reason the design does **not** fan out per note.

### Absence is not evidence of non-implementation

Of seven critical notes probed, only one (3746332) existed in
`CWBNTHEAD` at all. `CWBNTHEAD` holds only notes *downloaded via SNOTE*.
A note delivered in a support package was never downloaded, so it is
absent — indistinguishable from never applied.

Resolving this requires the note's **fixing support-package level**, which
NVD does not carry. That is the specific reason the SAP notes MCP server
is needed.

## Honesty requirement

The digest reports three buckets, never two:

| Bucket | Meaning |
| --- | --- |
| **Implemented** | Confirmed present in `CWBNTCUST`, or the system's SP level for the affected component is at or beyond the note's fixing SP. |
| **Missing** | ABAP-stack note, applicable component present, and neither condition above holds. |
| **Not determinable** | Non-ABAP product, or the fixing SP could not be resolved, or the system was not reached. |

A note reported as "missing" when it merely shipped in an SP is a false
alarm. Enough of those and the report gets ignored by the third month.
The third bucket is not a hedge; it is the correct answer for roughly
half the corpus.

The digest must also **enumerate which systems it actually reached**.
`registry.py:561-569` skips a server that fails to construct and builds
the agent anyway; a failed branch degrades a run to `partial` rather than
failing loudly. Without an explicit "checked: X, Y — not reached: Z" line,
a partial sweep reads as a clean bill of health.

## Architecture

Linear workflow, no fan-out. Fan-out is rejected because main-line steps
after a fan-out execute inside the per-item loop
(`workflow_runner.py:513-519`), so a digest step would fire once per item
— 129 emails instead of one. There is no run-level join step in the
engine.

```
POST /api/workflows/sap-security-notes/run   (BTP Job Scheduling, monthly)
  │
  ├─ 1. notes-fetcher        builtin:sapnotes  → NVD, all notes ≥ 9.0
  │
  ├─ 2. system analysts      one agent per ARC-1 system, attached as peers
  │        each: bulk CWBNTHEAD + CWBNTCUST + CVERS query
  │        returns per-note status + the system's identity
  │
  ├─ 3. note-detail          builtin:sapnotedetail → SAP notes MCP
  │        ONLY for candidates: ABAP-stack AND not already confirmed
  │        implemented. Expect 10–20, not 129.
  │
  └─ 4. digest-writer        one summary across all notes and all systems
           → send_mail(to, subject, body)
```

Step 3 runs after step 2 deliberately: the cheap pass narrows the set
before we scrape SAP's portal. Fetching all 129 full notes every month
through undocumented endpoints is both slow and a ToS liability.

### Why one agent per system

ARC-1 binds one MCP server to one SAP system via its BTP destination; no
tool takes a system parameter. A multi-system endpoint exists but is not
configured yet, and systems are being set up one at a time — so this
design assumes one MCP server per system.

Attaching several ARC-1 servers to a *single* agent is rejected:
`_compute_tool_prefixes` (`registry.py:97-123`) slugs same-host URLs to
one prefix and disambiguates **positionally** — `infrabel_app_0_SAPQuery`,
`infrabel_app_1_SAPQuery`. Nothing in the tool name identifies the SAP
system, and the index follows list order, so reordering
`extra_servers_json` silently remaps tools to different systems. On a
security report that is a wrong answer delivered confidently.

One server per agent means **no prefix at all** (prefixes are computed
only when `len(specs) > 1`), and system identity lives in the agent name.

If the multi-system ARC-1 endpoint later lands, this collapses to a single
analyst agent and the peer structure can be removed. The design should not
block on it.

## Components to build

### 1. `builtin:sapnotes` — NVD discovery

New file `agents/sapnotes_tools.py`, modelled on `agents/jira_tools.py`.

Tools:
- `list_critical_notes()` → note number, CVE id, score, published date,
  English description, product hint.

Config, via the `oauth` block on the server entry:

| Key | Default | Notes |
| --- | --- | --- |
| `min_score` | `9.0` | The one knob that belongs in the UI. |
| `lookback` | unset | Optional **ceiling**, not the primary filter. Unset means the whole backlog, which is the intended mode. Reuses `parse_lookback` from `agents/lookback.py`. |
| `api_key` | unset | Optional NVD key; raises the rate limit. |

`sourceIdentifier=cna@sap.com` is **pinned in code**, not exposed. It is
the definition of "SAP-assigned CVE", not a preference; exposing it lets
an admin silently break the tool. This follows the pinning precedent in
`jira_tools.py`, where project and status are fixed server-side and only
`lookback` is caller-adjustable.

Score filtering is client-side (see NVD API constraints).

`auth_mode: "none"` — NVD is public.

### 2. `builtin:sapnotedetail` — full note content

Wraps the SAP notes MCP server
(`marianfoo/sap-mcp-servers/packages/notes`) for the fields NVD lacks,
principally the fixing support-package level per component.

Deployment reality, from its README:
- Requires **S-user credentials** and drives a headless Chromium
  (Playwright) against SAP's private, undocumented endpoints.
- README warns: *"This MCP Server uses private APIs from SAP behind
  authentication. Please check whether the use violates SAP's ToS."*
- One fixed S-user per instance; session cookies cached 12h.
- **MFA breaks unattended operation** — it needs manual code entry in
  headful mode. The S-user must be MFA-exempt or this cannot be scheduled.

These are prerequisites, not implementation details. If the S-user cannot
be MFA-exempt, step 3 is not deliverable and the design degrades to the
two-bucket check with a much larger "not determinable" bucket.

### 3. Auth mode for the notes MCP

No existing mode fits a static-bearer MCP server. `jwt` forwards the
user's token; `oauth2`/`app_only` run OAuth flows; `destination` is
rejected outright for MCP URLs (`admin.py:286`) and
`create_mcp_server` has no destination branch — a bypass would silently
forward the user's XSUAA token to that host (`shared.py:279-280`).

**Option A (recommended).** Deploy the notes MCP on an internal-only CF
route (`apps.internal`, no public route), `ACCESS_TOKEN` unset, with a
network policy scoping access to this app. Use `auth_mode: "none"`. The
boundary is the network. **Zero code.** Precedent exists: the seed
already carries an `http://` server on `none`.

**Option B.** New `static_bearer` auth mode (13 chars, within
`AUTH_MODE_MAX_LENGTH = 16`, asserted at import in `db.py:62-74`). The
secret comes from a CF env var, **never the DB** — consistent with
`_clean_destination` / `_clean_client_credentials`, which deliberately
drop credential keys rather than storing them. Touches `db.py`,
`shared.py`, `admin.py`, `validators.ts`.

Option B is defense-in-depth on top of A, not an alternative to it.
Recommend shipping A and adding B only if the network policy is judged
insufficient.

### 4. `send_mail` — new capability

Neither email builtin can originate mail. `gmail_tools.py:173-206`
derives the recipient from the inbound message's reply-to header and
forces a `Re:` subject; it takes a `thread_id` and has no send tool at
all. `outlook_tools.py` `send_reply` posts to Graph
`/messages/{id}/reply`. Both are strictly reply-scoped.

Add to `agents/outlook_tools.py`:

```python
send_mail(to: list[str], subject: str, body: str) -> dict
create_mail_draft(to: list[str], subject: str, body: str) -> dict
```

Graph `POST /users/{mailbox}/sendMail` and `POST /users/{mailbox}/messages`.
`OutlookClient._root` / `._req` plumbing already exists
(`outlook_tools.py:130-175`).

Gated by the existing `allow_send` flag, registered conditionally in the
same `if can_send:` block as `send_reply` (`outlook_tools.py:446-451`).

**Recipients must be pinned in config**, not chosen by the model — the
same server-side pinning `jira_tools.py` applies to project and status.
An agent that can email arbitrary addresses is a new security boundary
this app does not currently have.

A new `recipients` config key goes in `_CC_KEYS`, alongside `allow_send`
in `_clean_client_credentials` (`db.py:962-989`) — **not** `_DEST_KEYS`,
which serves destination-mode servers only. The outlook builtin runs on
`app_only`/`oauth2`, so its config is cleaned by the client-credentials
path. Store it comma-separated, matching how `status` and `labels` are
handled (`db.py:1010-1016`).

The existing `allow_send` docstring makes the argument for pinning
directly: an app-only token's scope covers the whole registration, "so a
tenant that granted Mail.Send would otherwise hand every agent bound to
it the ability to send mail. The capability has to be turned on here, per
server, on purpose." Recipients deserve the same treatment.

**Ship with `allow_send` off.** The first several runs draft only, which
is the pattern already used for `outlook-triage`.

### 5. Scheduling infrastructure

Not currently deployed. `JOBSCHEDULER` appears in `agents/auth.py:238-260`
but **nowhere** in `xs-security.json`, `mta.yaml`, or `elia.mtaext`, and
there is no `jobscheduler` resource in the MTA. Locally the check is
permissive (`auth.py:247-248` returns a fake principal without a
validator), which is why this has not surfaced.

Required, per `docs/superpowers/specs/2026-08-19-scheduled-agent-runs-design.md:258-261`:
- `jobscheduler` resource, plan `standard`, in `mta.yaml`
- `JOBSCHEDULER` scope in `xs-security.json`
- `grant-as-authority-to-apps: ["$XSSERVICENAME(jobscheduler-service)"]`

Monthly cron, SAP's 7-field UTC format
(`Year Month Day DayOfWeek Hour Minute Second`), 06:00 on the 20th:
`* * 20 * 6 0 0`

**The 20th, not the 1st** — NVD lags SAP Patch Day (the second Tuesday)
by several days.

### 6. Keep-alive run — not optional

ARC-1 supports only `authorization_code` + `refresh_token`; no client
credentials. A scheduled run depends on a stored refresh token for a
technical user. Nothing refreshes tokens proactively — refresh happens
only on use, inside the httpx auth flow (`oauth2.py:421-435`).

XSUAA's stock refresh-token validity is 30 days. **A monthly job sits
exactly on that boundary.** Renewal cannot be scripted: `authorization_code`
requires a human at a browser.

Mitigation: a weekly keep-alive run that touches every ARC-1 server purely
to exercise its token. Without it the expected failure mode is the monthly
report silently not arriving.

Compounding this, `has_usable_token` (`oauth2.py:617-630`) passes if a
refresh token merely *exists* — it never tests that it still works. A dead
token clears preflight and fails mid-run.

## Configuration prerequisites

These gate delivery and are landscape work, not code:

1. **One ARC-1 URL per system**, following the existing `/SIA/100/mcp`
   convention. Only one ARC-1 endpoint exists in the repo today.
2. **One interactive OAuth sign-in per ARC-1 server**, performed while
   logged in **as the technical user** — tokens are keyed by
   `(user_id, server_key)` and `server_key` is the normalized MCP URL, so
   each system URL needs its own sign-in. The admin credentials panel
   (`admin.py:780-850`) renders one `login_url` per oauth2 server.
3. **`run_as_principal` must be the opaque XSUAA principal**
   (`user_uuid`/`sub`), obtained from `GET /admin/api/whoami` while logged
   in as the technical user. An email address here matches no token row and
   every run fails claiming re-authorization is needed. Note the existing
   Jira workflows carry `"wouter.lemaire@elia.be"` in this field and work
   only because `builtin:jira` uses `destination` auth and needs no token.
4. **SAP authorizations** for the technical user's mapped SAP account to
   read `CVERS`, `CWBNTHEAD`, `CWBNTCUST` on each system.
5. **An MFA-exempt S-user** for the notes MCP, if step 3 is in scope.

## Failure modes

| Failure | Handling |
| --- | --- |
| NVD unreachable / rate-limited | Fail the run loudly. A digest without the note list is worse than no digest. |
| One ARC-1 system unreachable | Continue; that system appears under "not reached" in the digest. Never silently omitted. |
| All ARC-1 systems unreachable | Fail the run. |
| Notes MCP unreachable | Continue; affected notes fall to "not determinable" with the reason stated. |
| Refresh token expired | Preflight fails the run before any model call. Keep-alive run is the prevention. |
| Note has no CVSS or no note number | Skip, and report the count of skipped records in the digest footer. |

## Testing

Following `tests/test_jira_tools.py` (34KB, covers pinning, confinement,
gating, 401 retry):

- `tests/test_sapnotes_tools.py` — NVD response parsing across all three
  metric shapes (`cvssMetricV31`/`V40`/`V30`); note-number extraction from
  both URL forms (`me.sap.com/notes/N` and the legacy
  `launchpad.support.sap.com/#/notes/N`); `min_score` filtering;
  `sourceIdentifier` pinned and not overridable; records missing score or
  note number handled without raising.
- `tests/test_outlook_tools.py` — extend: `send_mail` absent when
  `allow_send` is off; recipients pinned server-side and not overridable
  by a tool argument.
- `tests/test_admin_api.py` — validation for the new builtin URLs and
  auth-mode combinations.
- `ui5-admin/webapp/test/unit/validators.qunit.ts` — mirror the new
  builtin URLs (`:20` already asserts an unknown `builtin:teams` is
  rejected).

A cached NVD response should be committed as a fixture so tests do not
hit the network.

## Phasing

**Phase 1 — the report, drafting only.** `builtin:sapnotes`, `send_mail`
behind `allow_send` off, one system, manual trigger via
`POST /admin/api/workflows/{id}/run`. Proves the pipeline end to end and
lets us see how large the "not determinable" bucket really is.

**Phase 2 — determination.** Notes MCP deployment + `builtin:sapnotedetail`,
which should move most of that bucket into a real answer.

**Phase 3 — scheduled.** Job Scheduling infrastructure, keep-alive run,
remaining systems, `allow_send` on.

Phase 1 is independently useful. If phase 2 proves undeliverable (the MFA
constraint), phase 1 plus phase 3 still produces a monthly report — it
just carries a larger "needs manual review" section.

## Open questions

1. Which systems, and their ARC-1 URLs. Being provisioned one at a time.
2. Digest recipients — the pinned `recipients` config value.
3. Whether the multi-system ARC-1 endpoint lands soon enough to skip the
   peer structure.
4. Whether an MFA-exempt S-user is obtainable, which decides phase 2.

## Rejected alternatives

- **A `sap_security_notes` table.** Violates the platform boundary; the
  SAP system is the state.
- **Rolling `lookback` window as the primary filter.** Provably misses
  re-released notes — four of seven HotNews for September.
- **Fan-out per note.** 129 emails; replaces three SQL statements with
  129 model calls; the bulk IN-list makes it unnecessary.
- **Fan-out per note per system.** The above multiplied by system count,
  against a 30-minute budget and a 20-item parallelism cap.
- **Several ARC-1 servers on one agent.** Positional tool prefixes carry
  no system identity and silently remap on reorder.
- **`mcp-sap-docs` as the note source.** Indexes ABAP/UI5/CAP/BTP
  documentation, SAP Help and SAP Community — it does not index SAP Notes
  or Security Notes at all.
- **Notes MCP for discovery.** Its `search` takes only free text and a
  language; no CVSS, date, or security-note filter exists.
