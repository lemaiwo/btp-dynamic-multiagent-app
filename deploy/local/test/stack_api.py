"""Sign in to the local stack as the test user "developer" and call its API.
Helper for the test scripts; standard library only, runs on old Pythons.
Nothing here prints a password, a one-time code or a token."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("STACK_URL", "http://localhost:4180")


def _secret(path, pattern):
    with open(os.path.join(HERE, "secrets", path)) as handle:
        return re.search(pattern, handle.read(), re.M).group(1)


def token(user="developer"):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import totp
    form = {
        "grant_type": "password", "client_id": "agent-app", "username": user,
        "client_secret": _secret("stack.env", r"^OIDC_CLIENT_SECRET=(.+)$"),
        "password": _secret("logins.txt", r"^\s*%s\s*/\s*(\S+)" % user),
        "scope": "openid email profile",
    }
    key = _secret("logins.txt", r"^\s*%s\s.*totp-key:\s*([A-Z2-7]+)" % user)
    for _ in range(9):  # a one-time code is accepted once: wait for the next
        form["totp"] = totp.code(key)
        request = urllib.request.Request(
            "http://localhost:8180/realms/agents/protocol/openid-connect/token",
            data=urllib.parse.urlencode(form).encode())
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)["access_token"]
        except urllib.error.HTTPError:
            time.sleep(5)
    raise SystemExit("FAIL sign-in: Keycloak issued no token")


class Api:
    def __init__(self, user="developer"):
        self._token = token(user)

    def call(self, method, path, body=None):
        """(status, parsed JSON or None, headers)."""
        request = urllib.request.Request(
            BASE + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self._token,
                     "Content-Type": "application/json",
                     "X-Requested-With": "XMLHttpRequest"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw, status, headers = response.read(), response.status, response.headers
        except urllib.error.HTTPError as error:
            raw, status, headers = error.read(), error.code, error.headers
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return status, parsed, headers
