"""The invoke API's entries in ``xs-security.json``.

A user calls through the ``Agent Invoker`` role collection; an application
that cannot exchange its user's token here calls with a client-credentials
token of a binding or service key of this XSUAA instance, which carries the
``invoke`` scope only because the instance's own client is granted it as an
authority. Nothing else is granted that way: every other scope stays with a
user (role collections) or a named service instance.

Run:  .venv/bin/python -m pytest tests/test_xs_security_invoke.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
XS = json.loads((ROOT / "xs-security.json").read_text(encoding="utf-8"))


def _by_name(section: str) -> dict[str, dict]:
    return {entry["name"]: entry for entry in XS[section]}


def test_the_own_client_is_granted_invoke_and_nothing_else():
    assert XS["authorities"] == ["$XSAPPNAME.invoke"]
    assert "$XSAPPNAME.invoke" in _by_name("scopes")


def test_a_client_token_can_be_issued():
    assert "client_credentials" in XS["oauth2-configuration"]["grant-types"]


def test_users_still_get_invoke_through_the_role_collection_only():
    templates = _by_name("role-templates")
    collections = _by_name("role-collections")
    assert templates["AgentInvoker"]["scope-references"] == ["$XSAPPNAME.invoke"]
    assert collections["Agent Invoker"]["role-template-references"] == [
        "$XSAPPNAME.AgentInvoker"]
