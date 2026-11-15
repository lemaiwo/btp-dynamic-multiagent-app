# The agent app without BTP: Docker, PostgreSQL, OIDC and on-premise SSO

A test setup that runs the whole app on one machine, next to the SAP systems,
with no Cloud Foundry, XSUAA, destination service or Cloud Connector:

| On BTP | Here |
|---|---|
| approuter + XSUAA sign the user in | oauth2-proxy + an OIDC provider (Keycloak in this stack; SAP IAS or Entra ID work the same way) |
| XSUAA scopes (`admin`, `developer`, `user`, ...) | roles of the same names in the token |
| `postgresql-db` service | a PostgreSQL container |
| SAP AI Core | AI Core **and/or** the Anthropic and OpenAI APIs directly |
| destination service | a JSON file, `secrets/destinations.json` |
| principal propagation through the Cloud Connector | a SAML 2.0 bearer assertion this app signs per user; the ABAP system trusts the certificate in transaction SAML2 |

**Status: test branch.** Proven here: the stack starts, sign-in works, tokens
and roles are enforced, assertions verify with an independent XML signature
library. **Not yet run against an SAP system**: the assertion layout is the
one ARC-1's `proto/saml-bearer-pp` branch ran against an ABAP Platform 2025
system, but this implementation has not been in front of one.

## Start

```sh
cd deploy/local
./setup.sh you@example.com     # once; the e-mail of your SAP user (SU01)
# edit secrets/app.env:            a model API key (or AI Core credentials)
# edit secrets/destinations.json:  the SAP system
./stack.sh up
```

- Chat: <http://localhost:4180/chat> · classic chat: <http://localhost:4180/> · admin: <http://localhost:4180/admin>
- Sign in as `developer` (roles user, developer, admin) or `basic` (role user); the passwords are in `secrets/logins.txt`.
- Keycloak console: <http://localhost:8180> (user `admin`, password in `secrets/stack.env`).
- `./stack.sh logs app`, `./stack.sh ps`, `./stack.sh down` (`down -v` also drops the database).

Everything listens on loopback only. `secrets/` is gitignored; `setup.sh`
never overwrites a file that exists (delete one to have it made again, and
recreate the Keycloak container after changing `realm.json`).

The UI5 apps (`/ui5admin`, `/ui5ide`) are not part of this stack: on BTP the
HTML5 repository serves them. The classic admin at `/admin` and both chats are.

## Models

A model name with a provider prefix goes to that provider's API; a name
without one is an SAP AI Core deployment, as before.

| In `secrets/app.env` | Models offered |
|---|---|
| `ANTHROPIC_API_KEY` | `anthropic:<model>` for each name in `ANTHROPIC_MODELS` |
| `OPENAI_API_KEY` (+ `OPENAI_BASE_URL` for any OpenAI-compatible endpoint) | `openai:<model>` for each name in `OPENAI_MODELS` |
| `AICORE_AUTH_URL`, `AICORE_CLIENT_ID`, `AICORE_CLIENT_SECRET`, `AICORE_BASE_URL` | the deployments of the resource group |

`DEFAULT_MODEL` names the model a fresh registry starts with; afterwards the
admin picks it (Settings), and an agent can have its own. The catalogue's
embeddings are AI Core only, so they are off here (`DATA_CATALOG_EMBEDDINGS=0`)
unless AI Core is configured.

## Sign-in and roles

oauth2-proxy signs the user in and hands the access token to the app in
`X-Forwarded-Access-Token` (`AUTH_TOKEN_HEADER`). The app validates it
(`OIDC_ISSUER`, `OIDC_AUDIENCE`, signing keys from `OIDC_JWKS_URL`) on every
request, so the header gives nobody an identity the provider did not sign.
A server-to-server caller sends its own token as `Authorization: Bearer`.

Roles are read from `realm_access.roles`, `roles` and `groups`
(`OIDC_ROLES_CLAIM` changes that) and from the `scope` claim. The names are
those of `xs-security.json`: `user`, `admin`, `developer`, `a2a`, `invoke`,
`JOBSCHEDULER`.

Another provider (IAS, Entra ID): point the `OIDC_*` variables of the app and
the `OAUTH2_PROXY_*` variables of the proxy in `compose.yaml` at it, drop the
`keycloak` service, and make the provider put the role names into one of the
claims above and the app's client id into `aud`.

## Destinations

`secrets/destinations.json` holds what the destination service holds on BTP;
the property names are the same. A value may name an environment variable as
`${NAME}` (set it in `secrets/app.env`), so no password needs to be in the file.

| `Authentication` | Acts as | Notes |
|---|---|---|
| `NoAuthentication` | nobody | `URL.headers.Authorization` may carry a static credential |
| `BasicAuthentication` | a technical user | `User`, `Password` |
| `SAMLAssertion` | **the signed-in user** | `audience` (required), `userIdSource`, `nameIdFormat`, `recipient` |
| `ForwardUserToken` | **the signed-in user** | their OIDC token as `Bearer`, for a target that validates the same issuer (an ARC-1 MCP server) |

`"AllowPlainHttp": "true"` admits an `http://` URL, for a service inside the
container network only; never for `SAMLAssertion`.

The target is reached directly from the container, over `https`. If its
certificate is from your own CA, put the CA certificate (PEM, `.crt`) into
`secrets/extra-ca/` and restart the app: it is trusted next to the public
ones. There is no switch that turns certificate checks off.

In the admin, a built-in is attached to a destination by its name as on BTP
(OData services, analytical queries, MCP servers with auth mode
`destination`, ...); "Runs as: signed-in user" needs a `SAMLAssertion`
destination.

## Single sign-on to an on-premise ABAP system

For a `SAMLAssertion` destination the app signs a short-lived SAML 2.0
bearer assertion for the validated user and sends it as
`Authorization: SAML2.0 <base64>` with `x-sap-security-session: create`.
The NameID is the token's `email` claim by default.

On the ABAP system, once per client (SAP Help: "SAML Bearer Assertions for
Principal Propagation"; supported on-premise from S/4HANA 2020):

1. SICF: activate `/sap/bc/saml2`, `/sap/public/bc/sec/saml2` and
   `/sap/bc/webdynpro/sap/saml2`.
2. Transaction SAML2: enable SAML 2.0 support and create the local provider.
   Its **name** is the destination's `audience`.
3. Trusted Providers, show **SAML Bearer Assertion Providers** (not "Identity
   Providers", not "OAuth 2.0 Identity Providers"), Add, Upload Metadata
   File: `secrets/saml/saml-idp-metadata.xml`.
4. Edit the provider, Identity Federation, Add: NameID format **E-mail**,
   user ID source e-mail. Save, then Enable.
5. SU01: the user's e-mail address must be the one the identity provider
   sends, exactly once in the client.

To map by user name instead: `"nameIdFormat": "unspecified"` and
`"userIdSource": "preferred_username"` in the destination, and NameID format
"Unspecified" mapped to the logon ID in step 4.

A rejected assertion: the troubleshooting tab of transaction SAML2, or
`SEC_DIAG_TOOL`. The app's log names the destination and the principal an
assertion was issued for, never the assertion.

What to check with the first real system: whether the system accepts the
assertion at all (the signature is verified here only by libraries), whether
`x-sap-security-session: create` on every request is what the system wants
(set `"URL.headers.x-sap-security-session": "none"` in the destination to
send none), and CSRF-protected writes through OData.

## Publishing (reaching it from outside)

Nothing listens beyond loopback. To reach the app and the sign-in from
another device, a TLS reverse proxy on this machine (Caddy here) publishes
two names, one for the app and one for the identity provider:

```sh
./setup.sh you@example.com https://agents.example.com:8443 https://idp.example.com:8443
./stack.sh up --force-recreate
```

- `setup.sh` stores the two addresses in `secrets/stack.env`, renders the
  realm again for them (**new test passwords and one-time-code keys**, in
  `secrets/logins.txt`) and writes `secrets/Caddyfile.snippet`: the two site
  blocks. Append them to the Caddyfile once both names resolve to this
  machine (Caddy gets the certificates then), and reload Caddy.
- Use two names of their own, not paths or ports of another site: cookies
  ignore ports, and the app has no path prefix.
- The identity provider's block answers 404 for the admin console, the
  master realm, health and metrics; the console stays at
  <http://localhost:8180> on this machine.
- Sign-in asks for the password and a one-time code (the key is in
  `secrets/logins.txt`; add it to an authenticator app). Eight wrong
  attempts lock the account for a while. The password grant is off for the
  public client; the app's own client keeps it behind its client secret
  (the test scripts use it).
- This is still Keycloak in development mode with an import-only realm: a
  test setup for yourself, not a production identity provider.

`E2E_LOCAL_NAMES=1 node test/e2e.mjs` runs the browser test against the
public addresses with every name resolved to this machine, which works
before the DNS records exist (with `tls internal` in the two site blocks).

## Testing

**The stack itself, in a browser** (no model key needed):

```sh
STACK_TEST_MODEL=1 ./stack.sh up     # adds a stand-in model, "openai:test-model"
node test/e2e.mjs                    # drives the installed Chrome, headless
```

It signs in through Keycloak as `developer` and as `basic` (password and
one-time code), opens the web
chat, the classic chat and the admin, creates an agent on the `ARC1`
destination, chats, has the orchestrator delegate to that agent, and checks
that the user without the admin role gets 403. Screenshots land in
`secrets/e2e/`. The stand-in model (`test/fake_model.py`) answers fixed text
and calls a delegation tool when asked to "ask the <agent>": it proves the
plumbing, not an answer. `./stack.sh up` without the variable goes back to
the real providers.

**Principal propagation to the SAP system** (after the SAML2 trust is set up
and `secrets/destinations.json` names the system):

```sh
./test/pp.sh odata /sap/opu/odata/sap/<SERVICE> [v2|v4]
./test/pp.sh ina <InA query name> [package]
```

Both sign in as `developer` and use the app's own admin checks as that user
through the `SAP_USER` destination: the OData one reads `$metadata`, the InA
one registers the query as `pp-test`, describes it and runs the one-row test
read. A pass means SAP accepted the assertion for the user whose e-mail the
identity provider sent; which SAP user that is shows in SAP (SM04 or the
security audit log).

## ARC-1 on the same sign-in

The realm is set up like the one of ARC-1's `examples/local-sso`: a token the
app's users get also names `arc1-local` in `aud`, so an ARC-1 that validates
this realm accepts it, and the public client `arc1-local` lets an MCP client
(Claude Code, VS Code) sign in to that ARC-1 by itself.

To run ARC-1 in this stack:

```sh
cp secrets/arc1.env.template secrets/arc1.env    # then fill in the SAP system
./stack.sh up                                    # builds ARC-1's prototype branch too
```

- ARC-1 validates the realm's tokens (`SAP_OIDC_ISSUER`, audience
  `arc1-local`) and signs the SAML bearer assertion for the user itself, with
  the **same key pair** as the app (`secrets/saml`, issuer
  `agent-local-saml-idp`): the ABAP system needs one trusted provider.
- The app reaches it through the destination `ARC1` (`ForwardUserToken`: the
  signed-in user's token is forwarded; `AllowPlainHttp` because it is
  `http://arc1:8080` inside the container network).
- Attach it to an agent in the admin: MCP server URL `https://arc1.local/mcp`
  (only the path is used), auth mode `destination`, destination `ARC1`, user
  context on. For the ABAP Assistant, name `ARC1` as the destination of the
  target's conventions.
- MCP clients on this machine reach it at <http://localhost:8091/mcp>.
- The tokens carry no ARC-1 scopes (`read`, `write`, ...), which ARC-1 treats
  as read-only. Wider access needs those scopes as client scopes in the
  realm, on top of ARC-1's own `SAP_ALLOW_*` ceilings.

An ARC-1 elsewhere works the same way: point a `ForwardUserToken` destination
(https) at it, or use auth mode `jwt` with its URL and its host in
`MCP_URL_ALLOWLIST`; it must accept this realm's issuer and `arc1-local`
(or `agent-app`) as audience.
