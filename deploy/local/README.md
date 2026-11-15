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

## ARC-1 with the same sign-in

An ARC-1 that validates tokens of the same issuer can be attached to an agent
in two ways, both as the signed-in user:

- auth mode `jwt` with its URL (add the host to `MCP_URL_ALLOWLIST` in
  `secrets/app.env`), or
- auth mode `destination` with a `ForwardUserToken` destination.

ARC-1 then needs this stack's client id among the audiences it accepts
(`aud` of the token is `agent-app`).
