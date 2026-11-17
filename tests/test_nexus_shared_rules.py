"""The agent register reuses the app's rules instead of copying them.

``signs_in_as_user`` is the one rule of "this destination signs in as the
signed-in user", shared by the OData path (``destination_auth``) and the
register client; the httpx log filter is reached through public names.
"""
from __future__ import annotations

import ast
import dataclasses
import time
from pathlib import Path

import pytest

from agents.destination import Destination

ROOT = Path(__file__).resolve().parents[1]


def _dest(**kw) -> Destination:
    base = Destination(url="https://register.example.test",
                       headers={"Authorization": "Bearer minted"},
                       expires_at=time.monotonic() + 600,
                       auth_type="OAuth2JWTBearer", per_user=True)
    return dataclasses.replace(base, **kw)


@pytest.mark.parametrize("auth_type", ["OAuth2JWTBearer", "OAuth2UserTokenExchange",
                                       "OAuth2SAMLBearerAssertion", " OAuth2JWTBearer "])
def test_signs_in_as_user_for_the_user_propagating_types(auth_type):
    from agents.destination_auth import signs_in_as_user
    assert signs_in_as_user(_dest(auth_type=auth_type)) is True


@pytest.mark.parametrize("change", [
    {"per_user": False},
    {"auth_type": "BasicAuthentication"},
    {"auth_type": "OAuth2ClientCredentials"},
    {"auth_type": ""},
    {"system_user": "TECHUSER"},
])
def test_signs_in_as_user_refuses_everything_else(change):
    from agents.destination_auth import signs_in_as_user
    assert signs_in_as_user(_dest(**change)) is False


def _imported_names(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text())
    return [(node.module or "", alias.name)
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names]


def test_nexus_package_imports_no_private_name_of_another_module():
    for path in sorted((ROOT / "agents" / "nexus").glob("*.py")):
        for module, name in _imported_names(path):
            if module.startswith("agents.nexus"):
                continue
            assert not name.startswith("_"), f"{path.name} imports {module}.{name}"


def test_bitbucket_log_filter_has_public_names():
    from agents import bitbucket_tools
    assert bitbucket_tools.install_request_log_filter is bitbucket_tools._install_once
    assert bitbucket_tools.request_log_quiet is bitbucket_tools._sending
