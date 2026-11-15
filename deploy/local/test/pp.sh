#!/usr/bin/env sh
# Principal propagation check against the SAP system of secrets/destinations.json:
# signs in as the test user "developer" and calls the app's own admin checks
# through the proxy, AS THAT USER (destination SAP_USER, SAML bearer assertion).
#
#   ./test/pp.sh odata /sap/opu/odata/sap/<SERVICE> [v2|v4]   read $metadata
#   ./test/pp.sh ina <InA query name, e.g. 2C...> [package]   describe the query
#
# Prints statuses, error codes and counts; never a token, a password or a row.
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${STACK_URL:-http://localhost:4180}"
DEST="${PP_DESTINATION:-SAP_USER}"
KIND="${1:-}"
TARGET="${2:-}"
if [ -z "$KIND" ] || [ -z "$TARGET" ]; then
    sed -n '2,9p' "$0" >&2
    exit 2
fi

secret="$(sed -n 's/^OIDC_CLIENT_SECRET=//p' "$HERE/secrets/stack.env")"
password="$(sed -n 's/^  developer \/ \([^ ]*\).*/\1/p' "$HERE/secrets/logins.txt")"
otp="$(python3 "$HERE/test/totp.py" "$(sed -n 's/^  developer .*totp-key: \([A-Z2-7]*\).*/\1/p' "$HERE/secrets/logins.txt")")"
TOKEN="$(curl -s http://localhost:8180/realms/agents/protocol/openid-connect/token \
    -d grant_type=password -d client_id=agent-app --data-urlencode "client_secret=$secret" \
    -d username=developer --data-urlencode "password=$password" -d "totp=$otp" \
    -d 'scope=openid email profile' \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")"
if [ -z "$TOKEN" ]; then
    echo "FAIL sign-in: Keycloak issued no token" >&2
    exit 1
fi
echo "PASS sign-in as developer"

OUT="$(mktemp)"; HDR="$(mktemp)"
trap 'rm -f "$OUT" "$HDR"' EXIT
call() { # method path json-body
    curl -s -o "$OUT" -D "$HDR" -w '%{http_code}' -X "$1" "$BASE$2" \
        -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
        -H 'X-Requested-With: XMLHttpRequest' ${3:+-d "$3"}
}
code_of() { grep -i "^x-$1-error:" "$HDR" | sed 's/^[^:]*: *//' | tr -d '\r'; }
detail() { python3 -c "
import sys,json
try: d=json.load(open(sys.argv[1]))
except Exception: print(''); sys.exit()
print(str(d.get('detail',''))[:300] if isinstance(d,dict) else '')" "$OUT"; }

case "$KIND" in
odata)
    VERSION="${3:-v2}"
    BODY="$(python3 -c "import json,sys; print(json.dumps({'destination':sys.argv[1],'service_path':sys.argv[2],'odata_version':sys.argv[3],'user_context':True}))" "$DEST" "$TARGET" "$VERSION")"
    STATUS="$(call POST /admin/api/odata/metadata "$BODY")"
    if [ "$STATUS" = 200 ]; then
        python3 -c "
import sys,json
d=json.load(open(sys.argv[1]))
sets=d.get('entity_sets') or []
print('PASS \$metadata read as the signed-in user:', len(sets), 'entity sets,', len(d.get('operations') or []), 'operations')
print('     first entity sets:', ', '.join(str(s.get('name')) for s in sets[:8]))" "$OUT"
    else
        echo "FAIL \$metadata: HTTP $STATUS, code '$(code_of OData)': $(detail)"
        exit 1
    fi
    ;;
ina)
    PACKAGE="${3:-}"
    NAME="pp-test"
    call DELETE "/admin/api/ina/queries/$NAME" >/dev/null || true
    BODY="$(python3 -c "import json,sys; print(json.dumps({'name':sys.argv[1],'title':'Principal propagation test','purpose':'Connectivity test of the local stack','not_for':'','destination':sys.argv[2],'user_context':True,'query':sys.argv[3],'package':sys.argv[4],'enabled':True}))" "$NAME" "$DEST" "$TARGET" "$PACKAGE")"
    STATUS="$(call POST /admin/api/ina/queries "$BODY")"
    if [ "$STATUS" != 201 ]; then
        echo "FAIL register the query: HTTP $STATUS: $(detail)"
        exit 1
    fi
    echo "PASS query registered as '$NAME' (runs as the signed-in user)"
    STATUS="$(call POST "/admin/api/ina/queries/$NAME/describe" '{}')"
    if [ "$STATUS" = 200 ]; then
        python3 -c "
import sys,json
d=json.load(open(sys.argv[1]))
print('PASS described as the signed-in user:', len(d.get('dimensions') or []), 'dimensions,', len(d.get('measures') or []), 'measures,', len(d.get('variables') or []), 'variables')" "$OUT"
    else
        echo "FAIL describe: HTTP $STATUS, code '$(code_of Ina)': $(detail)"
        exit 1
    fi
    STATUS="$(call POST "/admin/api/ina/queries/$NAME/test" '{}')"
    if [ "$STATUS" = 200 ]; then
        python3 -c "
import sys,json
d=json.load(open(sys.argv[1]))
print('PASS' if d.get('ok') else 'FAIL', 'test read: ok', d.get('ok'), '| status', d.get('status'), '| columns', d.get('columns'))" "$OUT"
    else
        echo "INFO test read: HTTP $STATUS, code '$(code_of Ina)': $(detail)"
    fi
    ;;
*)
    echo "unknown check '$KIND' (odata | ina)" >&2
    exit 2
    ;;
esac
