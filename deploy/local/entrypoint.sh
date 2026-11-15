#!/bin/sh
# Container entrypoint of the agent app.
#
# Extra certificate authorities: an on-premise SAP system usually has a
# certificate of the company's own CA. Mount the CA certificates (PEM, *.crt
# or *.pem) under /extra-ca and they are trusted NEXT TO the public ones:
# httpx reads SSL_CERT_FILE as its whole trust store, so the bundle built here
# is certifi's list plus the mounted files. Nothing switches verification off.
set -eu

if [ -d /extra-ca ] && ls /extra-ca/*.crt /extra-ca/*.pem >/dev/null 2>&1; then
    bundle="$(mktemp /tmp/ca-bundle.XXXXXX)"
    python -c "import certifi; print(certifi.where())" | xargs cat > "$bundle"
    for file in /extra-ca/*.crt /extra-ca/*.pem; do
        [ -f "$file" ] || continue
        printf '\n' >> "$bundle"
        cat "$file" >> "$bundle"
    done
    export SSL_CERT_FILE="$bundle"
    export REQUESTS_CA_BUNDLE="$bundle"
    echo "entrypoint: trusting the extra certificate authorities in /extra-ca"
fi

exec "$@"
