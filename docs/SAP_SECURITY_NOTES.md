# SAP security notes monitoring — Phase 1

A manually-triggered workflow that lists every SAP security note scoring
CVSS 9.0–10.0, checks each one against a single ARC-1 ABAP system, and saves
one summary email as a draft.

Spec: `docs/superpowers/specs/2026-09-10-sap-security-notes-monitoring-design.md`
Plan: `docs/superpowers/plans/2026-09-10-sap-security-notes-phase1.md`

---

## 1. What it does, and why it reports the whole backlog

Three agents run in a line, each handing plain text to the next:

| Step | Agent | What it does |
|------|-------|--------------|
| 1 | `sapnotes-fetcher` | Calls `list_critical_notes` on `builtin:sapnotes` and returns every note at or above the CVSS floor |
| 2 | `sapnotes-system-analyst` | Resolves implementation status on one ABAP system with bulk `IN`-list queries via ARC-1 |
| 3 | `sapnotes-digest-writer` | Writes the digest and saves it as an Outlook draft |

The note list comes from the **public NVD API**, not from SAP. SAP is its own
CNA, so `sourceIdentifier=cna@sap.com` selects exactly the CVEs SAP assigned,
and recent ones carry a `me.sap.com/notes/{number}` reference. That makes NVD
a usable, unauthenticated index of SAP security notes with their scores —
which SAP itself publishes only behind an S-user login.

**The list is the whole backlog, not the current month.** This is deliberate.
SAP re-releases notes on later patch days without the underlying CVE being
re-published to NVD. Note 3771065 is the case that settled it: it appears on
a later patch day than its CVE's publication date, so any workflow scoped to
"notes published this month" silently drops it. Reporting everything and
letting the reader skip what is already implemented is the failure mode worth
having.

Two constraints are pinned in code and are not configurable:

- **The CVE source.** `cna@sap.com` in `agents/sapnotes_tools.py`. A different
  value does not narrow the toolset, it breaks it.
- **The digest audience.** `recipients` on the Outlook server config. Every
  other mail tool acts on a message that already exists, so the audience is
  whoever wrote in. Originating mail has no such anchor, and that choice must
  not be one an injected instruction can make.

Score filtering happens client-side: NVD's `cvssV3Severity` parameter returns
0 results when combined with `sourceIdentifier`, so the two do not compose.

## 2. Import the bundle

`docs/sap-security-notes-workflow.config.json` ships with placeholder values.
Replace them first — the file is gitignored, so edit it in place:

| Placeholder | Replace with |
|-------------|--------------|
| `https://arc1-replace-me.hana.ondemand.com/mcp` | Your ARC-1 MCP endpoint |
| `agent@replace-me.invalid` | The service mailbox the draft is saved in |
| `team@replace-me.invalid` | Who the digest is addressed to (comma-separated) |
| `<AZURE_APP_CLIENT_ID>` / `<TENANT_ID>` | The Entra app registration behind `builtin:outlook` — see `docs/OUTLOOK_SETUP.md` |

The Outlook **client secret is not in the file** and has to be added: either
paste it into `oauth.client_secret` on that server before importing, or leave
it out and fill the field in `/admin → Agents → sapnotes-digest-writer` after
the import. It is absent on purpose — `tests/test_agent_bundles.py` refuses
any bundle under `docs/` that carries a secret, and this file is the template
everyone starts from. Once you paste one in, that check will flag your local
copy; that is the check working, and the file is gitignored.

Then:

```bash
python scripts/import_bundle.py docs/sap-security-notes-workflow.config.json
```

## 3. Set the run-as identity

In `/admin → Agents`, set each agent's `run_as_principal` to the value
`GET /admin/api/whoami` returns **while logged in as the technical user that
will own the ARC-1 token**.

> An email address here matches no token row. Every run then fails claiming
> re-authorization is needed, with nothing in the logs pointing at the cause.
> It must be the opaque XSUAA user id, which is why the admin UI has a button
> for it rather than a free-text field.

## 4. Sign in once per ARC-1 server

From the admin credentials panel, sign in to the ARC-1 server **as the
technical user** — not as yourself. Signing in connects whoever is currently
authenticated, so to connect a service account you have to be signed into this
app as that account.

The stored refresh token has a 30-day TTL and cannot be renewed unattended;
a monthly run keeps it alive, a lapsed one needs a manual sign-in.

## 5. Press Reload

`/admin → Settings → Reload`. This rebuilds the orchestrator from the
database. Nothing takes effect until it runs.

## 6. Run it

```
POST /admin/api/workflows/{id}/run
```

The call acknowledges immediately and the run continues in the background;
watch it under `/admin → Workflow Runs`.

## 7. Read the draft

It lands in the configured mailbox as a draft, addressed to `recipients`.
Nothing is sent — see the limits below.

## 8. Known limits

- **Roughly half of critical notes are not ABAP-stack** — Commerce Cloud,
  SAP GUI, NPM packages, NetWeaver Java, BusinessObjects, Business One,
  SuccessFactors — and land in `UNKNOWN` until Phase 2 adds note-detail
  lookup. The digest names them rather than guessing.
- **A note absent from `CWBNTHEAD` is not missing.** That table holds only
  notes downloaded through SNOTE; a note delivered in a support package was
  never downloaded and is simply absent. The analyst prompt says so
  explicitly, because treating absence as MISSING would make most of the
  report wrong.
- **`allow_send` is off.** Phase 1 drafts only. Turning it on registers a
  `send_mail` tool on the digest writer; the recipients stay pinned either way.
- **NVD lags SAP Patch Day by several days.** A run on patch Tuesday will not
  see that day's notes yet.
- **One system.** Adding more is a Phase 3 concern; the digest writer already
  handles several analyst reports and names any system it was not given.

## 9. Optional: an NVD API key

Set `NVD_API_KEY` in the environment to raise the rate limit from 5 to 50
requests per 30 seconds. One run makes one request, so this only matters if
several workflows share the toolset. It is read from the environment and is
never stored in the database.

## Not in Phase 1

- **Phase 2 — note detail.** Deploying the SAP notes MCP server and adding
  `builtin:sapnotedetail`, which resolves the fixing support-package level and
  should move most of the `UNKNOWN` bucket into a real answer. Gated on an
  MFA-exempt S-user.
- **Phase 3 — scheduling.** The `jobscheduler` MTA resource, the
  `JOBSCHEDULER` scope, the monthly cron, a weekly keep-alive run for the
  30-day token, the remaining ARC-1 systems, and turning `allow_send` on.
