# Connecting an agent to Slack (`builtin:slack`)

This guide covers everything needed to give an agent access to Slack:

- a Slack app for the agent to act as;
- a BTP destination that holds that app's token;
- the toolset entry in the admin UI.

The agent acts as a **Slack bot**, never as a person. It reads the channels the
bot has been invited to. It posts only if you turn on **Sending**, and then
only under the bot's own name.

> Status: `agents/slack_tools.py` is unit-tested against a mocked Slack API.
> It has not yet been run against a real workspace. Treat your first real run
> as the test, and use a test channel.

---

## 1. How it fits together

```
agent ──► builtin:slack ──► BTP destination service ──► https://slack.com/api
                                  (holds: Authorization: Bearer xoxb-…)
```

Slack has no "client credentials" sign-in. A Slack app gets a bot token
(`xoxb-…`) once, when it is installed in the workspace, and that token stays
valid until it is revoked.

The token is stored in a **BTP destination**, the same way the Jira connector
stores its credential:

- this app's database holds only the destination's *name*;
- rotating or revoking the token happens in the BTP cockpit and needs no
  redeploy.

| What | Where it is configured |
|---|---|
| Which workspace the bot is in, and what it is allowed to do | Slack app (scopes) |
| The bot token | BTP destination |
| Which channels this agent may use, how far back it reads, whether it may post | Admin UI, toolset config |

---

## 2. Slack: create the app

You need to be allowed to install apps in the workspace. On many workspaces,
installing needs an admin's approval.

### 2.1 Create it from a manifest

1. Go to <https://api.slack.com/apps>, then **Create New App** → **From an app manifest**.
2. Pick the workspace.
3. Paste this manifest (YAML) and create the app:

```yaml
display_information:
  name: BTP Agent
  description: Agent from the SAP BTP multi-agent app
features:
  bot_user:
    display_name: BTP Agent
    always_online: false
oauth_config:
  scopes:
    bot:
      - channels:read      # list public channels
      - channels:history   # read messages in public channels
      - groups:read        # list private channels the bot is in
      - groups:history     # read messages in those private channels
      - users:read         # show names instead of user ids
      - chat:write         # post — leave this out for a read-only bot
settings:
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

Adjust the scopes to what the agent really needs:

| Scope | Needed for | Can drop it if… |
|---|---|---|
| `channels:read`, `channels:history` | public channels | the agent only uses private channels |
| `groups:read`, `groups:history` | private channels | the agent only uses public channels |
| `users:read` | author names in messages | you are fine with user ids like `U04AB…` |
| `chat:write` | `post_message`, `reply_in_thread` | the agent only reads |

`chat:write` alone does not let the agent post. The **Sending** switch in the
admin UI (§4) also has to be on. Leaving the scope out makes the bot read-only
at Slack's end too.

**Keep `token_rotation_enabled: false`.** With rotation on, bot tokens expire
after 12 hours, and a token stored in a destination cannot be refreshed.

### 2.2 Install it and copy the token

1. In the app's settings, open **OAuth & Permissions**, then **Install to Workspace**. Approve it, or request approval from an admin.
2. Copy the **Bot User OAuth Token**, which starts with `xoxb-`.

Treat the token like a password. You will paste it once, into the destination
in §3.

If you change the scopes later, Slack asks you to **reinstall** the app, and
the new permissions only apply after that. Usually the token stays the same.
If Slack shows a new one, update the destination.

### 2.3 Invite the bot to its channels

The bot only sees channels it is a member of. In every channel the agent should
use, run:

```
/invite @BTP Agent
```

Private channels work the same way, as long as the `groups:*` scopes are
granted.

### 2.4 Note the channel names or ids (optional)

To pin the agent to specific channels (recommended, see §4), note each
channel's name without the `#`, or its id.

- To find the id, open the channel, click its name, and look at the bottom of the **About** tab (`C0…`).
- Ids survive a channel being renamed. Names are easier to read.

### 2.5 Keep the app internal

Leave the app installed in your own workspace only. Don't submit it to the Slack
Marketplace or distribute it to other workspaces.

Slack announced much stricter rate limits on `conversations.history` and
`conversations.replies` for apps distributed outside the Marketplace. As
announced, internal apps kept the normal limits, but check Slack's current
[rate-limit docs](https://api.slack.com/apis/rate-limits) if reads get
throttled (see §6).

---

## 3. BTP: create the destination

The app is already bound to a destination service instance (`agent-destination`
in `mta.yaml`), so you only need to add the destination itself.

1. In the BTP cockpit, open the **subaccount** where the app runs, then **Connectivity** → **Destinations** → **Create Destination**.
2. Fill it in:

| Field | Value |
|---|---|
| Name | `SLACK_BOT` (any name; you enter it in the admin UI) |
| Type | `HTTP` |
| URL | `https://slack.com/api` |
| Proxy Type | `Internet` |
| Authentication | `NoAuthentication` |

3. Under **Additional Properties**, click **New Property** and add:

| Property | Value |
|---|---|
| `URL.headers.Authorization` | `Bearer xoxb-…` (the token from §2.2, including the word `Bearer` and one space) |

4. Save.

A few notes:

- **Why NoAuthentication plus a header property:** BTP has no authentication
  type for "a fixed token issued by the target". A `URL.headers.<Name>` property
  is sent as that header on every call. `agents/destination.py` accepts a
  NoAuthentication destination only when it carries a
  `URL.headers.Authorization` property. Without one, the agent stops with an
  error naming the destination.
- **"Check Connection"** in the cockpit only shows that `slack.com` can be
  reached. It does not test the token. The first agent run in §5 does.
- **Who can see the token:** anyone who can view destinations in this
  subaccount can read it. Limit the destination-admin role accordingly.
- **Instance-level destinations** also work. The app looks the name up on the
  service instance first, then in the subaccount.

### Running the app locally

Outside Cloud Foundry, the app reads the destination service credentials from
environment variables instead of the service binding. Put these in `.env`:

```bash
cf service-key agent-destination <key-name>   # create one with cf create-service-key first
```

```
DESTINATION_CLIENT_ID=<clientid>
DESTINATION_CLIENT_SECRET=<clientsecret>
DESTINATION_URI=<uri>
DESTINATION_UAA_URL=<url>
```

---

## 4. Admin UI: add the toolset to an agent

1. Open the UI5 admin (`/ui5admin`), then the agent, then **Toolsets** → **Add toolset**.
2. Under **Toolset**, pick **Slack (bot, through a BTP destination)**. The auth mode is set to *BTP destination* automatically.
3. Fill in the fields:

| Field | Value |
|---|---|
| Destination | `SLACK_BOT`, the name from §3 |
| Channels | e.g. `support, general`, or channel ids. **Blank means every channel the bot is in.** |
| Look back | e.g. `2d`. The furthest back the agent may read. It can ask for less, never more. Blank means no limit. |
| Sending | Off: the agent can only read. On: the agent can also post and reply as the bot. Needs `chat:write`. |

4. Click **OK**, then **Save**. Saving rebuilds the agents, and the new tools are live on the next chat.

Recommendations:

- **Pin the channels.** The bot may be invited to new channels later. With the
  list pinned, the agent's reach doesn't grow when that happens.
- **Start with Sending off.** Turn it on once the read side behaves as you
  expect. Posts are visible immediately, and Slack has no draft state to review
  first.

### The tools the agent gets

| Tool | What it does | Slack method |
|---|---|---|
| `list_channels` | Lists the channels the agent may use, with each channel's purpose | `conversations.list` |
| `list_messages` | Recent top-level messages in a channel, oldest first | `conversations.history` |
| `get_thread` | A message with all its thread replies | `conversations.replies` |
| `post_message` | New message in a channel (Sending only) | `chat.postMessage` |
| `reply_in_thread` | Reply in a thread (Sending only) | `chat.postMessage` with `thread_ts` |

Join and leave notices, topic changes and other housekeeping messages are
filtered out.

Everything the agent posts is escaped. `<!channel>`, `<!here>` and
`<@U…>` mentions arrive as plain text and notify no one, even if the model
copies them from a message it read.

---

## 5. Test it

Start in the chat with a read-only question, in a test channel:

> List the Slack channels you can see, then summarise the last 10 messages in #support.

If Sending is on, then try:

> Reply in the thread of the latest message in #support with "Test reply from the agent".

Check in Slack that the reply appears under the bot's name, in the thread.

---

## 6. Troubleshooting

Slack returns most errors as `ok: false` plus a code. The agent passes that
code through as `<method>: <error>`.

| Error | Cause | Fix |
|---|---|---|
| `no channel 'x' available to this agent` | The channel is not in the pinned **Channels** list, or (with no list) the bot is not a member | Add it to the list, or `/invite` the bot |
| `not_in_channel` | Pinned channel, but the bot was never invited | `/invite @BTP Agent` in that channel |
| `missing_scope` | A scope from §2.1 is missing, e.g. `groups:history` for a private channel | Add the scope and **reinstall** the app |
| `invalid_auth` / `not_authed` | Wrong token, token revoked, or the header value lacks `Bearer ` | Fix `URL.headers.Authorization` in the destination |
| `token_expired` | Token rotation is on | Turn rotation off, reinstall, and put the new token in the destination |
| `destination 'SLACK_BOT' returned no authentication token … carries neither` | The additional property is missing or misspelled | The property name must be exactly `URL.headers.Authorization` |
| `destination 'SLACK_BOT' does not exist …` | Name typo, or the destination is in a different subaccount | Check the name in the toolset config against the cockpit |
| `no destination service binding found` | Running locally without the `DESTINATION_*` variables | See §3, *Running the app locally* |
| `rate limited by Slack; retry after Ns` | Too many calls in a short time | Lower `limit` or the look-back window; for scheduled runs, space them out |
| Authors show as `U04AB…` instead of names | `users:read` not granted | Add the scope and reinstall (optional) |

---

## 7. Not supported (yet)

- **Posting as individual users.** Slack's per-user sign-in returns its token
  in a different place from the other providers, so the app's existing
  sign-in code can't read it. Only the bot identity is supported.
- **Direct messages and group DMs** (the `im:*` and `mpim:*` scopes). Only
  channels are supported.
- **Skipping threads the bot already answered** on repeated scheduled runs, as
  `builtin:jira` does for issues. For now, use **Look back** to bound each run.
- **Reacting to messages as they arrive** (the Slack Events API). The agent
  reads when asked, or when a scheduled run starts.
