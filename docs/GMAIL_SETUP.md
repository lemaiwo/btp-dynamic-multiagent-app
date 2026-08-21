# Handling email with an agent (Gmail)

Move a mail into a Gmail label, and a scheduled agent reads it, writes a draft
reply, and takes the label off again.

## Status (2026-08-22)

**Google's hosted Gmail MCP server does not work with a self-registered OAuth
client.** `https://gmailmcp.googleapis.com/mcp/v1` authenticates a token issued
by your own OAuth client and then refuses to authorize it:

```
403  "The caller does not have permission"
```

This was tested to exhaustion against a real personal Gmail account. See
[Appendix: what was ruled out](#appendix-what-was-ruled-out) — the short version
is that scope, identity, API enablement and audience were each eliminated, and
the same token reads the mailbox fine through `gmail.googleapis.com`. Google's
own documentation only ever shows redirect URIs for two clients — Antigravity
and Claude — which fits a service that accepts only its own approved clients.

**The route that should work is a self-hosted MCP server** calling the Gmail API
directly, since a token from your own client demonstrably works against that
API. [`workspace-mcp`](https://workspacemcp.com/docs) (MIT, `--transport
streamable-http`, OAuth 2.1, with `draft_gmail_message` and
`modify_gmail_message_labels`) is the obvious candidate. **That path has not been
validated yet** — everything in section 5 is from its documentation, not from a
working deployment here.

Sections 1–4 are validated and worth keeping either way: the OAuth client you
create is the same one a self-hosted server will use.

## How it works

There is no push, no webhook and no trigger subsystem. A scheduled run wakes up
every few minutes and the agent does the work itself through MCP tools:

```
BTP Job Scheduler  --cron-->  POST /api/agents/<slug>/run
                                     |
                              agent runs its prompt
                                     |
        search/list(label) -> read -> create draft -> relabel
                                     |
                              label removed = message handled
```

Removing the label is what makes it idempotent: a handled mail no longer matches
the search, so the next run skips it.

## Prerequisites

- A Google account you control. A personal `@gmail.com` account is simplest — a
  Workspace account may need admin approval for the OAuth client and scopes.
- A Google Cloud project — step 1.1 creates one. No billing account needed.
- `gcloud` CLI, or the Cloud Console if you prefer clicking.
- The app running with a reachable OAuth callback. Locally that is
  `http://127.0.0.1:7932/oauth/callback`; on BTP, your approuter URL plus
  `/oauth/callback`.

## 1. Google Cloud

### 1.1 Create or pick a project, and find its ID

In the [Cloud Console](https://console.cloud.google.com), use the project picker
in the top bar, or from the CLI:

```bash
gcloud projects create my-gmail-agent-2026 --name="Gmail agent"
gcloud projects list --format="table(projectId, name)"
```

**The project ID is not the project name.** The name is a display label; the ID
is permanent, globally unique, and often gets a number appended — a project
named "Gmail agent" may have the ID `gmail-agent-483920`. Every `--project` flag
wants the ID.

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud config get-value project
```

**Billing is not required** for the Gmail API at this scale.

### 1.2 Enable the Gmail API

```bash
gcloud services enable gmail.googleapis.com --project=YOUR_PROJECT_ID
```

`gmailmcp.googleapis.com` is only needed for Google's hosted server, which does
not work here — see Status. Enabling it does no harm if you want to retry later.

### 1.3 OAuth consent screen

**Where:** [console.cloud.google.com](https://console.cloud.google.com) →
**APIs & Services** → **Google Auth Platform**.

There is no menu item called "OAuth consent screen" any more; Google folded it
into **Google Auth Platform**, split across *Branding*, *Audience*, *Data
access* and *Clients*. A fresh project shows a **Get started** wizard first.

**Branding** — App name, your address for User support email and Developer
contact email. Google will not let you create a client until this is saved.

**Audience** — User type **External**, publishing status **Testing**, then
**Test users → Add users → your own Gmail address**. Not optional: in Testing
only listed addresses can consent, and everyone else gets `access_denied` with
no useful explanation.

Testing mode also means refresh tokens expire after **7 days**. Fine for trying
this; the thing to change if you keep it.

**Expect an "unverified app" warning** at consent — normal for a Testing
project. Click **Advanced → Go to ‹app name› (unsafe)**. It is unverified
because Google has not reviewed it; the app is yours.

### 1.4 Create the OAuth client

**Where:** the **Clients** tab → **Create client** (the older **APIs & Services
→ Credentials → Create credentials → OAuth client ID** works too).

Application type: **Web application**. This matters — `Oauth2Config.from_spec`
(`agents/oauth2.py:102`) reads `oauth["client_secret"]` unconditionally and
raises `KeyError` without one, so a Desktop or other public client cannot be
used.

**Authorized redirect URI:**

| Where the app runs | Redirect URI |
| --- | --- |
| Locally | `http://127.0.0.1:7932/oauth/callback` |
| On BTP | `https://<your-approuter-host>/oauth/callback` |

Google permits plain `http` for loopback, so the local one is valid.

Cloud Console will offer to download the client as
`client_secret_<id>.apps.googleusercontent.com.json`. **Do not leave that file
in the repo.** It is gitignored here, but you only need to copy two values out
of it.

## 2. The code change (done)

`create_mcp_server` used to append `/mcp` to any URL not already ending in it,
so any server with a versioned endpoint was unreachable. Fixed in commit
`09e5a5b`: `/mcp` is appended only when the URL has no path of its own, and
`normalize_mcp_url` now delegates to the same function so the endpoint and the
token-storage key cannot drift apart.

Nothing to do — noted because a guide written before that fix would have you
chasing a 404.

## 3. Allow the MCP host

Authenticated servers (anything other than `auth_mode=none`) are restricted to
BTP hosts by default, so saving any other host fails with:

```
url must be a BTP-hosted URL (*.hana.ondemand.com).
Set MCP_URL_ALLOWLIST to override, or set auth_mode=none for public MCP servers.
```

Set `MCP_URL_ALLOWLIST` to a comma-separated list of allowed URL prefixes
(`agents/admin.py:164-183`). Locally, in `.env`:

```
MCP_URL_ALLOWLIST=https://your-mcp-host,https://<your-btp-mcp-host>
```

On BTP, set the same value on `MCP_URL_ALLOWLIST` in `mta.yaml` and redeploy.

**Setting this replaces the BTP rule, it does not extend it.** A list with only
the new host will start rejecting your existing BTP MCP servers next time you
save one. List every authenticated host you use. Public servers
(`auth_mode=none`) skip this check entirely.

The value is read from the environment, so **restart the app** after editing
`.env`.

## 4. Register the server on an agent

In **/admin → Agents**, add an MCP server row:

| Field | Value |
| --- | --- |
| URL | your MCP server's endpoint |
| Auth mode | `oauth2` |
| DCR checkbox | **unchecked** — Google does not support Dynamic Client Registration |
| Client ID / Secret | from step 1.4 (the secret field is a password input; the API never returns it) |
| Authorize URL | `https://accounts.google.com/o/oauth2/v2/auth?access_type=offline&prompt=consent` |
| Token URL | `https://oauth2.googleapis.com/token` |
| Scope | `https://www.googleapis.com/auth/gmail.modify` |

**The query string on the Authorize URL is load-bearing.** `begin_authorization`
(`agents/oauth2.py:511`) builds a fixed parameter set with no hook for extras,
but appends to the configured URL with `&` when it already has a query string.
Without `access_type=offline` Google returns no refresh token, the app stores
`None`, and the agent stops working about an hour after you connect — with an
auth error that does not point back here.

`gmail.modify` alone covers reading, drafting and relabelling. Do not use
`https://mail.google.com/` — that is full access including permanent delete and
send, and nothing here needs it.

Also set the agent's **Run-as identity** (`run_as_principal`): scheduled runs
have no logged-in user and use this principal's stored token.

## 5. The MCP server itself — NOT YET VALIDATED

Google's hosted server is ruled out (see Status). The remaining option is to run
one. From [`workspace-mcp`](https://workspacemcp.com/docs)'s documentation:

- `uvx workspace-mcp --tool-tier core`
- `--transport streamable-http` for OAuth 2.1 support
- `MCP_ENABLE_OAUTH21=true`
- tools include `draft_gmail_message` and `modify_gmail_message_labels`

Deploy it somewhere this app can reach — beside the other MCP servers in the CF
space is the obvious spot — then register it per section 4 and add its host to
`MCP_URL_ALLOWLIST`.

**None of this has been run.** Treat the above as a starting point, not a
recipe, and expect the tool names in section 6's prompt to need adjusting to
whatever the server actually exposes.

## 6. Give the agent its prompt

Label the mails you want handled — `agent` below. Then set the **Run prompt**
(the field is a textarea, so multi-line content pastes fine):

```
Handle the emails waiting for me.

1. Search for messages with the query: label:agent
2. If there are none, say so in one line and stop.
3. For each thread, in order:
   a. Read it.
   b. Write a reply in my voice: brief, direct, no filler. Answer what was
      actually asked. If you cannot answer without information I have not
      given you, draft a reply that asks for exactly that, and say so.
   c. Create a draft reply to the last message in the thread.
   d. Remove the "agent" label and add "agent-drafted".

Never send anything. Drafts only.

Report a markdown table with the columns: From | Subject | What the draft says |
Status. One row per thread. If a step failed for a thread, say which step and
why in the Status column rather than dropping the row.
```

- **Step (d) is the idempotency mechanism.** Without it every run re-drafts
  every mail.
- **"Never send anything"** costs nothing and removes any doubt.
- **Keep failures in the table.** A row that quietly disappears is how a mail
  ends up never handled without you noticing.

## 7. Schedule it

Point a BTP Job Scheduling Service job at `POST /api/agents/<slug>/run` every
10–15 minutes. See
`docs/superpowers/specs/2026-08-19-scheduled-agent-runs-design.md` for the
wiring. Use the **Run now** button first.

## Appendix: what was ruled out

Recorded so nobody repeats it. Against Google's hosted server, with a real
personal Gmail account:

| Hypothesis | Verdict |
| --- | --- |
| `gmailmcp.googleapis.com` not enabled | Wrong — `gcloud services list --enabled` showed both APIs |
| Missing scope | Eliminated — still 403 with `https://mail.google.com/` full access |
| Wrong identity / connected as the wrong principal | Eliminated — token was stored under the agent's `run_as_principal` |
| No refresh token | Eliminated — refresh token present, refresh returned 200 |
| Missing RFC 8707 resource indicator | Eliminated as a fix — adding `resource=` to the authorize URL left `aud` as the client ID; Google ignored it |
| The `agent` label did not exist | Eliminated — found in the label list |

The control that makes this conclusive: **the same token returns 200 from
`https://gmail.googleapis.com/gmail/v1/users/me/profile`** and lists the
mailbox's labels. Authentication succeeds; authorization is refused by the MCP
service specifically.

Note that the metadata is published **per tool**, not at the server root — a
bare `/.well-known/oauth-protected-resource` returns 404, while
`/.well-known/oauth-protected-resource/search_threads` returns the document
declaring `"resource": "https://gmailmcp.googleapis.com/mcp"`. Missing that cost
a round of wrong conclusions.

### The diagnostic recipe

Reusable for any `auth_mode=oauth2` MCP server that misbehaves. Read the stored
token from the app's own store and probe directly:

```python
import sqlite3, httpx
c = sqlite3.connect('agents_registry.db')
tok, typ, scope = c.execute(
    'select access_token, token_type, scope from mcp_oauth_tokens '
    'where server_key like "%YOUR_HOST%"').fetchone()
print('granted:', scope)

# Does the underlying API accept it? (isolates the MCP layer)
print(httpx.get('https://gmail.googleapis.com/gmail/v1/users/me/profile',
                headers={'Authorization': f'{typ} {tok}'}).status_code)

# What does the MCP server say, with and without auth?
body = {'jsonrpc':'2.0','id':1,'method':'tools/call',
        'params':{'name':'TOOL','arguments':{}}}
r = httpx.post(URL, json=body, headers={'Authorization': f'{typ} {tok}',
     'Accept':'application/json, text/event-stream'})
print(r.status_code, r.reason_phrase, r.text[:200])
```

**401 vs 403 is the key signal.** 401 means it did not accept your credential;
403 means it accepted it and refused anyway — which points at the client or the
service, never at your scopes. Send no `Authorization` header at all to read the
`WWW-Authenticate` response, which names the metadata document.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `403 "The caller does not have permission"` from `gmailmcp.googleapis.com` | Expected — see Status. Not fixable from this end |
| `404` at connect, `McpError: Session terminated` | Pre-`09e5a5b` URL rewriting; update the app |
| `url must be a BTP-hosted URL` | `MCP_URL_ALLOWLIST` unset — section 3 |
| Works for an hour, then auth failures on scheduled runs | No refresh token; `access_type=offline` missing from the Authorize URL. Fix and reconnect |
| `KeyError: 'client_secret'` when saving | Desktop/public OAuth client; create a **Web application** client |
| Interactive chat works, scheduled runs say "no usable credential" | Connected as yourself instead of the agent's `run_as_principal` |
| `RuntimeError: PUBLIC_BASE_URL must be set for API-triggered runs` | Set `PUBLIC_BASE_URL` (locally `http://127.0.0.1:7932`) |
| `ModelHTTPError ... context_length_exceeded` | A tool returned a huge payload. Narrow the search (`label:agent newer_than:7d`) and cap threads per run |
| Every run re-drafts the same mails | Step (d) of the prompt is being skipped, or the label name does not match |

## Security notes

- Scope is deliberately `gmail.modify`, not `https://mail.google.com/`. It
  cannot permanently delete mail, and with no send tool the worst a misbehaving
  prompt can do is create drafts and move labels.
- If you ever grant full access while debugging, revoke it afterwards at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
  Narrowing the configured scope only affects the *next* connect; the token
  already issued keeps what it was granted.
- Email content is untrusted input. A mail body containing instructions is
  **data, not a command**. Keep the agent's instructions explicit that it drafts
  replies and never acts on requests found inside a message.
- A self-hosted MCP server holds a token to your mailbox. Prefer running it in
  your own landscape over a third-party host.

## Sources

- [Configure the Gmail MCP server](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server) — Google for Developers
- [Google Workspace MCP](https://workspacemcp.com/docs) — the self-hosted candidate
- [Manage App Audience](https://support.google.com/cloud/answer/15549945?hl=en) / [OAuth App Branding](https://support.google.com/cloud/answer/15549049?hl=en) — Cloud Console Help
