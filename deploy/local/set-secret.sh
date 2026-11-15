#!/usr/bin/env sh
# Puts one setting into secrets/app.env without it passing through a chat,
# a command line or the shell history: the value is typed at a hidden prompt.
#
#   ./set-secret.sh ANTHROPIC_API_KEY        ask for the value and store it
#   ./set-secret.sh --unset OPENAI_API_KEY   remove a setting
#
# Then ./stack.sh up (the app reads the file when its container is created).
set -eu
FILE="$(cd "$(dirname "$0")" && pwd)/secrets/app.env"
[ -e "$FILE" ] || { echo "run ./setup.sh first" >&2; exit 1; }
UNSET=""
if [ "${1:-}" = "--unset" ]; then UNSET=1; shift; fi
NAME="${1:-}"
case "$NAME" in
    ""|*[!A-Z0-9_]*) echo "usage: ./set-secret.sh [--unset] NAME   (NAME in capitals)" >&2; exit 2 ;;
esac
umask 077
grep -v "^$NAME=" "$FILE" > "$FILE.new" || true
if [ -z "$UNSET" ]; then
    printf '%s (input hidden): ' "$NAME" >&2
    stty -echo 2>/dev/null || true
    IFS= read -r VALUE
    stty echo 2>/dev/null || true
    printf '\n' >&2
    [ -n "$VALUE" ] || { rm -f "$FILE.new"; echo "nothing entered; unchanged" >&2; exit 1; }
    printf '%s=%s\n' "$NAME" "$VALUE" >> "$FILE.new"
fi
mv "$FILE.new" "$FILE"
[ -z "$UNSET" ] && echo "stored $NAME in secrets/app.env" || echo "removed $NAME from secrets/app.env"
