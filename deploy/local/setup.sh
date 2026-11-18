#!/usr/bin/env sh
# One-time setup of the local stack: generates every secret into ./secrets
# (gitignored) and never prints one.
#
#   ./setup.sh you@example.com
#   ./setup.sh you@example.com https://agents.example.com:8443 https://idp.example.com:8443
#
# With the two URLs (the app's and the identity provider's public addresses,
# https) the stack is set up to sit behind a TLS reverse proxy on this
# machine; secrets/Caddyfile.snippet then holds the two site blocks for it.
# Changing the URLs renders the realm again, with new test passwords.
#
# The e-mail address is the test user's: with the default settings it is the
# SAML NameID, so it must be the e-mail of your user in the SAP system (SU01).
# Existing files are kept: run it again after deleting what you want renewed.
set -eu

EMAIL="${1:-}"
APP_URL="${2:-}"
IDP_URL="${3:-}"
if [ -z "$EMAIL" ] || { [ -n "$APP_URL" ] && [ -z "$IDP_URL" ]; }; then
    echo "usage: ./setup.sh <e-mail address of your SAP user> [<app URL> <idp URL>]" >&2
    exit 2
fi
for url in "$APP_URL" "$IDP_URL"; do
    case "$url" in
        ""|https://*[A-Za-z0-9]) ;;
        *) echo "a public URL is https://host[:port], without a path" >&2; exit 2 ;;
    esac
    case "$url" in *[!A-Za-z0-9:/.-]*) echo "a public URL is https://host[:port]" >&2; exit 2 ;; esac
done
case "$EMAIL" in
    *[!A-Za-z0-9@._+-]*|*@*@*) echo "that does not look like an e-mail address" >&2; exit 2 ;;
    *@*.*) ;;
    *) echo "that does not look like an e-mail address" >&2; exit 2 ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
SECRETS="$HERE/secrets"
SAML="$SECRETS/saml"
ISSUER="agent-local-saml-idp"
umask 077
mkdir -p "$SAML" "$SECRETS/extra-ca"

random() { openssl rand -hex "$1"; }
kept() { echo "kept     $1"; }
made() { echo "created  $1"; }

# --- stack.env: what compose.yaml interpolates --------------------------------
if [ -e "$SECRETS/stack.env" ]; then
    kept secrets/stack.env
else
    {
        echo "POSTGRES_PASSWORD=$(random 16)"
        echo "KEYCLOAK_ADMIN_PASSWORD=$(random 12)"
        echo "OIDC_CLIENT_SECRET=$(random 24)"
        # oauth2-proxy wants 16, 24 or 32 bytes: 32 hex characters are 32 bytes.
        echo "PROXY_COOKIE_SECRET=$(random 16)"
    } > "$SECRETS/stack.env"
    made secrets/stack.env
fi
OIDC_CLIENT_SECRET="$(sed -n 's/^OIDC_CLIENT_SECRET=//p' "$SECRETS/stack.env")"

# --- public addresses (optional) ----------------------------------------------
setting() { sed -n "s|^$1=||p" "$SECRETS/stack.env"; }
if [ -n "$APP_URL" ] && { [ "$(setting APP_URL)" != "$APP_URL" ] || [ "$(setting IDP_URL)" != "$IDP_URL" ]; }; then
    IDP_HOST="${IDP_URL#https://}"; IDP_HOST="${IDP_HOST%%:*}"
    grep -v '^APP_URL=\|^IDP_URL=\|^IDP_HOST=\|^COOKIE_SECURE=' "$SECRETS/stack.env" > "$SECRETS/stack.env.new"
    {
        echo "APP_URL=$APP_URL"
        echo "IDP_URL=$IDP_URL"
        echo "IDP_HOST=$IDP_HOST"
        echo "COOKIE_SECURE=true"
    } >> "$SECRETS/stack.env.new"
    mv "$SECRETS/stack.env.new" "$SECRETS/stack.env"
    # The realm names the app's URL (redirect): render it again.
    rm -f "$SECRETS/realm.json" "$SECRETS/logins.txt"
    echo "updated  secrets/stack.env (public addresses)"
fi
APP_URL="$(setting APP_URL)"; APP_URL="${APP_URL:-http://localhost:4180}"
IDP_URL="$(setting IDP_URL)"; IDP_URL="${IDP_URL:-http://localhost:8180}"

# --- realm.json: the Keycloak realm with its client secret and test users -----
if [ -e "$SECRETS/realm.json" ]; then
    kept secrets/realm.json
else
    USER_PASSWORD="$(random 10)"
    BASIC_PASSWORD="$(random 10)"
    # The second factor (TOTP): Keycloak keeps the secret as text, an
    # authenticator app wants the base32 of it.
    USER_OTP="$(random 10)"
    BASIC_OTP="$(random 10)"
    key() { python3 -c "import base64,sys; print(base64.b32encode(sys.argv[1].encode()).decode().rstrip('='))" "$1"; }
    sed -e "s/__OIDC_CLIENT_SECRET__/$OIDC_CLIENT_SECRET/" \
        -e "s/__USER_EMAIL__/$EMAIL/" \
        -e "s/__USER_PASSWORD__/$USER_PASSWORD/" \
        -e "s/__BASIC_PASSWORD__/$BASIC_PASSWORD/" \
        -e "s/__USER_OTP__/$USER_OTP/" \
        -e "s/__BASIC_OTP__/$BASIC_OTP/" \
        -e "s|__APP_URL__|$APP_URL|g" \
        "$HERE/keycloak/realm.template.json" > "$SECRETS/realm.json"
    {
        echo "Sign-in at $APP_URL (Keycloak realm 'agents'): password, then a one-time code."
        echo "Add the key to an authenticator app (TOTP, 6 digits, 30 s, SHA-1)."
        echo "  developer / $USER_PASSWORD   totp-key: $(key "$USER_OTP")   roles: user, developer, admin   e-mail: $EMAIL"
        echo "  basic     / $BASIC_PASSWORD   totp-key: $(key "$BASIC_OTP")   roles: user"
        echo "Keycloak admin console http://localhost:8180 (this machine only): user 'admin', password in stack.env"
    } > "$SECRETS/logins.txt"
    # Keycloak runs as another uid and must be able to read the import file.
    chmod 644 "$SECRETS/realm.json"
    made secrets/realm.json
    made "secrets/logins.txt (the test users' passwords)"
fi

# --- app.env: the app's own environment --------------------------------------
if [ -e "$SECRETS/app.env" ]; then
    kept secrets/app.env
else
    cat > "$SECRETS/app.env" <<'ENV'
# The agent app's environment. Lines are NAME=value, no quotes.
#
# --- models: at least one provider -------------------------------------------
# Direct API providers. A model is named <provider>:<model>.
#ANTHROPIC_API_KEY=
#ANTHROPIC_MODELS=claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5
#OPENAI_API_KEY=
#OPENAI_MODELS=gpt-4o
#OPENAI_BASE_URL=
# SAP AI Core next to (or instead of) them: the four values of a service key.
#AICORE_AUTH_URL=
#AICORE_CLIENT_ID=
#AICORE_CLIENT_SECRET=
#AICORE_BASE_URL=
#AICORE_RESOURCE_GROUP=default
# The model the orchestrator starts with (else the first one available).
#DEFAULT_MODEL=anthropic:claude-opus-5-5
#
# --- passwords named as ${...} in destinations.json ---------------------------
#SAP_TECH_PASSWORD=
#
# --- optional -----------------------------------------------------------------
# Hosts an MCP server with auth mode "jwt" may be on (a local ARC-1).
#MCP_URL_ALLOWLIST=arc1.example.corp
DATA_CATALOG_EMBEDDINGS=0
ENV
    made "secrets/app.env (add a model API key before starting)"
fi

# --- arc1.env.template: an ARC-1 next to the app (optional) -------------------
if [ -e "$SECRETS/arc1.env.template" ]; then
    kept secrets/arc1.env.template
else
    cat > "$SECRETS/arc1.env.template" <<'ENV'
# ARC-1 next to the app, on the same sign-in. Copy this file to arc1.env and
# fill it in; ./stack.sh up then builds and starts ARC-1 as well.
SAP_URL=https://REPLACE_ME_SAP_HOST:44300
SAP_CLIENT=100
SAP_LANGUAGE=EN
# The shared startup identity (discovery, feature probes). Calls of a
# signed-in user never run as this user.
SAP_USER=REPLACE_ME
SAP_PASSWORD=REPLACE_ME
# The ABAP system's SAML2 local provider name (the destination's "audience").
SAP_PP_SAML_AUDIENCE=REPLACE_ME_SAML2_LOCAL_PROVIDER_NAME
# Safety ceilings; a propagated user is still limited by their own SAP authorizations.
SAP_ALLOW_WRITES=false
SAP_ALLOW_DATA_PREVIEW=false
SAP_ALLOW_FREE_SQL=false
ENV
    made "secrets/arc1.env.template (copy to arc1.env to add ARC-1)"
fi

# --- destinations.json --------------------------------------------------------
if [ -e "$SECRETS/destinations.json" ]; then
    kept secrets/destinations.json
else
    cp "$HERE/destinations.example.json" "$SECRETS/destinations.json"
    chmod 644 "$SECRETS/destinations.json"
    made "secrets/destinations.json (fill in the SAP system)"
fi

# --- SAML signing key pair and the metadata for transaction SAML2 -------------
KEY="$SAML/saml-idp.key"
CRT="$SAML/saml-idp.crt"
META="$SAML/saml-idp-metadata.xml"
if [ -e "$KEY" ]; then
    kept secrets/saml/saml-idp.key
else
    MSYS2_ARG_CONV_EXCL="/CN=" openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 365 \
        -subj "/CN=$ISSUER" -keyout "$KEY" -out "$CRT" 2>/dev/null
    DER="$(sed -e '/-----/d' "$CRT" | tr -d '\r\n')"
    cat > "$META" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" xmlns:ds="http://www.w3.org/2000/09/xmldsig#" entityID="$ISSUER">
  <md:IDPSSODescriptor WantAuthnRequestsSigned="false" protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data><ds:X509Certificate>$DER</ds:X509Certificate></ds:X509Data></ds:KeyInfo></md:KeyDescriptor>
    <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>
    <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified</md:NameIDFormat>
    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" Location="http://localhost:4180/saml/sso"/>
    <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" Location="http://localhost:4180/saml/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>
XML
    # The app container runs as uid 10001 and reads the key through a
    # read-only mount; the folder itself stays private to you.
    chmod 644 "$KEY" "$CRT" "$META"
    made "secrets/saml/saml-idp.key, .crt"
    made "secrets/saml/saml-idp-metadata.xml (upload in transaction SAML2)"
fi

# --- the reverse proxy's site blocks, for public addresses --------------------
case "$APP_URL" in https://*)
    cat > "$SECRETS/Caddyfile.snippet" <<CADDY
# Agent app and its identity provider (deploy/local of the agent app).
# Append to the Caddyfile once both names resolve to this machine, then
# reload Caddy. The names share the port with other sites: Caddy routes by name.
${APP_URL#https://} {
	request_body {
		max_size 60MB
	}
	reverse_proxy 127.0.0.1:4180 {
		flush_interval -1
	}
}

${IDP_URL#https://} {
	# Sign-in only: no admin console, no master realm, no health or metrics.
	@closed path /admin /admin/* /realms/master /realms/master/* /metrics /metrics/* /health /health/*
	respond @closed 404
	reverse_proxy 127.0.0.1:8180
}
CADDY
    made "secrets/Caddyfile.snippet (the reverse proxy's two site blocks)"
    ;;
esac

echo
echo "Next: add a model API key to secrets/app.env, the SAP system to"
echo "secrets/destinations.json, then ./stack.sh up"
