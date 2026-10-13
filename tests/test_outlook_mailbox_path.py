"""The admin-set mailbox is one Graph path segment (review finding B-int-2).

``/users/{mailbox}`` was built unquoted: a ``/``, ``?`` or ``#`` in the
value changed which Graph endpoint the app's credential was sent to.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx  # noqa: E402
import pytest  # noqa: E402

from agents.outlook_tools import OutlookClient  # noqa: E402


def _client(seen: list[httpx.Request], mailbox: str) -> OutlookClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "draft-1"})

    http = httpx.AsyncClient(base_url="https://graph.microsoft.com",
                             transport=httpx.MockTransport(handler))
    return OutlookClient(http, mailbox=mailbox, recipients=["team@example.com"])


@pytest.mark.parametrize("mailbox, segment", [
    ("agent@example.com?x=1", b"agent@example.com%3Fx%3D1"),
    ("agent@example.com#frag", b"agent@example.com%23frag"),
    ("../groups/abc", b"..%2Fgroups%2Fabc"),
    ("agent%40example.com", b"agent%2540example.com"),
])
async def test_a_mailbox_reaches_graph_as_one_encoded_segment(mailbox, segment):
    seen: list[httpx.Request] = []
    await _client(seen, mailbox).create_mail_draft("Subject", "Body")
    assert len(seen) == 1
    request = seen[0]
    assert request.url.raw_path == b"/v1.0/users/" + segment + b"/messages"
    assert request.url.query == b""


async def test_a_plain_address_is_sent_as_before():
    seen: list[httpx.Request] = []
    await _client(seen, "agent@example.com").create_mail_draft("Subject", "Body")
    assert seen[0].url.raw_path == b"/v1.0/users/agent@example.com/messages"
