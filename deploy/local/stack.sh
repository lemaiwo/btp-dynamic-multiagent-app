#!/usr/bin/env sh
# docker compose for the local stack, with the secrets file it needs.
#   ./stack.sh up        build and start (detached)
#   ./stack.sh down      stop (the database volume is kept; "down -v" drops it)
#   ./stack.sh logs app  follow a service's log
#   ./stack.sh ps | restart app | ...   anything else goes to docker compose
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ ! -e "$HERE/secrets/stack.env" ]; then
    echo "run ./setup.sh <e-mail address> first" >&2
    exit 1
fi
compose() { docker compose --project-directory "$HERE" -f "$HERE/compose.yaml" --env-file "$HERE/secrets/stack.env" "$@"; }
case "${1:-}" in
    up)   shift; compose up -d --build "$@" ;;
    logs) shift; compose logs -f --tail 100 "$@" ;;
    "")   echo "usage: ./stack.sh up | down | logs [service] | ps | ..." >&2; exit 2 ;;
    *)    compose "$@" ;;
esac
