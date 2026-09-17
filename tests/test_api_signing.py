"""Golden-vector tests for AlwaysFullClient request signing.

These are pure unit tests: no real aiohttp I/O, no Home Assistant. The
signing golden vectors were computed from the canonical string and
independently verified against the vendor's live production server (a
signed request returned a business-logic error rather than a signature
error, proving the server accepted the signature). They are ground truth —
if the implementation does not reproduce them, the implementation is wrong.

The `request()` tests use a tiny fake aiohttp session (`_FakeSession`) that
records exactly what would have been sent, rather than doing any network
I/O — still a pure unit test, just one that can also assert on what
`request()` hands to `aiohttp`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Self

import pytest

from custom_components.alwaysfull import api as api_module
from custom_components.alwaysfull.api import AlwaysFullClient, _truncate_offset_to_hours
from custom_components.alwaysfull.const import SIGN_SECRET

BODY = {
    "account": "user@example.com",
    "password": "d41d8cd98f00b204e9800998ecf8427e",
    "appId": "appBiz",
    "appType": "android",
    "appVersion": "1.2.29",
    "timeZone": -7,
}


class _FakeResponse:
    """Minimal async-context-manager response, standing in for aiohttp's."""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type: str | None = None) -> dict[str, Any]:
        return self._payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Records every `.request()` call instead of doing network I/O."""

    def __init__(self, payload: dict[str, Any] | None = None, status: int = 200) -> None:
        self.calls: list[dict[str, Any]] = []
        self._status = status
        self._payload = payload or {"code": "200", "msg": "ok", "data": {}}

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResponse(self._status, self._payload)


def test_sign_golden_vector_no_token():
    c = AlwaysFullClient(session=None, token="")
    assert c.sign(BODY, 1700000000000) == "09e59d24d687126cda92550f23c5c3f9"


def test_sign_golden_vector_with_token():
    c = AlwaysFullClient(session=None, token="tok123")
    assert c.sign(BODY, 1700000000000) == "8f62367db288f5479ce5936207fda3c0"


def test_sign_skips_none_values():
    c = AlwaysFullClient(session=None, token="")
    a = c.sign({**BODY, "extra": None}, 1700000000000)
    assert a == "09e59d24d687126cda92550f23c5c3f9"


def test_sign_serialises_non_strings_as_compact_json():
    c = AlwaysFullClient(session=None, token="")

    # Pins the exact canonical string for a body mixing a string, an int, a
    # bool and a nested dict, so a change to the JSON separators or to the
    # sort-ascending key order fails this test. The dict literal is
    # deliberately declared in an order that differs from sorted order
    # (meta, enabled, count, account instead of account, count, enabled,
    # meta) so that a regression which dropped the sorted() call in
    # canonical() and iterated insertion order instead would produce a
    # different string and fail this assertion, rather than coincidentally
    # matching it.
    body = {
        "meta": {"b": 2, "a": 1},
        "enabled": True,
        "count": 3,
        "account": "user@example.com",
    }
    expected = f'account=user@example.com&count=3&enabled=true&meta={{"b":2,"a":1}}&0{SIGN_SECRET}'
    assert c.canonical(body, 0) == expected

    # timeZone is an int; it must appear as -7 with no spaces.
    assert "timeZone=-7&" in c.canonical({"timeZone": -7}, 0)


def test_truncate_offset_to_hours_truncates_toward_zero():
    """A negative half-hour(-or-finer) offset must truncate toward zero.

    Floor division (`// 3600`) would turn -3:30 into -4; the correct value
    is -3.
    """
    assert _truncate_offset_to_hours(timedelta(hours=-3, minutes=-30)) == -3
    assert _truncate_offset_to_hours(timedelta(hours=3, minutes=30)) == 3
    assert _truncate_offset_to_hours(timedelta(hours=-7)) == -7


@pytest.mark.asyncio
async def test_explicit_tz_offset_overrides_os_value(monkeypatch: pytest.MonkeyPatch):
    """An explicitly-passed tz_offset_hours must win over the OS-derived value."""
    # Sabotage the OS fallback with an unmistakably different value: if
    # request() ever falls back to it despite the explicit override, this
    # test will catch it.
    monkeypatch.setattr(api_module, "_local_time_zone_offset_hours", lambda: 99)
    session = _FakeSession()
    client = AlwaysFullClient(session=session, token="", tz_offset_hours=-3)

    await client.request("POST", "/some/path")

    sent_body = session.calls[0]["json"]
    assert sent_body["timeZone"] == -3

    # What was signed must be exactly what was sent.
    timestamp = int(session.calls[0]["headers"]["timestamp"])
    assert session.calls[0]["headers"]["sign"] == client.sign(sent_body, timestamp)


@pytest.mark.asyncio
async def test_no_explicit_tz_offset_falls_back_to_os_value(monkeypatch: pytest.MonkeyPatch):
    """With no explicit override, request() must still work via the OS fallback."""
    monkeypatch.setattr(api_module, "_local_time_zone_offset_hours", lambda: 5)
    session = _FakeSession()
    client = AlwaysFullClient(session=session, token="")

    await client.request("POST", "/some/path")

    assert session.calls[0]["json"]["timeZone"] == 5


@pytest.mark.asyncio
async def test_none_valued_params_are_stripped_from_json_body_and_signature(
    monkeypatch: pytest.MonkeyPatch,
):
    """A None-valued caller param must not appear in the sent body, and the
    signed key set must exactly match the sent key set."""
    monkeypatch.setattr(api_module, "_local_time_zone_offset_hours", lambda: -7)
    session = _FakeSession()
    client = AlwaysFullClient(session=session, token="tok123")

    await client.request(
        "POST",
        "/some/path",
        params={"account": "user@example.com", "extra": None},
    )

    sent_body = session.calls[0]["json"]
    assert None not in sent_body.values()
    assert "extra" not in sent_body
    assert set(sent_body.keys()) == {"account", "appId", "appType", "appVersion", "timeZone"}

    timestamp = int(session.calls[0]["headers"]["timestamp"])
    assert session.calls[0]["headers"]["sign"] == client.sign(sent_body, timestamp)


@pytest.mark.asyncio
async def test_none_valued_params_are_stripped_from_get_query(monkeypatch: pytest.MonkeyPatch):
    """The same None-stripping must apply to the GET query string."""
    monkeypatch.setattr(api_module, "_local_time_zone_offset_hours", lambda: -7)
    session = _FakeSession()
    client = AlwaysFullClient(session=session, token="")

    await client.request("GET", "/some/path", params={"deviceId": "abc", "cursor": None})

    query = session.calls[0]["params"]
    assert None not in query.values()
    assert "cursor" not in query
    assert query["deviceId"] == "abc"
