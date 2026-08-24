# Handling email with an agent (Outlook / Microsoft 365)

Move a mail into a subfolder of the Inbox, and a scheduled agent reads it,
writes a draft reply, and moves it out again.

The Gmail equivalent is `docs/GMAIL_SETUP.md`. Read that one first if you want
the reasoning; this document is the Microsoft variant and stands on its own.

## Status

**Nothing is built yet.** This is the setup and the go/no-go probe. Write the
toolset only once the probe passes — the risk here is your tenant's policy, not
the code, and it is answerable in minutes.

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

## 5. The probe — run this before writing any code

`scripts/probe_outlook.py` does one delegated sign-in and reports which gates
you cleared. It talks to Entra and Graph only, touches none of this app's code
or database, and creates nothing in the mailbox.

```bash
python scripts/probe_outlook.py \
    --tenant  <TENANT_ID> \
    --client  <CLIENT_ID> \
    --secret  <CLIENT_SECRET> \
    --folder  "agent"          # the Inbox subfolder, optional
```

It opens a browser, waits on `http://localhost:7932/oauth/callback`, then
prints a verdict. What each outcome means:

| Result | Meaning |
| --- | --- |
| refresh token present, folder found | All three gates clear. Build it |
| token but **no refresh token** | `offline_access` missing from step 3 |
| `AADSTS65001` / `AADSTS90094` | Admin consent required — ask IT to grant it |
| `AADSTS50011` | Redirect URI mismatch — see the localhost note in step 1 |
| `AADSTS7000218` | The app was registered as a public client; it must be **Web** |
| `AADSTS53003` | Conditional Access blocked the sign-in. This is the one to escalate |

## 6. The Inbox subfolder

Create a subfolder under Inbox in Outlook — `agent` below, matching the Gmail
setup — plus wherever handled mail should land. Moving to a second folder such
as `agent-done` keeps an audit trail; moving back to the Inbox does not.

The probe resolves the folder by display name and prints its id. Folder ids are
opaque and stable, so the id goes in the agent config and the display name is
never used at runtime.

## 7. What gets built afterwards

A sibling of `agents/gmail_tools.py`, attached the same way — `agents/registry.py`
already branches on the `builtin:` scheme, so no framework change is needed.

| Tool | Graph call |
| --- | --- |
| `list_pending(limit)` | `GET /me/mailFolders/{id}/messages` |
| `get_message(id)` | `GET /me/messages/{id}` |
| `create_reply_draft(id, body)` | `POST /me/messages/{id}/createReply`, then `PATCH` the body |
| `move_message(id, folder)` | `POST /me/messages/{id}/move` |

Four tools rather than Gmail's five, and no send tool. `createReply` builds the
threaded draft itself, so none of the MIME assembly and `In-Reply-To` handling
in the Gmail version is needed.

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
