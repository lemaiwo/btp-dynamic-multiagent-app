# SAP note detail (`builtin:sapnotedetail`) — design

**Goal:** Resolve the fixing support-package level for a SAP security note, so
the monthly digest can say MISSING or IMPLEMENTED where it currently says
UNKNOWN.

**Status:** Design approved 2026-09-10. Phase 2 of the security-note
monitoring work; Phase 1 (`builtin:sapnotes`, NVD discovery) is merged.

**Related:**
- `docs/superpowers/specs/2026-09-10-sap-security-notes-monitoring-design.md`
- `docs/superpowers/plans/2026-09-10-sap-security-notes-phase1.md`
- `docs/SAP_SECURITY_NOTES.md`

---

## Why this route

Phase 1 leaves roughly half of critical notes in an UNKNOWN bucket, because
NVD gives a note number and a CVSS score but never says *which support package
fixes it*. Without that, "the note is not in `CWBNTCUST`" cannot be turned into
"the system is exposed" — a note delivered in a support package was never
downloaded through SNOTE and is legitimately absent.

That field lives in SAP's backbone. Three routes were investigated on
2026-09-10 and two were rejected:

**Rejected — the signed note download.** `notesdownloads.sap.com/note/{key}`
does serve a plain HTTP client presenting Basic credentials, and returns a
SAPCAR archive (`CAR 2.01`) holding `<note>_00.ZIP` and `SIGNATURE.SMF`. Two
things kill it: the archive needs SAPCAR's proprietary decompressor, and no
note-number → download-key mapping was found (SAP's documented example key
`0040000000874972019` returns an archive named for note 2755640, which the key
does not encode). Without addressing arbitrary notes the service is useless
here.

**Rejected — a supported API.** There is none. SAP Community asks 12793379
(2023) and 14450559 (2026) both went unanswered, and the latter reports the
Support Portal OData services "do not appear to expose complete SAP Note
details". The Maintenance Planner API that SAP Cloud ALM uses is real and is
reachable with a technical communication user, but it is undocumented and is a
BSP app (`controller.do`) with no UI5 manifest to name its services.

**Chosen — the private me.sap.com backend.** `mcp-sap-notes`
(`marianfoo/sap-mcp-servers`) reads note detail from
`https://me.sap.com/backend/raw/sapnotes/Detail`, whose response carries
`validity[]`, `supportPackages[]`, `supportPackagePatches[]`,
`correctionsSummary[]`, `component` and `priority`. `supportPackages` is
exactly the missing field.

### Two constraints this forces

**The credential must be a browser session, not an API credential.** me.sap.com
sits behind XSUAA/SAML. Probed on 2026-09-10, `Detail` returns a byte-identical
723-byte JS bootstrap page with HTTP Basic and anonymously
(`scripts/probe_me_sap_notes_api.py`) — Basic is ignored outright. The upstream
package solves this with Playwright: it fills the `accounts.sap.com` IAS form,
waits out MFA, and reuses the resulting cookies.

**The existing technical communication user cannot be used.** A technical
communication user cannot log on in dialog mode by definition, and this is a
dialog form login. This route needs a *dialog* S-user / Universal ID. That
credential is MFA-eligible, expires, and is personally attributed — which is
why the session is refreshed by a human on demand rather than held as a
service credential.

**Operating model:** the SAP session is short-lived (the upstream cache TTL is
12h) and the job is monthly, so the session is always stale when a run is due.
Runs are therefore **attended**: refresh the session, then trigger the run.
This is accepted, not worked around.

**Terms of service:** the upstream repo warns that these are private SAP APIs
and that use should be checked against SAP's ToS. That judgment belongs to the
operator, and the operator guide must repeat the warning.

---

## Architecture

```
scripts/refresh_sapnotes_session.py   (operator's machine, Playwright)
        │  login at accounts.sap.com, capture *.sap.com cookies
        ▼
POST /admin/api/sessions/{server_key}   (admin-only)
        │  upsert
        ▼
mcp_oauth_tokens (user_id, server_key)  access_token = Cookie header
        │                               expires_at   = now + TTL
        ▼
builtin:sapnotedetail  ──►  me.sap.com/backend/raw/sapnotes/Detail
        ▲
        │  get_note_details(notes)
   specialist agent  ──►  digest
```

Chromium never runs on Cloud Foundry. The only thing that reaches the platform
is a cookie string.

---

## Components

### 1. `agents/sapnotedetail_tools.py`

Mirrors `agents/sapnotes_tools.py` in shape and conventions.

**Produces:**

- `BUILTIN_SAPNOTEDETAIL_URL: str = "builtin:sapnotedetail"`
- `SapNoteDetailClient(http, cookie, *, max_concurrency=5)`
  - `async get_details(notes: list[str]) -> dict[str, Any]`
- `sapnotedetail_toolset(oauth, *, http=None, server_key=..., auth_mode=None) -> FunctionToolset`

**The single tool is batched by design:**

```python
get_note_details(notes: list[str]) -> dict[str, Any]
```

One call per note would be ~129 LLM turns for a monthly run. The tool takes the
whole list, fetches concurrently server-side behind a small semaphore
(default 5 — polite to a private endpoint, and the run is not latency-bound),
and returns a compact table.

**Per-note result:**

```python
{
  "note": "3771065",
  "status": "ok",                    # ok | unavailable
  "component": "SAP_BASIS",
  "component_text": "...",
  "priority": "HotNews",
  "validity": [{"software_component": "SAP_BASIS", "from": "740", "to": "757"}],
  "support_packages": [{"software_component": "SAP_BASIS", "level": "SAPKB74012"}],
}
```

**Envelope:**

```python
{
  "notes": [...],
  "count": 12,
  "unavailable": 0,
  "session_expired": False,   # drives the digest banner
}
```

`unavailable` and `session_expired` are part of the contract for the same
reason Phase 1's `skipped_*` counts are: a digest that silently drops notes
reads as complete when it is not.

### 2. New `auth_mode = "session"`

`AUTH_MODE_SESSION = "session"` in `agents/db.py`, added to `VALID_AUTH_MODES`.
Not in `OAUTH_CONFIG_MODES` — this mode carries no config block at all.

Validation mirrors the existing `builtin:jira` rule: `session` is accepted
**only** on `builtin:sapnotedetail`, and `builtin:sapnotedetail` accepts no
other mode. Both directions are rejected at the admin payload boundary, so a
misconfiguration is a 422 naming the field rather than a registry rebuild that
silently drops the agent.

### 3. Credential storage — reuse `mcp_oauth_tokens`

No new table. The row is `(user_id, server_key)` as today:

| column | value |
|---|---|
| `server_key` | `builtin:sapnotedetail` |
| `access_token` | the serialized `Cookie` header |
| `refresh_token` | `NULL` — there is nothing to refresh with |
| `expires_at` | now + TTL |

Consequences, all of them free:

- `token_status()` returns `valid` then `expired` with no change. `refreshable`
  is unreachable because `refresh_token` is NULL, which is correct: only a
  human with a browser can renew this.
- The admin credentials panel already renders state + expiry, so an operator
  sees a dead session *before* triggering a run.
- No secret lands in a server config block, so the project's "no secrets in the
  database" rule — which means *no secrets in config blocks*, per the existing
  `mcp_oauth_tokens` precedent — continues to hold.

### 4. `POST /admin/api/sessions/{server_key}`

Admin-only. Body:

```json
{"cookie": "<Cookie header>", "expires_in_hours": 12, "principal": "<optional>"}
```

`principal` defaults to the caller's own principal. The operator sets the
agent's `run_as_principal` to that same value using the existing "Use my
principal" button — the documented flow, and the one that avoids the known
footgun of typing an email address that matches no token row.

The endpoint never logs the cookie value.

### 5. `scripts/refresh_sapnotes_session.py`

Runs on the operator's machine, not on the platform:

1. Launch Playwright (headful when MFA is expected).
2. Navigate to a me.sap.com URL that forces the SAML flow.
3. Fill the `accounts.sap.com` form from `SAP_DIALOG_USER` / `SAP_DIALOG_PWD`,
   or let the operator complete it by hand.
4. Capture every cookie whose domain contains `sap.com` and serialize them as
   `name=value; name=value` — the upstream package takes all of them rather
   than naming specific cookies, so this does the same.
5. POST to the admin endpoint.

Playwright is an ops dependency: documented in the operator guide, kept out of
`requirements.txt` and out of the CF buildpack, exactly as the UI5 npm
toolchain is kept out of the Python dependencies.

### 6. Admin UI

Both admin UIs learn the mode: `ui5-admin/webapp/model/validators.ts`,
the server dialog fragment, `types.ts`, and the i18n labels; plus the legacy
`templates/admin.html` so the mode is not silently unselectable there. The
credentials panel needs no change — it reads `token_state` already.

---

## Failure handling

**Pre-flight.** `agents/workflow_runner.py` already records a pre-flight
credential failure as a failed step. `session` must be reported as a mode that
needs a user token so that machinery covers it. A run with a dead session
therefore never starts, and the run view shows a red step naming the server.

**Mid-run.** A note whose fetch returns an auth failure yields
`{"status": "unavailable", "reason": "session expired"}` and sets
`session_expired` on the envelope. Remaining notes are not attempted once the
session is known dead — retrying 100 times against a dead session is noise.
Notes already fetched are kept and returned.

**Digest.** The writer's existing rule stands: never report MISSING where the
analyst said UNKNOWN. An `unavailable` note is UNKNOWN with a stated reason,
and `session_expired` becomes a banner so nobody reads a thin report as a clean
bill of health.

**Transport errors** that are not auth failures (timeout, 5xx) mark that one
note `unavailable` and continue — one flaky note should not end a run.

---

## Testing

Follows `tests/test_sapnotes_tools.py`: `httpx.MockTransport`, no network.

- Parsing: `validity`, `supportPackages`, `supportPackagePatches`, component
  and priority extracted from a captured fixture.
- A note the endpoint does not know returns `unavailable`, not an exception.
- An auth failure mid-batch sets `session_expired`, keeps earlier results, and
  stops further fetches.
- The concurrency cap is respected.
- `session` is rejected on any URL other than `builtin:sapnotedetail`, and
  `builtin:sapnotedetail` is rejected under any other mode.
- The admin endpoint upserts a row and never echoes the cookie.
- UI5: validator accepts the new mode for the right URL, QUnit case added.

The suite is run the project's way — each script-style file executed directly,
plus pytest over the pytest-style files. `pytest tests/` alone is not this
project's runner.

---

## Out of scope

- **Rewiring the workflow.** Feeding `supportPackages` into the analyst prompt
  and reshaping the digest is a follow-up; this design delivers the toolset.
- **Unattended runs.** Requires a credential SAP does not offer for this
  endpoint. Scheduling stays Phase 3 and stays attended for this step.
- **Note text, corrections, TCI transports.** `correctionsSummary` is parsed
  only far enough to report that corrections exist.
- **Replacing ARC-1.** CVERS still says what is installed; this says what fixes
  the note. Both are needed.

---

## Risks and open questions

1. **`Detail`'s request AND response shapes are unconfirmed.** The upstream
   repo pins `Search` as `?q=<note>&t=E&maxResults=<n>`; `Detail`'s query
   parameters are inferred. The *field names* are equally inferred: the
   TypeScript interfaces confirm `validity`, `supportPackages`,
   `supportPackagePatches` and `correctionsSummary` exist and that each
   carries a `softwareComponent`, but not what the remaining keys are called
   in the raw JSON. The per-note result shown above is therefore a target,
   not a contract. **First implementation step is to capture one real
   response to a fixture and pin both shapes**; everything else builds on
   that, so it must not be guessed.
2. **Session lifetime is unmeasured.** 12h is the upstream *cache* TTL, not a
   measured SAP session lifetime. If the real session is shorter, the attended
   window tightens. The TTL is therefore a parameter of the refresh script,
   not a constant.
3. **A private API can change without notice.** Parsing must degrade to
   `unavailable` on an unexpected shape rather than raise, so a format change
   costs a thin digest rather than a failed run.
4. **ToS.** Restated here because it does not stop being true: this is a
   private API behind authentication, and whether to use it is the operator's
   call.
