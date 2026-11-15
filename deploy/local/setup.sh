#!/usr/bin/env sh
# One-time setup of the local stack: generates every secret into ./secrets
# (gitignored) and never prints one.
#
#   ./setup.sh you@example.com
#
# The e-mail address is the test user's: with the default settings it is the
# SAML NameID, so it must be the e-mail of your user in the SAP system (SU01).
# Existing files are kept: run it again after deleting what you want renewed.
set -eu

EMAIL="${1:-}"
if [ -z "$EMAIL" ]; then
    echo "usage: ./setup.sh <e-mail address of your SAP user>" >&2
    exit 2
fi
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

# --- realm.json: the Keycloak realm with its client secret and test users -----
if [ -e "$SECRETS/realm.json" ]; then
    kept secrets/realm.json
else
    USER_PASSWORD="$(random 10)"
    BASIC_PASSWORD="$(random 10)"
    sed -e "s/__OIDC_CLIENT_SECRET__/$OIDC_CLIENT_SECRET/" \
        -e "s/__USER_EMAIL__/$EMAIL/" \
        -e "s/__USER_PASSWORD__/$USER_PASSWORD/" \
        -e "s/__BASIC_PASSWORD__/$BASIC_PASSWORD/" \
        "$HERE/keycloak/realm.template.json" > "$SECRETS/realm.json"
    {
        echo "Sign-in at http://localhost:4180 (Keycloak realm 'agents')"
        echo "  developer / $USER_PASSWORD   roles: user, developer, admin   e-mail: $EMAIL"
        echo "  basic     / $BASIC_PASSWORD   roles: user"
        echo "Keycloak admin console http://localhost:8180: user 'admin', password in stack.env"
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

echo
echo "Next: add a model API key to secrets/app.env, the SAP system to"
echo "secrets/destinations.json, then ./stack.sh up"
