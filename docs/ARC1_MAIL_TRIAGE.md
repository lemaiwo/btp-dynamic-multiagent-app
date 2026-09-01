# Mail triage with a live system check (ARC-1)

`gmail-sap-arc1-assistant` is the Gmail triage agent with the SAP system added
to it. It reads the threads labelled `agent`, and where a mail reports that
something is slow, hanging or failing, it looks at the system before it answers:
short dumps, the Gateway error log, SM02, existing traces and OData timings come
first, then the SAP documentation is searched for what that evidence points at,
and the draft says both.

Bundle: [`gmail-sap-arc1-assistant.config.json`](gmail-sap-arc1-assistant.config.json).
It is the Gmail agent's shape plus one MCP server, so
[GMAIL_SETUP.md](GMAIL_SETUP.md) remains the reference for the mailbox side —
the Google client, the `gmail.modify` scope, the `access_type=offline` query
string, and the label workflow that stops a thread being drafted twice.

## What it is made of

| Server | Auth | Tools |
| --- | --- | --- |
| `builtin:gmail` | `oauth2` (Google web client) | `gmail_*` — search, read, draft, labels. No send tool exists |
| `…arc1-mcp-server…/mcp` | `oauth2` with `dcr: true` | `infrabel_app_*` — `SAPDiagnose`, `SAPQuery`, `SAPContext`, `SAPRead`, `SAPSearch` |
| `mcp-sap-docs.marianzeis.de/mcp` | `none` | `mcp_sap_docs_*` — documentation search and fetch |

Skills: `sap-mail-triage` (unchanged, shared with the Gmail agent) and
`sap-performance-triage` (new — the diagnostic order, the read-only boundary,
and the rule that observation, documentation and inference stay separate in the
reply).

Tool names are prefixed per server because three are attached; the prefix is the
first DNS label truncated to 12 characters, which is why the ARC-1 tools read as
`infrabel_app_SAPDiagnose`. Change the ARC-1 host and that prefix changes with
it — the instructions name it explicitly, so update them together.

## The read-only boundary

The agent uses only the `SAPDiagnose` actions that read what already exists:
`dumps`, `gateway_errors`, `system_messages`, `traces`, `odata_perf`, `cds_sql`.
It never calls `trace_start`, `trace_cancel` or `set_sql_trace_state`. Those arm
a trace and need someone to reproduce the problem while it is armed, which an
unattended mailbox run cannot do; ARC-1 also gates them behind a write scope the
acc instance is not expected to grant. When a diagnosis genuinely needs a fresh
trace, the draft asks for the reproduction instead.

`odata_perf` and `authorization_trace` need `allowDataPreview` /
`SAP_ALLOW_DATA_PREVIEW` on the ARC-1 instance. Without it the tool returns an
error and the run continues on the other evidence — nothing breaks, the draft is
just thinner.

## Importing it

The committed bundle has an empty `client_secret`; secrets are not kept in git.

```bash
# Local: fills the secret from the repo-root client_secret_*.json and imports
python scripts/import_bundle.py docs/gmail-sap-arc1-assistant.config.json

# For a landscape this machine cannot reach: materialise a filled copy,
# upload it through /admin -> Import, then delete it
python scripts/import_bundle.py docs/gmail-sap-arc1-assistant.config.json --out filled.json
```

After importing, in **/admin → Agents → gmail-sap-arc1-assistant**:

- Set **Run-as identity** (`run_as_principal`). Exports deliberately do not
  carry it — it is landscape-specific — and a scheduled run has no logged-in
  user, so without it the run has no token.
- Press **Reload** so the registry rebuilds.

Sign-in carries over. Per-user OAuth tokens are keyed by MCP URL, not by agent,
so an account already connected to `builtin:gmail` and to ARC-1 through another
agent needs no new authorisation here. The Google `client_id`/`client_secret`
must match what the other Gmail agent uses, for the same reason: the token store
is shared and the refresh resolves whichever config it finds for that URL.

## Both Gmail agents watch `label:agent`

`gmail-sap-assistant` and `gmail-sap-arc1-assistant` are triggered by the same
label. If both run, each drafts on the same thread and they race to strip the
label — expect duplicate drafts. This one is a superset of the other, so run one
of them: disable `gmail-sap-assistant`, or move this agent to a label of its own
(change `label:agent` in the run prompt and the label names in step (e)).
