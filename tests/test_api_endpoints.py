"""Tests for the typed Always Full API endpoint methods.

These build on the `AlwaysFullClient.request()` transport verified by
`test_api_signing.py`. Endpoint methods are thin wrappers over `request()`,
so these tests assert two things per method: the exact wire shape sent
(path, HTTP method, and -- critically -- the device-id parameter name,
which is NOT uniform across endpoints) and that the vendor's `data` payload
is passed back to the caller unmodified.

Dependency note: the task brief's illustrative tests use `aioresponses`
against a real `aiohttp.ClientSession`. In this environment `aioresponses`
0.7.9 (the latest available; requirements-dev.txt pins 0.7.8, which has the
same problem) is incompatible with `aiohttp` 3.14.3 (the version
`homeassistant==2026.9.2` pins): mocking any request raises
`TypeError: ClientResponse.__init__() missing 1 required keyword-only
argument: 'stream_writer'` regardless of what is being tested -- reproduced
with a two-line `aioresponses` GET against `example.com`. That is exactly
the "test suite self-poisoning" failure mode this project's own history
warns about (see the design doc's CI notes): a broken dependency would fail
every test in this file for a reason that has nothing to do with the code
under test. So, like `test_api_signing.py`, this file uses a small hand-rolled
fake aiohttp session that records exactly what `request()` would have sent,
with no real I/O and no incompatible dependency.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Self

import pytest

from custom_components.alwaysfull.api import AlwaysFullClient
from custom_components.alwaysfull.const import API_BASE
from custom_components.alwaysfull.exceptions import (
    AlwaysFullAuthError,
    AlwaysFullCredentialsError,
    AlwaysFullError,
    AlwaysFullRateLimitError,
)

FIX = pathlib.Path(__file__).parent / "fixtures"


def fx(name: str) -> dict[str, Any]:
    """Load a committed fixture by filename."""
    return json.loads((FIX / name).read_text())


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


def _client(
    payload: dict[str, Any] | None = None, *, token: str = "tok", status: int = 200
) -> tuple[AlwaysFullClient, _FakeSession]:
    session = _FakeSession(payload, status)
    client = AlwaysFullClient(session, token=token, tz_offset_hours=0)
    return client, session


# --- login -------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_returns_token():
    client, _ = _client({"code": "200", "msg": "success", "data": "TOKEN123"}, token="")
    assert await client.login("user@example.com", "pw") == "TOKEN123"


@pytest.mark.asyncio
async def test_login_hashes_password_twice():
    # md5(md5("pw")), computed via:
    #   python -c "import hashlib;m=lambda s:hashlib.md5(s.encode()).hexdigest();print(m(m('pw')))"
    expected = "d7fe7a84e8373b67aec2832ca6f9d477"
    client, session = _client({"code": "200", "msg": "success", "data": "T"}, token="")
    await client.login("user@example.com", "pw")
    body = session.calls[0]["json"]
    assert body["password"] == expected
    assert body["account"] == "user@example.com"
    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE}/app/user/login"


@pytest.mark.asyncio
async def test_login_stores_token_on_client():
    client, _ = _client({"code": "200", "msg": "success", "data": "TOKEN123"}, token="")
    await client.login("user@example.com", "pw")
    assert client.token == "TOKEN123"


# --- shared auth failure -------------------------------------------------
#
# Verified against the live API: a real account with a deliberately wrong
# password and an email with no account at all BOTH answer
# `652 "Invalid email address or password."`. An earlier reading of 602 as
# "unknown email" and 652 as "known email, wrong password" is DISPROVEN --
# nothing may try to tell those two cases apart.
#
# These assert the exact class, not `AlwaysFullError`: every one of these is
# an `AlwaysFullError` by inheritance, so `pytest.raises(AlwaysFullError)`
# would pass no matter which branch the code took. That is precisely how the
# original 602 test passed while a mistyped password reached users as
# "unexpected error".


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["652", "602"])
async def test_rejected_credentials_raise_credentials_rejected(code: str):
    client, _ = _client(
        {"code": code, "msg": "Invalid email address or password.", "data": None}, token=""
    )
    with pytest.raises(AlwaysFullCredentialsError):
        await client.login("user@example.com", "wrong")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["652", "602"])
async def test_a_credential_code_from_a_token_bearing_call_is_a_session_failure(code: str):
    """The SAME code means something different away from the login endpoint.

    `652` from `/app/user/login` means the password was rejected. `652`
    from a call that carried a token means the SESSION was rejected -- and
    against a single-session vendor whose owner signs us out every time
    they open their phone app, that is routine and heals with one
    re-login.

    Classifying it as a credential rejection regardless of endpoint is
    what bricked a live install: a transient `652` on a poll skipped the
    re-login that would have fixed it, every entity went unavailable, and
    the config entry still said `loaded`. The stored credentials were
    correct throughout.
    """
    client, _ = _client(
        {"code": code, "msg": "Invalid email address or password.", "data": None}
    )
    with pytest.raises(AlwaysFullAuthError) as err:
        await client.device_list()
    assert not isinstance(err.value, AlwaysFullCredentialsError)


@pytest.mark.asyncio
async def test_expired_token_raises_auth_error_but_not_credentials_rejected():
    """651 is an auth error, and stays distinguishable from a bad password.

    The coordinator answers the two differently in principle -- one silent
    re-login for an expired token, straight to reauth for wrong credentials
    -- so collapsing them into one class would throw that away.
    """
    client, _ = _client({"code": "651", "msg": "token expired", "data": None}, token="x")
    with pytest.raises(AlwaysFullAuthError) as err:
        await client.device_list()
    assert not isinstance(err.value, AlwaysFullCredentialsError)


@pytest.mark.asyncio
async def test_unclassified_code_is_a_plain_error_and_nothing_more():
    """An unknown code must not be guessed into a reauth the user cannot fix.

    Asserted as an EXACT type, not as "an `AlwaysFullError` that is not an
    `AlwaysFullAuthError`". That weaker pair still admits
    `AlwaysFullRateLimitError`, which the config flow answers with
    "cannot connect" rather than "unknown error" -- so a mutation
    classifying every unclassified code as a rate limit would change what
    the user is told while the test stayed green. Verified by making
    exactly that mutation: it passes the old assertion and fails this one.
    """
    client, _ = _client({"code": "500", "msg": "server exploded", "data": None}, token="x")
    with pytest.raises(AlwaysFullError) as err:
        await client.device_list()
    assert type(err.value) is AlwaysFullError


# --- rate limiting -------------------------------------------------------
#
# The vendor signals a rate limit in the ENVELOPE CODE, not with HTTP 429.
# VERIFIED: eight logins in a few seconds answer
# `603 "Too many requests, please try again later."` with HTTP 200.


@pytest.mark.asyncio
async def test_a_rate_limited_login_is_a_rate_limit_not_a_generic_error():
    """603 from the login endpoint is the vendor asking us to slow down.

    Asserted as an EXACT type. `AlwaysFullRateLimitError` is an
    `AlwaysFullError`, so `pytest.raises(AlwaysFullError)` would pass for
    the generic-error branch that shipped this bug -- which is precisely
    how every rate limit reached users as "unexpected error".
    """
    client, _ = _client(
        {"code": "603", "msg": "Too many requests, please try again later.", "data": None},
        token="",
    )
    with pytest.raises(AlwaysFullError) as err:
        await client.login("user@example.com", "pw")
    assert type(err.value) is AlwaysFullRateLimitError


@pytest.mark.asyncio
async def test_a_rate_limited_read_is_a_rate_limit_and_never_an_auth_error():
    """603 on a token-bearing endpoint must not look like an auth problem.

    A rate limit mapped anywhere into the auth family pushes the user into
    a reauth flow that succeeds and changes nothing, and the next poll is
    rate-limited again: a loop they cannot escape.
    """
    client, _ = _client(
        {"code": "603", "msg": "Too many requests, please try again later.", "data": None}
    )
    with pytest.raises(AlwaysFullRateLimitError) as err:
        await client.device_list()
    assert not isinstance(err.value, AlwaysFullAuthError)


@pytest.mark.asyncio
async def test_http_429_is_still_a_rate_limit():
    """The transport-level mapping stays, even though this vendor never uses it.

    It has never fired against the live service -- every observed rate
    limit arrived as envelope code 603 with HTTP 200 -- but it costs
    nothing and another deployment of the same app may answer this way.
    """
    client, _ = _client({"code": "200", "msg": "ok", "data": {}}, status=429)
    with pytest.raises(AlwaysFullRateLimitError):
        await client.device_list()


# --- device_list ---------------------------------------------------------


@pytest.mark.asyncio
async def test_device_list_sends_pagination_params_and_returns_envelope():
    fixture = fx("device_list.json")
    client, session = _client(fixture)

    result = await client.device_list()

    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == f"{API_BASE}/app/device/list"
    params = session.calls[0]["params"]
    # Integers are JSON-encoded for the GET query by request(), so these
    # arrive as the strings "1"/"50", not ints.
    assert params["pageNum"] == "1"
    assert params["pageSize"] == "50"

    # The pagination envelope is passed through unmodified -- callers that
    # need hasNext/totalSize still have them.
    assert result == fixture["data"]
    assert result["hasNext"] is False
    assert result["data"][0]["deviceId"] == "aabbccddeeff"


# --- device_config ---------------------------------------------------------


@pytest.mark.asyncio
async def test_device_config_sends_device_id_param():
    fixture = fx("device_config.json")
    client, session = _client(fixture)

    result = await client.device_config("aabbccddeeff")

    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == f"{API_BASE}/app/device/config"
    assert session.calls[0]["params"]["deviceId"] == "aabbccddeeff"
    assert result == fixture["data"]
    assert result["capacity"] == 378541


# --- drinking_log ---------------------------------------------------------


@pytest.mark.asyncio
async def test_drinking_log_formats_dates_and_returns_bare_array():
    fixture = fx("drinking_log.json")
    client, session = _client(fixture)

    result = await client.drinking_log("aabbccddeeff", 1, "2026-09-10", "2026-09-17")

    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == f"{API_BASE}/app/drinking/log"
    params = session.calls[0]["params"]
    assert params["deviceId"] == "aabbccddeeff"
    assert params["units"] == "1"
    assert params["startTime"] == "2026-09-10 00:00:00"
    assert params["endTime"] == "2026-09-17 23:59:59"

    # Bare array, NOT a pagination envelope -- this asymmetry is real.
    assert isinstance(result, list)
    assert result == fixture["data"]
    # 2026-09-16 (second-to-last row; the last row is the following day at 0).
    assert result[-2]["drinkingDate"] == "2026-09-16"
    assert result[-2]["totalCapacity"] == 903


# --- notify_config / save_notify_config -------------------------------------


@pytest.mark.asyncio
async def test_notify_config_returns_both_notify_arrays():
    fixture = fx("notify_config.json")
    client, session = _client(fixture)

    result = await client.notify_config()

    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == f"{API_BASE}/app/notify/getConfig"
    assert result == fixture["data"]
    assert "notifyItems" in result
    assert "notifyList" in result
    assert result["notifyItems"] == result["notifyList"]


@pytest.mark.asyncio
async def test_save_notify_config_roundtrips_whole_object_unchanged():
    config = fx("notify_config.json")["data"]
    # A realistic caller: fetch, flip one field, save the whole thing back.
    mutated = {**config, "isTextNotify": 0}
    client, session = _client({"code": "200", "msg": "success", "data": None})

    await client.save_notify_config(mutated)

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE}/app/notify/saveConfig"
    sent_body = session.calls[0]["json"]
    # Every key/value from the caller's object must survive verbatim --
    # both notifyItems AND notifyList, not a payload reconstructed from
    # just one of them.
    for key, value in mutated.items():
        assert sent_body[key] == value
    assert sent_body["notifyItems"] == config["notifyItems"]
    assert sent_body["notifyList"] == config["notifyList"]
    assert sent_body["isTextNotify"] == 0


# --- notify_log ---------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_log_sends_device_id_and_default_page_size():
    fixture = fx("notify_log.json")
    client, session = _client(fixture)

    result = await client.notify_log("aabbccddeeff")

    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == f"{API_BASE}/app/notify/getNotifyLog"
    params = session.calls[0]["params"]
    assert params["deviceId"] == "aabbccddeeff"
    assert params["pageNum"] == "1"
    assert params["pageSize"] == "20"
    assert result == fixture["data"]
    assert result["totalSize"] == 13


@pytest.mark.asyncio
async def test_notify_log_honours_custom_page_size():
    fixture = fx("notify_log.json")
    client, session = _client(fixture)

    await client.notify_log("aabbccddeeff", page_size=5)

    assert session.calls[0]["params"]["pageSize"] == "5"


# --- writers: the devNo/deviceId trap, one test per method ------------------


@pytest.mark.asyncio
async def test_set_flush_config_uses_dev_no():
    client, session = _client({"code": "200", "msg": "success", "data": None})

    await client.set_flush_config("aabbccddeeff", cleanCycle=1800, cleanTime=25)

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE}/app/device/flushConfig"
    body = session.calls[0]["json"]
    assert body["devNo"] == "aabbccddeeff"
    assert "deviceId" not in body
    assert body["cleanCycle"] == 1800
    assert body["cleanTime"] == 25


@pytest.mark.asyncio
async def test_set_sleep_config_uses_dev_no():
    client, session = _client({"code": "200", "msg": "success", "data": None})

    await client.set_sleep_config("aabbccddeeff", sleepStart=1320, sleepEnd=360)

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE}/app/device/sleepConfig"
    body = session.calls[0]["json"]
    assert body["devNo"] == "aabbccddeeff"
    assert "deviceId" not in body
    assert body["sleepStart"] == 1320
    assert body["sleepEnd"] == 360


@pytest.mark.asyncio
async def test_set_filter_config_uses_dev_no_and_writes_filter_capacity_wire_name():
    client, session = _client({"code": "200", "msg": "success", "data": None})

    await client.set_filter_config("aabbccddeeff", filterCapacity=378541)

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE}/app/device/filterConfig"
    body = session.calls[0]["json"]
    assert body["devNo"] == "aabbccddeeff"
    assert "deviceId" not in body
    # Read back as `capacity`, but MUST be written as `filterCapacity`.
    assert body["filterCapacity"] == 378541
    assert "capacity" not in body


@pytest.mark.asyncio
async def test_set_maintenance_config_uses_dev_no():
    client, session = _client({"code": "200", "msg": "success", "data": None})

    await client.set_maintenance_config("aabbccddeeff", deviceCanUseTime=864000)

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE}/app/device/maintenanceConfig"
    body = session.calls[0]["json"]
    assert body["devNo"] == "aabbccddeeff"
    assert "deviceId" not in body
    assert body["deviceCanUseTime"] == 864000


@pytest.mark.asyncio
async def test_set_water_config_uses_dev_no():
    client, session = _client({"code": "200", "msg": "success", "data": None})

    await client.set_water_config("aabbccddeeff", units=1, dayMinWater=2000, dayMaxWater=7500)

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE}/app/device/waterConfig"
    body = session.calls[0]["json"]
    assert body["devNo"] == "aabbccddeeff"
    assert "deviceId" not in body
    # units must be echoable -- the writer sends whatever it's handed.
    assert body["units"] == 1
    assert body["dayMinWater"] == 2000
    assert body["dayMaxWater"] == 7500


@pytest.mark.asyncio
async def test_set_log_config_uses_dev_no():
    client, session = _client({"code": "200", "msg": "success", "data": None})

    await client.set_log_config("aabbccddeeff", logState=1)

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE}/app/device/logConfig"
    body = session.calls[0]["json"]
    assert body["devNo"] == "aabbccddeeff"
    assert "deviceId" not in body
    assert body["logState"] == 1


@pytest.mark.asyncio
async def test_set_units_uses_device_id_not_dev_no():
    client, session = _client({"code": "200", "msg": "success", "data": None})

    await client.set_units("aabbccddeeff", 1)

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE}/app/device/setUnits"
    body = session.calls[0]["json"]
    assert body["deviceId"] == "aabbccddeeff"
    assert "devNo" not in body
    assert body["units"] == 1


@pytest.mark.asyncio
async def test_set_device_type_uses_dev_no():
    client, session = _client({"code": "200", "msg": "success", "data": None})

    await client.set_device_type("aabbccddeeff", 0)

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE}/app/device/setType"
    body = session.calls[0]["json"]
    assert body["devNo"] == "aabbccddeeff"
    assert "deviceId" not in body
    # Sent verbatim -- the 0="9-inch"/1="7-inch" inversion is a caller concern.
    assert body["deviceType"] == 0


@pytest.mark.asyncio
async def test_reset_filter_uses_dev_no():
    client, session = _client({"code": "200", "msg": "success", "data": None})

    await client.reset_filter("aabbccddeeff")

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE}/app/device/reset/filter"
    body = session.calls[0]["json"]
    assert body["devNo"] == "aabbccddeeff"
    assert "deviceId" not in body
