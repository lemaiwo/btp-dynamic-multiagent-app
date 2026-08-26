# Handling email with an agent (Outlook / Microsoft 365)

Move a mail into a subfolder of the Inbox, and a scheduled agent reads it,
writes a draft reply, and moves it out again.

The Gmail equivalent is `docs/GMAIL_SETUP.md`. Read that one first if you want
the reasoning; this document is the Microsoft variant and stands on its own.

## Status

**The toolset is built and unit-tested; it has never touched a real mailbox.**
`agents/outlook_tools.py` exists with all its tools, 39 assertions against a
mocked transport, app-only auth (`agents/client_credentials.py`, 35 more
assertions), and the registry wiring done. What is missing is a tenant to run
it against, so nothing here has been verified end to end the way the Gmail path
was.

Two routes exist, and which one applies depends on whether a human can sign in
to the target mailbox:

* **delegated** (sections 1-3, 4) -- a person signs in; the agent acts as them.
* **app-only** (section 3b) -- the agent authenticates as itself. The only
  option for a shared or service mailbox with no interactive login.

Run the probe in section 5 first. If it passes, connect the agent and the tools
should work; if it fails, the code is fine and the answer is a conversation with
IT, not a change here.

Microsoft's own hosted Outlook MCP server (Work IQ Mail, under Agent 365) is not
a route for this app: preview only, requires a Microsoft 365 Copilot licence,
requires an IT admin to register an enterprise app, and has no support for
personal accounts. Same shape of gate that ruled out Google's hosted Gmail MCP
server. So the plan is the same one that worked for Gmail: call the vendor's
REST API — Microsoft Graph — from an in-process toolset.

## What has to be true

Three tenant policies can each block this. Section 5's probe answers all three
at once, which is why it comes before any code.

| Gate | Where it bites |
| --- | --- |
| App registration allowed for non-admins | You cannot create the app at all (step 1) |
| User consent allowed for `Mail.ReadWrite` | Consent screen says an admin must approve |
| Conditional Access | Sign-in blocked even when consent is fine |

None of these are fixable in code. All three have a person you can ask, which
is the difference from Google's verification wall.

## How it will work

```
BTP Job Scheduler  --cron-->  POST /api/agents/<slug>/run
                                     |
                              agent runs its prompt
                                     |
     list(folder) -> read -> create reply draft -> move out of folder
                                     |
                        message left the folder = handled
```

Moving the message is the idempotency mechanism, and it is a better one than
Gmail's label removal: the message physically leaves the queue in a single
atomic call, and there is no search query to get wrong.

## 1. Register the application

**Where:** [entra.microsoft.com](https://entra.microsoft.com) → **Identity** →
**Applications** → **App registrations** → **New registration**. (Microsoft
moves these labels around; if the path differs, search for "App registrations".)

| Field | Value |
| --- | --- |
| Name | `btp-outlook-integration` — appears on the consent screen |
| Supported account types | **Single tenant** (accounts in this org only) |
| Redirect URI platform | **Web** |
| Redirect URI | `http://localhost:7932/oauth/callback` |

**Use `localhost`, not `127.0.0.1`.** Entra rejects plain `http` redirect URIs
except for the loopback host, and it treats the two spellings as different
strings. Then set `PUBLIC_BASE_URL=http://localhost:7932` in `.env`, or the
callback the app builds will not match what you registered and you get
`AADSTS50011`.

For a deployed instance, add a second redirect URI: your approuter URL plus
`/oauth/callback`, over https.

From the app's **Overview** page, note the **Application (client) ID** and the
**Directory (tenant) ID**. Both go into the agent config.

**If "New registration" is greyed out or missing**, the tenant does not let
users register apps. Stop here and ask IT to create it with the values above,
then have them send you the client ID, tenant ID and a secret from step 2.

## 2. Create a client secret

**Certificates & secrets** → **Client secrets** → **New client secret**.

Copy the **Value** immediately — Entra shows it once and displays only the ID
afterwards. Note the expiry: tenant policy often caps it at 6, 12 or 24 months,
and this app has nothing that warns you before a secret dies.

## 3. Add the Graph permissions

**API permissions** → **Add a permission** → **Microsoft Graph** →
**Delegated permissions**:

| Permission | Why |
| --- | --- |
| `Mail.ReadWrite` | read the folder, create drafts, move messages |
| `offline_access` | refresh token |

Add nothing else. In particular **do not add `Mail.Send`** — the toolset will
have no send tool, and not holding the permission makes "drafts only" true at
the token level rather than only in the prompt.

`offline_access` is the one people miss. It is Microsoft's analogue of Google's
`access_type=offline`: without it you get an access token, everything works for
about an hour, and then scheduled runs start failing with an auth error that
does not point back here.

**If a "Grant admin consent for &lt;tenant&gt;" button is present and you can
click it**, do — it settles gate 2 in advance. If it is greyed out you are not
an admin, which is fine: the probe in section 5 will tell you whether user
consent alone is enough.

## 3b. The app-only route (for a mailbox nobody signs into)

Everything above describes *delegated* access: a person signs in and the agent
acts as them. That is impossible for a shared or service mailbox with no
interactive login, which is the common case for an automated inbox.

App-only (`app_only`) solves it. The agent authenticates as itself,
so there is no sign-in, no stored user token, and nothing that breaks when
someone leaves the company. Scheduled runs need no human at all.

**What it needs, and only an admin can give it:**

| Permission type | Permission | Why |
| --- | --- | --- |
| **Application** | `Mail.ReadWrite` | read the folder, draft replies, move messages |

Application permissions always require admin consent — no user can self-serve
them. Note the type: a *delegated* `Mail.ReadWrite` will not work here, and the
two look identical in the portal's permission list.

**Do not grant `Mail.Send`.** An application `Mail.Send` lets the registration
send as any mailbox it can reach. This toolset has no send tool unless one is
switched on deliberately (below), but the permission is worth refusing at the
source.

**Scope the app.** Application `Mail.ReadWrite` reaches *every mailbox in the
tenant* by default. An Exchange admin narrows it with an Application Access
Policy:

```powershell
New-ApplicationAccessPolicy -AppId <CLIENT_ID> `
    -PolicyScopeGroupId agent-mailboxes@example.com `
    -AccessRight RestrictAccess `
    -Description "Limit the agent to the mailboxes in this group"
```

Without it the blast radius of a leaked secret is the whole tenant's mail.

**Check what you actually have** before configuring anything:

```bash
python scripts/probe_outlook.py --app-only --mailbox service.mailbox@example.com
```

No browser and no user — it can be run by anyone holding the credentials. It
prints the token's `roles` claim, which is the only reliable way to see which
application permissions were consented to: Entra issues a token whether or not
any were, and the difference otherwise surfaces as a 403 several calls later.

### On sending

`send_reply` exists, and is off unless a server's config sets `allow_send`.

The intended output is a draft that a human approves. The tool exists only
because a tenant may grant `Mail.Send` without `Mail.ReadWrite`, leaving an app
able to send but not to draft — and holding the permission is deliberately not
enough to switch the tool on.

The reason for the separation: this toolset reads mail written by strangers and
feeds it to a language model, so a message body is attacker-controlled text.
With drafting, a prompt injection wastes somebody's time. With sending, it
reaches the outside world signed as the mailbox owner. Leave it off unless you
have a specific reason, and turn it off again once `Mail.ReadWrite` lands.

## 4. Agent configuration (once the probe passes)

In **/admin → Agents**, on the agent's MCP server list:

| Field | Value |
| --- | --- |
| URL | `builtin:outlook` |
| Auth mode | `oauth2` |
| DCR checkbox | **unchecked** |
| Client ID | from step 1 |
| Client secret | from step 2 |
| Authorize URL | `https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/authorize` |
| Token URL | `https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token` |
| Scope | `https://graph.microsoft.com/Mail.ReadWrite offline_access` |

Unlike Google, no query string is needed on the Authorize URL — Microsoft
carries the offline request in the scope instead.

Also set the agent's **Run-as identity** (`run_as_principal`): scheduled runs
have no logged-in user and use that principal's stored token.

### App-only variant

For the `app_only` route from section 3b, the same server row instead
reads:

| Field | Value |
| --- | --- |
| URL | `builtin:outlook` |
| Auth mode | `App-only (client credentials)` |
| Client ID / Secret | from steps 1–2 |
| Token URL | `https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token` |
| Scope | `https://graph.microsoft.com/.default` |
| Mailbox | the target address, e.g. `service.mailbox@example.com` |
| Look back | `2d` — how far back a listing may reach; blank means no limit |
| Sending | leave unchecked |

Three differences from the delegated row, each following from there being no
user: no Authorize URL (nobody visits a browser), a Mailbox (the token names
nobody, so the target cannot be inferred — the app refuses to start rather than
fall back to `/me`), and `.default` as the scope, which is the only form this
grant accepts.

### The look-back window

`lookback` bounds how far back `list_pending` will reach: `90m`, `5h`, `2d`,
`1w`, or a bare number meaning hours. Blank means no limit, which is the
original behaviour.

It matters most when the queue is not a folder that drains. Pointed at a busy
Inbox, an unfiltered listing returns the *oldest* mail in the mailbox — mail
that may be months stale and was never meant for triage — and the recent
message somebody actually wants answered never appears within the limit. A
window of `2d` fixes that without anyone having to tidy the folder first.

It is a **ceiling, not a default**. `list_pending` also takes a `lookback`
argument, so a prompt can ask for a narrower window, but the tighter of the two
always wins. A prompt cannot talk its way into reading more of the mailbox than
the configuration allows — the same reasoning as `allow_send`: capabilities are
bounded by config, not by wording a model may reinterpret.

A malformed value is rejected when the agent is saved, naming the field.
Defaulting it to "no filter" would silently hand the agent the whole mailbox,
which is the exact failure the setting exists to prevent.

**`run_as_principal` is not needed** app-only, and the credentials panel shows
the server as connected without anyone signing in. It is connected by
configuration.

## 5. The probe — run this before connecting the agent

`scripts/probe_outlook.py` does one delegated sign-in and reports which gates
you cleared. It talks to Entra and Graph only, touches none of this app's code
or database, and creates nothing in the mailbox.

Put the credentials in `.env` (gitignored) rather than on the command line:

```
OUTLOOK_TENANT_ID=<TENANT_ID>
OUTLOOK_CLIENT_ID=<CLIENT_ID>
OUTLOOK_CLIENT_SECRET=<CLIENT_SECRET>
OUTLOOK_FOLDER=agent            # the Inbox subfolder, optional
```

```bash
python scripts/probe_outlook.py
```

A secret passed as `--secret` ends up in shell history, in `ps` output, and in
the transcript of any agent asked to run the probe. The flags still exist and
override `.env` — useful for a one-off run against a second tenant — but `.env`
is the intended route. The probe prints the tenant and client ids it loaded and
a `sha256:` fingerprint of the secret: enough to tell one value from another
across runs without echoing any of it.

It opens a browser, waits on `http://localhost:7932/oauth/callback`, then
prints a verdict. What each outcome means:

| Result | Meaning |
| --- | --- |
| refresh token present, folder found | All three gates clear. Connect the agent |
| token but **no refresh token** | `offline_access` missing from step 3 |
| `AADSTS65001` / `AADSTS90094` | Admin consent required — ask IT to grant it |
| `AADSTS50011` | Redirect URI mismatch — see the localhost note in step 1 |
| `AADSTS7000218` | The app was registered as a public client; it must be **Web** |
| `AADSTS53003` | Conditional Access blocked the sign-in. This is the one to escalate |

## 6. The Inbox subfolder

Create a subfolder under Inbox in Outlook — `agent` below, matching the Gmail
setup — plus wherever handled mail should land. Moving to a second folder such
as `agent-done` keeps an audit trail; moving back to the Inbox does not.

The probe prints each subfolder's id, which is useful for debugging, but **no
folder id goes in the config**: the toolset resolves the display name at
runtime and caches it. So the run prompt names the folder (`agent`), and
renaming it in Outlook is all it takes to point the agent elsewhere.

## 7. The toolset (built, unverified)

`agents/outlook_tools.py`, a sibling of `agents/gmail_tools.py`, dispatched by
`agents/builtins.py`. No framework change was needed — `agents/registry.py`
already branches on the `builtin:` scheme.

| Tool | Graph call |
| --- | --- |
| `list_pending(limit)` | `GET /me/mailFolders/{id}/messages` |
| `get_message(id)` | `GET /me/messages/{id}` |
| `create_reply_draft(id, body)` | `POST /me/messages/{id}/createReply`, then `PATCH` the body |
| `send_reply(id, body)` | `POST /me/messages/{id}/reply` — only when `allow_send` is set |
| `move_message(id, folder)` | `POST /me/messages/{id}/move` |

`createReply` builds the threaded draft itself, so none of the MIME assembly and
`In-Reply-To` handling in the Gmail version is needed. Gmail has no `send_reply`
counterpart: its toolset registers no send tool at all.

Behaviour worth knowing before writing the run prompt:

- **`send_reply` bodies are converted to HTML before sending.** On the JSON
  path Graph builds the reply as HTML — the reply API's `Prefer: outlook.timezone`
  note says it creates the message "in HTML … based on the request body" — so a
  plain-text `comment` arrives with every newline collapsed as HTML whitespace
  and the answer reads as one run-on paragraph. `_text_to_html` escapes the
  model's text and turns blank lines into `<p>` and single newlines into `<br>`.
  The tool contract stays plain text: the model must not be asked for HTML, both
  because escaping belongs on this side and because `comment` and
  `message.body` cannot both be sent (Graph returns 400). `create_reply_draft`
  needs none of this — it PATCHes `contentType: text`.
- **`move_message` takes a folder display name**, so where handled mail goes is
  a prompt decision, not a config one: `agent-done` keeps an audit trail,
  `inbox` puts it back. Well-known names (`inbox`, `archive`, `deleteditems`)
  are passed to Graph as-is; anything else is resolved among the Inbox's
  subfolders.
- **Folders resolve by display name at runtime**, cached per toolset, so nothing
  needs a folder id in config. An unknown name raises and lists what it did
  find — "no mail waiting" and "wrong folder name" must not look alike.
- **`list_pending` returns oldest first** and never includes bodies; a folder
  listing carrying bodies would exhaust the context before the first reply.
- **Tool names are prefixed when an agent binds more than one server** —
  `outlook_list_pending`, not `list_pending`. Same trap as Gmail.

### Suggested run prompt

```
Handle the emails waiting for me.

1. Call outlook_list_pending with folder: agent
2. If there are none, say so in one line and stop.
3. For each message, in order:
   a. Call outlook_get_message to read it.
   b. Write a reply in my voice: brief, direct, no filler. Answer what was
      actually asked. If you cannot answer without information I have not
      given you, draft a reply that asks for exactly that, and say so.
   c. Call outlook_create_reply_draft with the reply.
   d. Call outlook_move_message to move it to agent-done.

Never send anything. Drafts only.

Report a markdown table with the columns: From | Subject | What the draft says |
Status. One row per message. If a step failed, say which step and why in the
Status column rather than dropping the row.
```

Step (d) is the idempotency mechanism: the message leaves the queue folder, so
the next run does not see it. Create `agent-done` in Outlook first — like
Gmail's labels, `move_message` refuses an unknown destination rather than
silently skipping.

## Security notes

- `Mail.ReadWrite` cannot send. Combined with no send tool, a misbehaving prompt
  can at worst write drafts and move messages between folders.
- The client secret grants access to a corporate mailbox. It belongs in the
  agent config (stored encrypted, never returned by the API), not in a file.
- Email content is untrusted input. A mail body containing instructions is
  **data, not a command** — keep the agent's prompt explicit that it drafts
  replies and never acts on requests found inside a message.
- Revoke at [myapps.microsoft.com](https://myapps.microsoft.com) or, as an
  admin, in Entra under the app's **Enterprise application** entry.

## Sources

- [Microsoft Graph mail API](https://learn.microsoft.com/graph/api/resources/mail-api-overview)
- [Register an app with the Microsoft identity platform](https://learn.microsoft.com/entra/identity-platform/quickstart-register-app)
- [OAuth 2.0 authorization code flow](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-auth-code-flow)
