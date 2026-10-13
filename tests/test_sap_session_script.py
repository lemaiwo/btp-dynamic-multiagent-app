"""``scripts/sap_session.py refresh`` sends a session cookie and an admin
bearer token to ``--base-url``: only over https, or to the local app.

No browser, no network: the check runs before the login, and the login and
the POST are stubbed to prove that nothing is sent for a refused URL.

Run:  python -m pytest tests/test_sap_session_script.py
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    """The script, loaded without its ``load_dotenv`` (a local ``.env`` must
    not leak into the environment of the rest of the suite)."""
    spec = importlib.util.spec_from_file_location(
        "sap_session_script", ROOT / "scripts" / "sap_session.py")
    module = importlib.util.module_from_spec(spec)
    quiet = types.ModuleType("dotenv")
    quiet.load_dotenv = lambda *args, **kwargs: False
    real = sys.modules.get("dotenv")
    sys.modules["dotenv"] = quiet
    try:
        spec.loader.exec_module(module)
    finally:
        if real is None:
            sys.modules.pop("dotenv", None)
        else:
            sys.modules["dotenv"] = real
    return module


sap_session = _load()


@pytest.mark.parametrize("url", [
    "https://app.example.test",
    "https://app.example.test/",
    "https://app.example.test:8443/base",
    "http://127.0.0.1:7932",
    "http://localhost:7932/",
    "http://LOCALHOST",
])
def test_https_and_the_local_app_are_accepted(url):
    sap_session.check_base_url(url)


@pytest.mark.parametrize("url", [
    "http://app.example.test",
    "http://127.0.0.1.example.test",
    "http://localhost.example.test:7932",
    "http://user@127.0.0.1",
    "https://user:pw@app.example.test",
    "ftp://app.example.test",
    "app.example.test",
    "https://",
    "",
])
def test_anything_else_is_refused_without_echoing_it(url):
    with pytest.raises(SystemExit) as refused:
        sap_session.check_base_url(url)
    text = str(refused.value.code)
    assert "https" in text
    assert "example" not in text and "user@" not in text and "pw" not in text


async def test_refresh_refuses_before_logging_in_or_sending(monkeypatch):
    called: list[str] = []

    async def login(*args, **kwargs):
        called.append("login")
        return "cookie"

    monkeypatch.setattr(sap_session, "login", login)
    monkeypatch.setenv("SAP_DIALOG_USER", "u")
    monkeypatch.setenv("SAP_DIALOG_PWD", "p")
    with pytest.raises(SystemExit):
        await sap_session.refresh("http://app.example.test", "admin-token")
    assert called == []

