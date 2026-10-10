"""``scripts/probe_sapnotes_auth.py`` prints the HOST of a redirect, never
the ``Location`` itself: a SAML redirect carries a request, a relay state or
a session id in its query, as the other probes already assume.

No network: the SAP host is a mock transport.

Run:  python -m pytest tests/test_probe_sapnotes_auth.py
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    """The script, without its ``load_dotenv`` (a local ``.env`` must not
    leak into the environment of the rest of the suite)."""
    spec = importlib.util.spec_from_file_location(
        "probe_sapnotes_auth_script", ROOT / "scripts" / "probe_sapnotes_auth.py")
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


probe = _load()


async def _attempt(location: str | None) -> str:
    def answer(request):
        headers = {"Location": location} if location is not None else {}
        return httpx.Response(302, headers=headers, content=b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        assert await probe.attempt(client, probe.PORTAL, ("u", "p")) == 302


@pytest.mark.parametrize("location, shown", [
    ("https://Accounts.SAP.com/saml2/idp/sso?SAMLRequest=s3cret&RelayState=r3lay",
     "accounts.sap.com"),
    ("https://user:pw@idp.example.test:8443/x?sid=s3cret", "idp.example.test"),
    ("/relative/path?sid=s3cret", ""),
    ("http://[not-a-host/x?sid=s3cret", ""),
    (None, ""),
])
async def test_only_the_host_of_a_redirect_is_printed(capsys, location, shown):
    await _attempt(location)
    out = capsys.readouterr().out
    (line,) = [x for x in out.splitlines() if x.startswith("  location")]
    assert line == f"  location    {shown}"
    assert "s3cret" not in out and "r3lay" not in out and "pw" not in out
