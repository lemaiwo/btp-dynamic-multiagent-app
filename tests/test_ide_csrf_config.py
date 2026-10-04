"""CSRF protection on the IDE backend routes (approuter + ui5-ide standalone)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

IDE_ROUTES = {
    "approuter/xs-app.json": {"^/ui5ide/backend/(.*)$", "^/ide/api/(.*)$"},
    "ui5-ide/xs-app.json": {"^/backend/(.*)$"},
}

# (source, csrfProtection) of every other route, as of the IDE phase 1c commit
# (None = key absent).
BASELINE = {
    "approuter/xs-app.json": {
        "^/admin(/.*)?$": False,
        "^/healthz$": False,
        "^/.well-known/(agent-card|agent).json$": False,
        "^/a2a$": False,
        "^/ui5admin/resources/(.*)$": False,
        "^/ui5admin/backend/(.*)$": False,
        "^/ui5admin/?$": False,
        "^/ui5admin/(.*)$": False,
        "^/ui5ide/resources/(.*)$": False,
        "^/ui5ide/?$": False,
        "^/ui5ide/(.*)$": False,
        "^(.*)$": False,
    },
    "ui5-ide/xs-app.json": {
        "^/resources/(.*)$": None,
        "^(.*)$": None,
    },
}


def _routes(rel):
    data = json.loads((ROOT / rel).read_text())
    return {r["source"]: r.get("csrfProtection") for r in data["routes"]}


def test_ide_backend_routes_have_csrf_protection():
    for rel, sources in IDE_ROUTES.items():
        routes = _routes(rel)
        for s in sources:
            assert routes[s] is True, (rel, s)


def test_other_routes_unchanged():
    for rel, expected in BASELINE.items():
        routes = _routes(rel)
        others = {s: v for s, v in routes.items() if s not in IDE_ROUTES[rel]}
        assert others == expected, rel


# Review minor: the whole route of every IDE entry in the approuter, in order
# (the approuter takes the first match, so ``^/ui5ide/(.*)$`` must stay after
# the backend route). Any drift -- a scope, a destination, an auth type, a
# CSRF flag -- fails here and has to be made on purpose.
IDE_APPROUTER_ROUTES = [
    {
        "source": "^/ui5ide/resources/(.*)$",
        "target": "/1.120.50/resources/$1",
        "destination": "ui5",
        "authenticationType": "none",
        "csrfProtection": False,
    },
    {
        "source": "^/ui5ide/backend/(.*)$",
        "target": "/ide/api/$1",
        "destination": "pydantic-agent-backend",
        "authenticationType": "xsuaa",
        "scope": "$XSAPPNAME.developer",
        "csrfProtection": True,
    },
    {
        "source": "^/ide/api/(.*)$",
        "target": "/ide/api/$1",
        "destination": "pydantic-agent-backend",
        "authenticationType": "xsuaa",
        "scope": "$XSAPPNAME.developer",
        "csrfProtection": True,
    },
    {
        "source": "^/ui5ide/?$",
        "target": "/comagentide/index.html",
        "service": "html5-apps-repo-rt",
        "authenticationType": "xsuaa",
        "scope": "$XSAPPNAME.developer",
        "cacheControl": "no-cache, must-revalidate",
        "csrfProtection": False,
    },
    {
        "source": "^/ui5ide/(.*)$",
        "target": "/comagentide/$1",
        "service": "html5-apps-repo-rt",
        "authenticationType": "xsuaa",
        "scope": "$XSAPPNAME.developer",
        "cacheControl": "no-cache, must-revalidate",
        "csrfProtection": False,
    },
]


def test_ide_approuter_routes_snapshot():
    data = json.loads((ROOT / "approuter/xs-app.json").read_text())
    ide = [r for r in data["routes"]
           if r["source"].startswith(("^/ui5ide", "^/ide/"))]
    assert ide == IDE_APPROUTER_ROUTES
    # ... and they come before the catch-all, in this order.
    sources = [r["source"] for r in data["routes"]]
    positions = [sources.index(r["source"]) for r in IDE_APPROUTER_ROUTES]
    assert positions == sorted(positions)
    assert positions[-1] < sources.index("^(.*)$")
