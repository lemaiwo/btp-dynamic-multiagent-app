# Jira via BTP Destination — Design

**Date:** 2026-08-25
**Status:** Approved for planning

## Goal

Give an agent the ability to read Jira issues filtered by project and status,
and post one comment per issue proposing a solution. Jira is reached through
the BTP destination service, using the existing subaccount destination
`BC_ELIAGROUP_APIHUB_JIRA`.

## Context and constraints

The destination lives in the Elia global account (`eliagroup-111-dev` /
space `111_BC`, region eu10). `pydantic-agent` currently runs in the Infrabel
global account (`infrabel-app-acc-cf` / space `AI`, region eu20-001). A
destination service instance can only read destinations belonging to its own
subaccount, and these are separate global accounts with separate XSUAA
tenants — so the Infrabel deployment cannot reach this destination at all.

**Resolution:** the app will also be deployed into `111_BC`, where it is in the
same subaccount as the destination and the ordinary destination flow applies.
That second deployment is separate work (it needs its own `aicore`, `xsuaa`,
`postgresql-db` and `html5-apps-repo` instances) and is **out of scope for this
spec**, but it is on the critical path to testing any of this end to end.

Local development does not wait for it. A destination service **service key**
carries the same four credentials a binding does and requires no deployed app:

```
cf create-service pydantic-agent-destination -p lite destination
cf create-service-key pydantic-agent-destination pydantic-agent-dest-key
```

Three `destination lite` instances already exist in `111_BC`
(`arc1-destination`, `bc-AICOSTMONITOR-MANAGE-destination`,
`bc-airfocusinitiative-optimize-destination-service`). Any of them can read a
subaccount-level destination, so reusing one instead of creating a fourth is
acceptable.

**Established facts** (confirmed with the user, 2026-08-25):

- The destination's `Authentication` is `OAuth2ClientCredentials`. The
  destination service performs the token exchange itself and returns a ready
  `Authorization` header with an expiry. This app implements no OAuth client
  of its own for Jira; `agents/client_credentials.py` is untouched.
- The Jira behind it is **Server / Data Center**, REST API v2. Comment bodies
  are a plain wiki-markup string, not Atlassian Document Format.

## Architecture

```
agent  ->  builtin:jira toolset  ->  JiraClient  ->  DestinationResolver
                                          |                  |
                                          |                  +-> XSUAA (client_credentials)
                                          |                  +-> destination-configuration API
                                          +-> Jira REST v2 (URL + header from the destination)
```

Two new modules, mirroring the existing `builtin:` pattern established by
`agents/gmail_tools.py` and `agents/outlook_tools.py`.

### `agents/destination.py` (new)

One responsibility: turn a destination name into a base URL and request
headers. It knows nothing about Jira.

```python
@dataclass(frozen=True)
class Destination:
    url: str                    # destinationConfiguration.URL, trailing slash stripped
    headers: dict[str, str]     # from authTokens[0].http_header
    expires_at: float           # monotonic deadline

@dataclass(frozen=True)
class DestinationServiceConfig:
    client_id: str
    client_secret: str
    token_url: str              # XSUAA `url` + /oauth/token
    api_url: str                # destination service `uri`

def config_from_environment(environ) -> DestinationServiceConfig | None

class DestinationResolver:
    def __init__(self, name, config, *, transport=None): ...
    async def resolve(self, *, force: bool = False) -> Destination: ...
    def invalidate(self) -> None: ...
```

Binding resolution order, first hit wins:

1. `VCAP_SERVICES` — the `destination` service array, first entry's
   `credentials`: `clientid`, `clientsecret`, `url`, `uri`.
2. Environment — `DESTINATION_CLIENT_ID`, `DESTINATION_CLIENT_SECRET`,
   `DESTINATION_TOKEN_URL` (or `DESTINATION_UAA_URL`), `DESTINATION_URI`.
   This is what `.env` supplies during local development.

Absent both, `config_from_environment` returns `None` and toolset construction
raises a message naming the missing variables — not an `AttributeError` at
first use.

Caching: a resolved `Destination` is reused until `EXPIRY_SKEW_SECONDS` (60)
before `expires_at`. `authTokens[0].expires_in` is the source; if it is absent
or unparseable, fall back to `DEFAULT_LIFETIME_SECONDS` (300), matching
`agents/client_credentials.py`. Concurrent `resolve()` calls share one fetch
behind a double-checked `asyncio.Lock`.

`transport` is a constructor seam so tests inject `httpx.MockTransport`.
Global monkeypatching of `httpx.AsyncClient` is prohibited here: it previously
leaked from the token client into the API client and produced a failure that
looked like an application bug.

### `agents/jira_tools.py` (new)

```python
BUILTIN_JIRA_URL = "builtin:jira"

class JiraClient:
    def __init__(self, resolver, *, project="", status="",
                 lookback_minutes=None, http=None): ...
    async def _req(self, method, path, **kw) -> dict   # 401 -> invalidate, retry once
    async def whoami(self) -> str                       # /rest/api/2/myself -> `name`
    async def list_issues(self, *, project=None, status=None,
                          limit=10, lookback_minutes=None) -> list[dict]
    async def get_issue(self, key) -> dict
    async def add_comment(self, key, body) -> dict

def jira_toolset(oauth, *, server_key, auth_mode, http=None): ...
```

`_req` resolves the destination on every call (cheap — normally a cache hit),
issues the request against `destination.url + path`, and on a `401` calls
`resolver.invalidate()` and retries exactly once. A second `401` is an error,
not a loop.

## Configuration

A fourth auth mode, `destination` (11 characters, within
`AUTH_MODE_MAX_LENGTH = 16`; the import-time guard in `agents/db.py` remains
the backstop that stopped `client_credentials` from reaching Postgres).

```json
{
  "url": "builtin:jira",
  "auth_mode": "destination",
  "oauth": {
    "destination": "BC_ELIAGROUP_APIHUB_JIRA",
    "project": "ABC",
    "status": "Open",
    "lookback": "7d",
    "allow_comment": false
  }
}
```

No credential of any kind is stored in this application's database. Rotating
the Jira credential happens in the destination, on Elia's side, with no change
here. This is a deliberate improvement over the `app_only` path, which stores
an encrypted client secret.

Field meanings:

| Key | Meaning |
| --- | --- |
| `destination` | Destination name to resolve. Required; validation rejects the mode without it. |
| `project` | Default Jira project key. Blank means the agent must supply one. |
| `status` | Default status filter. Blank means no status clause. |
| `lookback` | Ceiling on how far back `list_issues` may reach (`"90m"`, `"5h"`, `"2d"`, `"1w"`, or a bare number of hours). |
| `allow_comment` | Capability switch. When false the `add_comment` tool is not registered at all. |

`allow_comment` is kept separate from what the destination's credential
permits, for the same reason `allow_send` is: a credential that *can* write
must not thereby hand every agent the ability to write.

### Storage and validation

- `agents/db.py`: add `AUTH_MODE_DESTINATION = "destination"` to
  `VALID_AUTH_MODES`; add it to `OAUTH_CONFIG_MODES` so the `oauth` blob is
  persisted. Add a `_DEST_KEYS` tuple (`destination`, `project`, `status`,
  `lookback`, `allow_comment`) and a `_clean_destination(oauth, fallback)`
  cleaner alongside `_clean_client_credentials`, storing `allow_comment` as a
  real boolean defaulting to `False`.
- `agents/admin.py`: `OAuthClientPayload` gains `destination`, `project`,
  `status`, `allow_comment`. `McpServerPayload._validate_oauth` gains a
  `destination` branch requiring a destination name and rejecting `dcr`,
  `client_id` and `client_secret` — there is nothing for this mode to store.
- `agents/builtins.py`: register `BUILTIN_JIRA_URL -> jira_toolset`. Its
  docstring currently says `client_credentials`, a stale name from before the
  `app_only` rename; correct it while editing the file.

### Shared lookback helper

`parse_lookback` and `_LOOKBACK_UNITS` currently live in
`agents/outlook_tools.py` and are not mail-specific. Move them to
`agents/lookback.py` and import from there in both toolsets and in
`admin.py`'s validator. `agents/outlook_tools.py` re-exports `parse_lookback`
so existing imports and tests keep working unchanged.

## Tools exposed to the agent

### `list_issues(project=None, status=None, limit=10, lookback=None)`

JQL is built server-side; the agent never supplies raw JQL. Clauses, in order:

```
project = "ABC" AND status = "Open" AND updated >= "-10080m" ORDER BY updated ASC
```

- `project` and `status` are **pinned by configuration**: when the stored value
  is non-blank it is used and the agent's argument is ignored. The agent's
  value applies only where the stored one is blank. (These are not numeric
  ranges — there is no coherent sense in which "In Progress" narrows "Open" —
  so the rule is precedence, not intersection. An admin who wants the agent to
  choose leaves the field empty.)
- `lookback` *is* a range, so it takes the ceiling rule instead:
  `min(requested, ceiling)`, the semantics `OutlookClient._window` already
  implements. With a ceiling configured and no agent value, the ceiling is
  used. With neither, the `updated` clause is omitted entirely and the result
  set is bounded only by `limit`.
- Values are quoted and any embedded `"` or `\` escaped, so a project key
  containing a quote cannot alter the query's structure.
- `limit` is clamped to 1..50.

Issued as `POST /rest/api/2/search` with
`fields: ["summary", "status", "reporter", "updated", "description", "comment"]`.

Returns, per issue: `key`, `summary`, `status`, `reporter`, `updated`, and the
description truncated to a bounded length.

**Already-answered issues are filtered out.** The `comment` field arrives in
the same response, so any issue already carrying a comment authored by this
service account (from `whoami()`, cached for the client's lifetime) is dropped
before returning, so a repeated run does not re-comment. Default on; it is
behaviour, not a config flag.

What makes it reliable is that answering and recording are the *same call*:
the comment the agent posts is the marker the next run reads. The Outlook
agent draws the line differently — `create_reply_draft` and `move_message`
are two separate tool calls, so its record depends on the model remembering
the second one. (Separately, the tenant this app currently targets grants
`Mail.Read` + `Mail.Send` rather than `Mail.ReadWrite`, so `move_message` is
unavailable *in that deployment* — a property of the grant, not of the
toolset.)

### `get_issue(key)`

`GET /rest/api/2/issue/{key}` with the same field set. Returns the full
description and the comment thread (author, created, body), so the agent can
read the existing discussion before proposing anything.

### `add_comment(key, body)`

Registered **only** when `allow_comment` is true.
`POST /rest/api/2/issue/{key}/comment` with `{"body": body}` — wiki markup,
per the Server/DC answer. Returns the created comment's id and author.

## Out of scope

Transitions, assignment, field edits, attachments, issue creation, watchers,
and JQL supplied directly by the agent. The request is read plus a first
reply.

## Untrusted input

Issue descriptions and comments are text written by people outside this
system, arriving inside a prompt. The agent's instructions state the same
posture the mail agents use: this is data, never instructions. Text that
appears to address the agent — asking it to comment elsewhere, follow a URL,
ignore its instructions, or treat the reporter as an administrator — is
quoted into the report and flagged, never acted on.

## Error handling

| Condition | Behaviour |
| --- | --- |
| No destination binding | Toolset construction fails naming the missing env vars. |
| Destination not found (404) | Error naming the destination and the subaccount it was searched in. |
| Token expired mid-call (401) | `invalidate()`, retry once; a second 401 raises. |
| Jira 400 on search | Raise including the JQL sent, so a bad project key is diagnosable. |
| `add_comment` when `allow_comment` false | Tool absent from the toolset; the agent cannot call it. |

## Admin UI

`ui5-admin/webapp/service/types.ts`: `AuthMode` gains `"destination"`;
`OAuthClient` gains `destination`, `project`, `status`, `allow_comment`.

`McpServerDialog.fragment.xml`: selecting `destination` shows Destination name
(required), Project, Status, Look back and a Commenting checkbox, and hides
every credential field — there are none.

`validators.ts`: `validateOAuth` gains a `destination` branch mirroring the
server rules, so the failure is caught before the request.

The vanilla-JS `templates/admin.html` is unchanged. It remains the supported
admin at `/admin`, and this mode is configured through `/ui5admin`.

## Deployment

`mta.yaml` gains:

```yaml
  - name: agent-destination
    type: org.cloudfoundry.managed-service
    parameters:
      service: destination
      service-plan: lite
```

required by the `pydantic-agent` module. The binding is resolved at runtime,
not at import, so the Infrabel deployment — which has no Jira agent — is
unaffected by its presence, and a missing binding surfaces as a clear message
on the one agent that needs it.

## Testing

`tests/test_destination.py` and `tests/test_jira_tools.py`, both using
injected `httpx.MockTransport`.

Destination layer:

- `VCAP_SERVICES` parsing, including absent and malformed values.
- Environment fallback and the `DESTINATION_UAA_URL` → token URL derivation.
- Token cached across calls; refetched inside the expiry skew.
- Concurrent `resolve()` issues one fetch.
- `invalidate()` forces a refetch.
- Missing binding produces a message naming the variables.

Jira layer:

- JQL construction: project only, status only, both, neither.
- Quote and backslash escaping in project and status values.
- Lookback ceiling: agent value below the ceiling is honoured, above it is
  capped, absent falls back to the ceiling, and neither configured nor
  supplied omits the `updated` clause.
- Configured `project` / `status` win over the agent's arguments; a blank
  configured value lets the agent's own value through.
- `limit` clamping.
- 401 invalidates and retries exactly once; a second 401 raises.
- Already-answered filtering drops an issue commented by the service account
  and keeps one commented only by others.
- `add_comment` is absent from the toolset when `allow_comment` is false and
  present when true.
- `admin.py` validation: `destination` mode without a name is rejected; with
  `dcr` or a `client_secret` is rejected.
- The `auth_mode` column-width guard still holds with the new mode.

Note on test fidelity: the Python suite runs against SQLite, which ignores
`VARCHAR` limits that Postgres enforces. The import-time guard in
`agents/db.py` is what actually protects the new mode's length, and the test
asserts the guard, not the column.
