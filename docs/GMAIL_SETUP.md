# Handling email with an agent (Gmail)

Move a mail into a Gmail label, and a scheduled agent reads it, writes a draft
reply, and takes the label off again.

**Working since 2026-08-24, over the Gmail REST API.** Google's hosted MCP
server is a dead end for a self-registered OAuth client (the whole Status
section below is the evidence), so the five tools this needs are served
in-process by `agents/gmail_tools.py`. Sections 1 and 3-7 still apply -- the
same Google Cloud project, the same OAuth client, the same agent config -- only
the server URL changes, to `builtin:gmail`. If you are setting this up fresh,
read [section 5](#5-the-built-in-gmail-toolset) and skip the Status section
entirely.

## Status (2026-08-24, second pass)

**Google's hosted Gmail MCP server does not work with a self-registered OAuth
client.** `https://gmailmcp.googleapis.com/mcp/v1` authenticates a token issued
by your own OAuth client and then refuses to authorize it:

```
403  "The caller does not have permission"
```

This was tested to exhaustion against a real personal Gmail account. See
[Appendix: what was ruled out](#appendix-what-was-ruled-out) — the short version
is that scope, identity, API enablement and audience were each eliminated, and
the same token reads the mailbox fine through `gmail.googleapis.com`.

Google's documentation shows worked examples only for Antigravity and Claude,
but it does **not** describe an allowlist, an approval process or a verification
requirement — it presents a self-registered OAuth client as the normal path and
says "many AI applications have ways to connect to a remote MCP server". So the
cause of the refusal remains genuinely unexplained; an undocumented client gate
is a hypothesis that fits the behaviour, not something Google states.

**The route that should work is a self-hosted MCP server** calling the Gmail API
directly, since a token from your own client demonstrably works against that
API. [`workspace-mcp`](https://workspacemcp.com/docs) (MIT, `--transport
streamable-http`, OAuth 2.1, with `draft_gmail_message` and
`modify_gmail_message_labels`) is the obvious candidate. **That path has not been
validated yet** — everything in section 5 is from its documentation, not from a
working deployment here.

Sections 1–4 are validated and worth keeping either way: the OAuth client you
create is the same one a self-hosted server will use.

### Validated 2026-08-24: the read + draft loop, and a query-syntax trap

The tool sequence this design depends on was exercised end to end against the
same mailbox, through Claude Code's own Gmail connector. That connector reaches
a Gmail MCP service that answers `tools/call` normally.

**This does not unblock the hosted server.** The connector uses Anthropic's own
credential, which a BTP-hosted agent cannot borrow, and there is no way to see
from the client which endpoint it talks to. What it does confirm is that the
403 above is about *which client is asking*, not about anything you configured
— and it settles the tool behaviour the prompt in section 6 relies on:

| Step | Tool | Result |
| --- | --- | --- |
| Find labelled mail | `search_threads` `label:agent` | works — **by display name only**, see below |
| Read the mail | `get_thread` `PLAIN_TEXT` | full body and signature, clean |
| Draft the reply | `create_draft` + `replyToMessageId` | draft created on the right thread |
| Relabel | `label_thread` / `unlabel_thread` | refused: `Request had insufficient authentication scopes` |

**The trap: `label:` wants the display name, not the label ID.** A query built
from an ID returns zero results and no error — indistinguishable from an empty
label. This contradicts the tool's own description, which states it accepts IDs
and not display names. Verified both ways against a label with 240 live threads:

```
label:Label_5781717798834007196   ->  {}          zero results, no error
label:Partners/Amista             ->  201 threads
```

Nested labels take the full path (`Partners/Amista`). Names containing spaces
need quoting (`label:"TE BETALEN"`).

**The app's own client was then run against Google's hosted server** —
`create_mcp_server` → `PerUserOAuth2Auth`, the real code path, stored config
untouched. The boundary is sharp:

| Stage | Result |
| --- | --- |
| Token refresh (stored token had expired) | works |
| `initialize`, `tools/list` | works — all 22 tools listed, including the four this design needs |
| `tools/call` — `list_labels`, `search_threads`, `get_thread` | all refused: `The caller does not have permission` |

Note the refusal arrives as **HTTP 200 with a JSON-RPC `isError` body**, not an
HTTP 403; the 403 is what the raw transport returns. Anything that only checks
the HTTP status will read this as success.

The relabel refusal is a property of that particular connector's scope grant,
not of Gmail — a self-hosted server holding `gmail.modify` can relabel. But
since a read-plus-compose deployment is a plausible place to end up, the
idempotency fallback under [How it works](#how-it-works) is worth knowing.

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

**If the server cannot write labels**, there is a fallback that needs no write
scope at all: `list_drafts` returns a `threadId` on every draft, so the agent
lists existing drafts first and skips any thread that already has one. Verified
2026-08-24. Note the check has to go through `list_drafts` — `get_thread` does
**not** include the draft among the thread's messages, so a thread that has
already been drafted looks untouched from the thread side.

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
| URL | `builtin:gmail` (see [section 5](#5-the-built-in-gmail-toolset)) |
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

## 5. The built-in Gmail toolset

`builtin:gmail` is not a URL. `agents/registry.py` recognises the scheme and
attaches `agents/gmail_tools.py` in-process instead of opening an MCP
connection. Nothing is deployed and nothing is listening -- the tools call
`https://gmail.googleapis.com` directly, with the same per-user token the OAuth2
MCP servers use, through the same `PerUserOAuth2Auth`. Sign-in, refresh and the
"connect this agent" link all behave exactly as they do for a real MCP server.

Five tools, and deliberately no sixth:

| Tool | Does |
| --- | --- |
| `search_threads(query, max_results)` | Gmail search; metadata only, capped at 50 threads |
| `get_thread(thread_id, max_chars)` | every message as plain text, per-message cap |
| `create_draft(thread_id, body)` | threaded reply saved as a draft |
| `list_labels()` | `{display name: label id}` |
| `modify_labels(thread_id, add, remove)` | add/remove on a whole thread, by name or id |

**There is no send tool.** "Drafts only" is then a property of the toolset
rather than a line in a prompt a model can talk itself out of.

Two details that cost time if you meet them cold:

- **Search matches labels by display name** (`label:agent`), but **`modify_labels`
  resolves names to ids** for you, because the REST API modifies by id. An
  unknown label raises rather than being skipped -- silently dropping a removal
  would leave the mail labelled and every later run would redo the thread.
- **Tool names are prefixed when an agent binds more than one server.** With
  three servers attached the model sees `gmail_search_threads`, not
  `search_threads`, and the run prompt in section 6 has to match. Check the
  actual names before blaming the model for not calling a tool.

### Switching an existing agent over

Replace the `https://gmailmcp.googleapis.com/mcp/v1` row with `builtin:gmail`,
keeping the same oauth block. Tokens are stored per server key, so the existing
grant does not carry over: the agent will report "no-token" until you reconnect
once through the sign-in link. Nothing else changes.

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

- **Use the label's display name in the query, never its ID.** `label:agent`
  finds mail; `label:Label_8458289880304789230` silently returns nothing. See
  Status.
- **Step (d) is the idempotency mechanism.** Without it every run re-drafts
  every mail. If the deployed server has no label-write scope, replace (d) with
  a check at the top of step 3: list existing drafts and skip any thread that
  already has one.
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
| Scope too *broad* — restricted scopes refused from an unverified app | Eliminated 2026-08-24 — refreshed down to exactly the two documented scopes (`gmail.readonly` + `gmail.compose`, per Google's setup guide). Google honoured the narrowing; the server refused all the same |
| Something in this app's MCP client | Eliminated 2026-08-24 — `tools/list` succeeds through the app and returns all 22 tools; only `tools/call` is refused |
| A stale or badly-minted token | Eliminated 2026-08-24 — a fresh grant taken through the app's own `/oauth/login` → consent → callback is refused identically |
| Workspace admin policy blocking the app | Eliminated 2026-08-24 — services all Unrestricted, no configured-app restriction, and the OAuth audit log records **zero** denials |
| Publishing status / External user type | Eliminated 2026-08-24 — switched the app to **Internal**, reconnected for a fresh grant, still refused |
| Wrong or cross-project API enablement | Eliminated 2026-08-24 — both APIs enabled in `btp-ai-gmail`, whose project number matches the OAuth client id |

### The Workspace and Cloud Console sweep (2026-08-24)

The mailbox is a **Google Workspace** account (`lemaire.tech`, MX
`aspmx.l.google.com`), not a personal one — so the admin-side controls were
checked too. All clean:

| Checked | Finding |
| --- | --- |
| Security → API controls → App access control | All 18 Google services **Unrestricted**, Gmail included; **Configured apps empty** |
| Agents → Agent access management | Empty; its banner defers non-Gemini connections to app access control |
| Reporting → **OAuth log events**, 7 days | 18 events, **all Grant/Revoke — no denial of any kind**. The client's two grants are recorded and nothing blocked them |
| Cloud project | `btp-ai-gmail`, org parent present; **both APIs enabled in that same project**; client id prefix matches the project number |
| Verification centre | "Verification is not required since your app is configured with a testing publishing status" — nothing pending or rejected |

Then the publishing-status hypothesis was tested directly: the OAuth app was
switched from **External / Testing** to **Internal** (possible because the
project sits under the Workspace org), reconnected through the app for a fresh
grant, and re-probed. **Still refused.** Eliminated.

### Where this leaves it

Everything a customer can configure has now been checked and is correct: the
app's code, its OAuth implementation, the token, the scopes, the Cloud project,
the enabled APIs, the Workspace policy, and the publishing status. `tools/list`
succeeds; every `tools/call` is refused.

The one remaining observable difference is that clients known to work with
Google's connectors carry Google's **verification badge** and this one does not
— correlation, not a demonstrated cause. Chasing it means Google's OAuth
verification review, and Gmail's scopes are all *restricted*, so that is the
expensive tier (privacy policy, verified domain, demo video, typically a CASA
security assessment; weeks to months). Note also that **an Internal app cannot
be verified at all** — verification applies to External apps — so the two
remaining levers are mutually exclusive.

**Recommendation: stop here and self-host.** The Gmail REST API accepts this
exact token (200 from `users/me/profile`), only four tools are needed
(`search_threads`, `get_thread`, `create_draft`, `list_drafts`), and that path
is bounded work rather than an open-ended bet on an unproven hypothesis.

One thing worth keeping regardless of which way you go: leave the app
**Internal**. Testing mode expires refresh tokens after 7 days, which would
break a scheduled agent every week; Internal does not.

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
| `search_threads` returns `{}` for a label you can see in `list_labels` | The query used the label **ID**. Use the display name: `label:agent`, not `label:Label_8458…` |
| `Request had insufficient authentication scopes` on a label write | The token has read/compose but not `gmail.modify`. Reconnect with the scope from 1.3, or use the `list_drafts` idempotency fallback |
| `OAuthAuthorizationRequired: no-token` after switching to `builtin:gmail` | Expected once. Tokens are keyed by server key; reconnect through the sign-in link |
| The agent never calls a Gmail tool | Tool names are prefixed per server. With several servers attached it is `gmail_search_threads`, not `search_threads` |
| `unknown label 'x'; call list_labels for valid names` | The label does not exist in the mailbox. Create it in Gmail or drop it from the prompt — `modify_labels` refuses rather than silently skipping |

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
