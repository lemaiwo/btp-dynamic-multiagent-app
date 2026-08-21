# Handling email with an agent (Gmail)

Move a mail into a Gmail label, and a scheduled agent reads it, writes a draft
reply, and takes the label off again.

## Status: what works today

**One code change is required first** (see step 2). Everything else is
configuration — no new code, no server to deploy, no third-party service
touching your mailbox.

Google publishes a first-party remote Gmail MCP server. Verified live on
2026-08-21: `https://gmailmcp.googleapis.com/mcp/v1` answers MCP
`protocolVersion 2025-06-18`, is stateless (no session header), and exposes 22
tools including the four this workflow needs — `search_threads`, `get_thread`,
`create_draft` and `update_message_labels`.

## How it works

There is no push, no webhook and no trigger subsystem. A scheduled run wakes up
every few minutes and the agent does the work itself through MCP tools:

```
BTP Job Scheduler  --cron-->  POST /api/agents/<slug>/run
                                     |
                              agent runs its prompt
                                     |
        search_threads(label) -> get_thread -> create_draft -> update_message_labels
                                     |
                              label removed = message handled
```

Removing the label is what makes it idempotent: a handled mail no longer
matches the search, so the next run skips it. `update_message_labels` adds and
removes atomically, so a message can be moved from `agent` to `agent-done` in a
single call if you prefer an audit trail over deletion.

## Prerequisites

- A Google account you control. A personal `@gmail.com` account is simplest —
  a Workspace account may need admin approval for the OAuth client and scopes.
- A Google Cloud project — step 1.1 creates one if you have none. No billing
  account needed.
- `gcloud` CLI, or the Cloud Console if you prefer clicking.
- The app running somewhere with a reachable OAuth callback. Locally that is
  `http://127.0.0.1:7932/oauth/callback`; on BTP it is your approuter URL plus
  `/oauth/callback`.

## 1. Google Cloud (you do this — it involves credentials)

### 1.1 Create or pick a project, and find its ID

In the [Cloud Console](https://console.cloud.google.com), open the project
picker in the top bar and either select an existing project or click
**New project**. Or from the CLI:

```bash
# Create one (the ID must be globally unique across all of Google Cloud)
gcloud projects create my-gmail-agent-2026 --name="Gmail agent"

# Or list the projects you already have
gcloud projects list
```

**The project ID is not the project name.** The name is a display label you
choose and can change; the ID is permanent, globally unique, and often gets a
number appended when your first choice is taken — so a project named
"Gmail agent" may well have the ID `gmail-agent-483920`. Every `--project` flag
below wants the **ID**.

Where to find it: the Cloud Console project picker shows the ID under each
name, and the project's **Settings → Project info** card lists it. From the
CLI:

```bash
gcloud projects list --format="table(projectId, name)"
```

Set it as your default so you can drop the `--project` flag entirely:

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud config get-value project      # confirm what is currently selected
```

**Billing is not required.** The Gmail API and the Gmail MCP API are free to
enable and have no usage charges at this scale, so you do not need a card on
the project.

### 1.2 Enable both APIs

```bash
gcloud services enable gmail.googleapis.com gmailmcp.googleapis.com \
  --project=YOUR_PROJECT_ID
```

`gmailmcp.googleapis.com` is the MCP server itself. Enabling only the Gmail API
is a common miss and produces an opaque failure at connect time.

### 1.3 OAuth consent screen (all in Google Cloud)

**Where:** [console.cloud.google.com](https://console.cloud.google.com) →
**APIs & Services** → **Google Auth Platform**.

If you are looking for a menu item called "OAuth consent screen", it no longer
exists — Google folded it into **Google Auth Platform**, split across four
tabs: *Branding*, *Audience*, *Data access* and *Clients*. On a fresh project
you get a **Get started** wizard instead of the tabs; fill that in and the tabs
appear afterwards. Nothing here happens in this app's admin page — that starts
at step 3.

Fill in the tabs:

**Branding** — App name (anything, e.g. `Gmail agent`), User support email
(your own address), Developer contact email (your own address). Save. Google
will not let you create a client until this is filled in.

**Audience** — User type **External**. Leave publishing status on **Testing**.
Then under **Test users** → **Add users**, add your own Gmail address.

That last step is not optional. In Testing mode, only addresses listed as test
users can complete consent; anyone else gets `access_denied` with no useful
explanation. Add the same address you intend to read mail from.

**Data access** — optional. You can list the three scopes from step 4 here, but
the app requests them at consent time and a test user can grant them either
way. Adding them makes the consent screen show exactly what is being asked for.

**Clients** — this is step 1.4 below. The new UI puts client creation on this
tab; the older path (**APIs & Services → Credentials → Create credentials**)
still works and creates the same thing.

**Expect an "unverified app" warning** the first time you consent in step 4.
That is normal for a project in Testing and does not mean anything is
misconfigured — click **Advanced**, then **Go to ‹app name› (unsafe)**. It says
unsafe because Google has not reviewed the app; the app in question is yours.

### 1.4 Create the OAuth client

**Where:** the **Clients** tab of Google Auth Platform → **Create client**
(or the older **APIs & Services → Credentials → Create credentials →
OAuth client ID** — same result).

Application type: **Web application**.

The client type matters. `Oauth2Config.from_spec` (`agents/oauth2.py:102`)
reads `oauth["client_secret"]` unconditionally and raises `KeyError` without
one, so a *Desktop* or other public client cannot be used here.

Add an **Authorized redirect URI**:

| Where the app runs | Redirect URI |
| --- | --- |
| Locally | `http://127.0.0.1:7932/oauth/callback` |
| On BTP | `https://<your-approuter-host>/oauth/callback` |

Google permits plain `http` for loopback addresses, so the local one is valid.
Add both if you intend to use both.

Keep the client ID and client secret. You will paste them into the admin UI
yourself in step 4 — the secret field is a password input and the value is
never returned by the API, only a `has_client_secret` flag.

## 2. The code change (required)

`create_mcp_server` appends `/mcp` to any MCP URL that does not already end in
it (`agents/shared.py:238`), and `normalize_mcp_url` repeats that rule to build
the token-storage key (`agents/oauth2.py:131`). Google's endpoint ends in
`/v1`, so it becomes `https://gmailmcp.googleapis.com/mcp/v1/mcp`, which 404s.

The fix is to append `/mcp` only when the URL has no path, in **both** places —
they must stay identical, or tokens get stored under a key the live connection
never looks up. All existing MCP URLs in this project end in `/mcp` and are
unaffected.

Until that lands, step 5 will fail with a 404 at connect time.

## 3. Allow the Gmail host

Before the server can be saved, its host has to be allowed. Authenticated
servers (anything other than `auth_mode=none`) are restricted to BTP hosts by
default, so saving the Gmail server first fails with:

```
url must be a BTP-hosted URL (*.hana.ondemand.com).
Set MCP_URL_ALLOWLIST to override, or set auth_mode=none for public MCP servers.
```

Set `MCP_URL_ALLOWLIST` to a comma-separated list of allowed URL prefixes
(`agents/admin.py:164-183`). Locally, in `.env`:

```
MCP_URL_ALLOWLIST=https://gmailmcp.googleapis.com,https://<your-btp-mcp-host>
```

On BTP, set the same value on the `MCP_URL_ALLOWLIST` property in `mta.yaml`
and redeploy.

**Setting this replaces the BTP rule, it does not extend it.** A list
containing only Google will start rejecting your existing BTP MCP servers the
next time you save one. List every authenticated host you use.

Public servers (`auth_mode=none`) are exempt from this check entirely, so
anything already registered that way is unaffected.

The value is read from the environment, so **restart the app** after changing
`.env`.

## 4. Register the MCP server on an agent

In **/admin → Agents**, create or edit an agent and add an MCP server row:

| Field | Value |
| --- | --- |
| URL | `https://gmailmcp.googleapis.com/mcp/v1` |
| Auth mode | `oauth2` |
| DCR checkbox | **unchecked** — Google does not support Dynamic Client Registration |
| Client ID | from step 1.4 |
| Client secret | from step 1.4 |
| Authorize URL | `https://accounts.google.com/o/oauth2/v2/auth?access_type=offline&prompt=consent` |
| Token URL | `https://oauth2.googleapis.com/token` |
| Scope | `https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.modify` |

Two of those are not obvious:

**The query string on the Authorize URL is load-bearing.** `begin_authorization`
(`agents/oauth2.py:511`) builds a fixed parameter set with no hook for extra
parameters, but it appends to the configured URL with `&` when that URL already
has a query string. Without `access_type=offline`, Google returns no refresh
token, the app stores `None`, and the agent stops working roughly an hour after
you connect — with an auth error that does not obviously point back here.
`prompt=consent` forces Google to re-issue a refresh token if you ever
reconnect.

**`gmail.modify` is beyond Google's documented scopes.** Their guide lists only
`gmail.readonly` and `gmail.compose`. Those cover reading and drafting but not
relabelling, and relabelling is what makes the loop idempotent.

Also set the agent's **Run-as identity** (`run_as_principal`) — scheduled runs
have no logged-in user and use this principal's stored token.

## 5. Connect

In the agent form, use the credentials panel to connect the Gmail server. You
will be redirected to Google, asked to consent, and returned to
`/oauth/callback`.

**Connect as the agent's run-as principal, not as yourself.** Tokens are stored
per `(principal, server)`. If you connect under your own identity, interactive
chat will work and every scheduled run will fail with "no usable credential".
This is the single most common way to get this wrong.

## 6. Give the agent its prompt

Label the mails you want handled with a Gmail label — `agent` below. Then set
the agent's **Run prompt**:

```
Handle the emails waiting for me.

1. Call search_threads with the query: label:agent
2. If there are none, say so in one line and stop.
3. For each thread, in order:
   a. Call get_thread to read it.
   b. Write a reply in my voice: brief, direct, no filler. Answer what was
      actually asked. If you cannot answer without information I have not
      given you, draft a reply that asks for exactly that, and say so.
   c. Call create_draft as a reply to the last message in the thread.
   d. Call update_message_labels to remove "agent" and add "agent-drafted".

Never send anything. Drafts only.

Report a markdown table with the columns: From | Subject | What the draft says |
Status. One row per thread. If a step failed for a thread, say which step and
why in the Status column rather than dropping the row.
```

Points that matter in that prompt:

- **Step (d) is the idempotency mechanism.** Without it, every run re-drafts
  every mail.
- **"Never send anything"** is worth stating outright. The server has no send
  tool, but the instruction costs nothing and removes any doubt.
- **The report table** is what you will actually read each morning. Ask for the
  failures to stay in it — a row that quietly disappears is how you end up not
  noticing a mail was never handled.

## 7. Schedule it

Point a BTP Job Scheduling Service job at `POST /api/agents/<slug>/run` on a
cron of every 10–15 minutes. See `docs/superpowers/specs/2026-08-19-scheduled-agent-runs-design.md`
for the scheduler wiring. The endpoint answers `202` immediately and the run
happens in the background.

Start with the agent's **Run now** button in the admin UI before automating it.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `404` at connect, or `McpError: Session terminated` | Step 2 not applied — the URL is being rewritten to `/mcp/v1/mcp` |
| Works for an hour, then auth failures on every scheduled run | No refresh token. `access_type=offline` missing from the Authorize URL. Reconnect after fixing it |
| `KeyError: 'client_secret'` when saving | The OAuth client is a Desktop/public client. Create a **Web application** client |
| Drafts work, relabelling fails | `gmail.modify` missing from the scope list. Add it and reconnect |
| Interactive chat works, scheduled runs say "no usable credential" | Connected as yourself instead of as the agent's `run_as_principal` |
| `RuntimeError: PUBLIC_BASE_URL must be set for API-triggered runs` | Set `PUBLIC_BASE_URL` (locally `http://127.0.0.1:7932`); on BTP it comes from `mta.yaml` |
| `ModelHTTPError ... context_length_exceeded` | A tool returned a very large payload. Narrow the search (`label:agent newer_than:7d`) and cap how many threads one run handles |
| Every run re-drafts the same mails | Step (d) of the prompt is being skipped, or the label name does not match |

## Security notes

- The MCP server is Google's own, so no third party holds a token to your
  mailbox. The token is minted for *your* OAuth client and stored in this app's
  `mcp_oauth_tokens` table.
- Scope is deliberately not `https://mail.google.com/`. The three scopes above
  cannot permanently delete mail, and there is no send tool — the worst a
  misbehaving prompt can do is create drafts and move labels.
- Email content is untrusted input. A mail body that contains instructions is
  *data*, not a command, and the agent should be prompted accordingly. Keep the
  agent's instructions explicit that it drafts replies and never acts on
  requests found inside a message.

## Sources

- [Configure the Gmail MCP server](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server) — Google for Developers
- [Google Workspace MCP](https://workspacemcp.com/docs) — self-hosted alternative (MIT), if you would rather not depend on Google's hosted server
