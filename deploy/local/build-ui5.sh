#!/usr/bin/env sh
# Builds the two UI5 apps (ui5-admin, ui5-ide) into their dist/ folders, which
# the stack's web server serves at /ui5admin and /ui5ide. Run it once, and
# again after changing an app. The build replaces the dist folders, so the
# web server is restarted at the end to see the new ones.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
for app in ui5-admin ui5-ide; do
    echo "building $app ..."
    (cd "$ROOT/$app" && { [ -d node_modules ] || npm ci; } && npm run build:ui5 >/dev/null)
    echo "built    $app/dist"
done
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^agent-local-web-1$'; then
    docker restart agent-local-web-1 >/dev/null && echo "restarted the stack's web server"
fi
