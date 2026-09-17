"""Shared fixtures for the Always Full test suite.

The HA-level tests here never touch the network. `AlwaysFullClient` is
replaced wholesale by `FakeAlwaysFullClient`, which serves the committed,
sanitised fixtures under `tests/fixtures/` and records what it was asked
for. That is deliberate: Tasks 1-3 already test the real client's signing,
parameter names and envelope handling as pure unit tests, so re-testing the
transport through a mocked HTTP layer here would only duplicate coverage
while coupling these tests to aiohttp's internals.

`aioresponses` is NOT used anywhere in this repo -- it raises
`TypeError: ... missing 'stream_writer'` against the aiohttp that
homeassistant 2026.9.2 pins. Tests that genuinely need HTTP mocking should
use pytest-homeassistant-custom-component's `aioclient_mock` fixture.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.alwaysfull.exceptions import AlwaysFullAuthError

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# The two bowls in `device_list_multi.json`. Both ids are synthetic.
DEVICE_ID = "aabbccddeeff"
SECOND_DEVICE_ID = "001122334455"

# The day-boundary test freezes time here. Under freezegun the NAIVE clock
# (`datetime.now()` with no tz) reads the frozen UTC wall time whatever the
# host's TZ is -- verified, not assumed -- so both the "reads the OS clock"
# and "reads UTC" mistakes produce FROZEN_UTC_DATE. Asserting a zone whose
# local date differs from that is therefore host-independent on its own:
# 18:00 UTC on the 16th is already 03:00 on the 17th in Tokyo.
FROZEN_INSTANT = "2026-09-16T18:00:00+00:00"
FROZEN_ZONE = "Asia/Tokyo"
FROZEN_LOCAL_DATE = "2026-09-17"
FROZEN_UTC_DATE = "2026-09-16"

# DST-free zones, so each offset is a year-round constant.
_ZONE_CANDIDATES = (("Asia/Tokyo", 9), ("Pacific/Honolulu", -10))


def zone_unlike_host() -> tuple[str, int]:
    """Return a `(zone, utc_offset_hours)` pair whose offset differs from this host's.

    Used by the tests that run on the REAL clock, where the host's zone can
    leak into the result. Hard-coding a zone makes those tests
    host-dependent in both directions: on a runner that happens to sit at
    UTC+9, asserting "the offset is 9" passes for an implementation reading
    the OS clock (a false green), while asserting "the offset is not the
    host's" fails on entirely correct code (worse). Choosing at runtime
    keeps the asserted value a fixed known constant while guaranteeing it
    cannot have come from the host clock. The host can match at most one
    candidate, so this always finds one.
    """
    host_utc_offset = datetime.now().astimezone().utcoffset()
    assert host_utc_offset is not None
    host_offset = int(host_utc_offset.total_seconds() / 3600)

    for zone, offset in _ZONE_CANDIDATES:
        if offset != host_offset:
            return zone, offset

    msg = f"No candidate zone differs from the host offset {host_offset}"
    raise RuntimeError(msg)


def load_fixture_data(name: str) -> Any:
    """Return the `data` member of the committed `name`.json envelope."""
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())["data"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let every test in this suite load `custom_components.alwaysfull`.

    Home Assistant refuses to load custom integrations in tests unless this
    fixture is requested; making it autouse means no test can silently be
    testing a *built-in* integration that happens to share our domain.
    """
    return


class FakeAlwaysFullClient:
    """Drop-in stand-in for `AlwaysFullClient` backed by the JSON fixtures.

    Every method records its arguments so tests can assert on the exact
    calls the coordinator made (including the day-snapped drinking-log
    range and the once-every-N-polls notify-config fetch).
    """

    def __init__(
        self,
        session: Any = None,
        *,
        token: str = "",
        tz_offset_hours: int | None = None,
    ) -> None:
        """Mirror the real client's constructor signature exactly."""
        self.session = session
        self.token = token
        self.tz_offset_hours = tz_offset_hours
        # Kept separately because the coordinator re-syncs `tz_offset_hours`
        # on every poll; without this, a test could not tell whether the
        # value it sees came from the constructor or from that re-sync.
        self.constructed_tz_offset_hours = tz_offset_hours

        # Recorded calls.
        self.login_calls: list[str] = []
        self.device_list_calls = 0
        self.device_config_calls: list[str] = []
        self.drinking_log_calls: list[tuple[str, int, str, str]] = []
        self.notify_log_calls: list[str] = []
        self.notify_config_calls = 0

        # Injectable failures. `_remaining` of None means "raise forever".
        self.device_list_error: Exception | None = None
        self.device_list_error_remaining: int | None = None
        self.login_error: Exception | None = None

        # Injectable response, so a test can prove a re-read really re-read.
        self.device_config_override: dict[str, Any] | None = None

        # Per-device drinking totals, so a multi-device test can prove each
        # bowl got ITS OWN total rather than the first bowl's.
        self.drinking_totals: dict[str, int] = {DEVICE_ID: 903, SECOND_DEVICE_ID: 250}

    def fail_device_list(self, error: Exception, times: int | None = None) -> None:
        """Make `device_list()` raise `error`, for `times` calls (None = forever)."""
        self.device_list_error = error
        self.device_list_error_remaining = times

    async def login(self, email: str, password: str) -> str:
        """Record the login attempt and return a fresh token."""
        # The password is deliberately NOT recorded: no test needs it, and a
        # recorded secret is a secret waiting to end up in a failure dump.
        assert password
        self.login_calls.append(email)
        if self.login_error is not None:
            raise self.login_error
        self.token = "FRESH-TOKEN"
        return self.token

    async def device_list(self) -> Any:
        """Return the paginated device-list envelope from the fixtures.

        Serves `device_list_multi.json`, NOT the single-row live capture:
        with one row, every assertion about the per-device loop passes
        identically for an implementation that only ever handles `rows[0]`,
        so a user's second bowl could silently produce no entities at all
        with the suite still green. The multi fixture keeps the captured row
        verbatim and adds a second synthetic bowl plus a half-provisioned
        row with a null `deviceId`. `device_list.json` stays pristine for
        the transport tests.
        """
        self.device_list_calls += 1
        if self.device_list_error is not None and self.device_list_error_remaining != 0:
            if self.device_list_error_remaining is not None:
                self.device_list_error_remaining -= 1
            raise self.device_list_error
        return copy.deepcopy(load_fixture_data("device_list_multi"))

    async def device_config(self, device_id: str) -> Any:
        """Return the device-config object for `device_id` from the fixtures.

        `devNo` is echoed back as the requested id so a test can prove each
        bowl was given ITS OWN config, not another bowl's.
        """
        self.device_config_calls.append(device_id)
        config = copy.deepcopy(self.device_config_override or load_fixture_data("device_config"))
        config["devNo"] = device_id
        return config

    async def drinking_log(self, device_id: str, units: int, start: str, end: str) -> Any:
        """Return one row for the requested day, plus one unrelated day.

        Echoing the *requested* date back keeps this deterministic no matter
        what the calendar says when the suite runs, while the extra row
        proves the coordinator picks today's row rather than the first or
        last row the server happened to send.
        """
        self.drinking_log_calls.append((device_id, units, start, end))
        return [
            {"drinkingDate": "1999-12-31", "totalCapacity": 4242},
            {"drinkingDate": start, "totalCapacity": self.drinking_totals.get(device_id, 0)},
        ]

    async def notify_log(self, device_id: str, page_size: int = 20) -> Any:
        """Return the paginated notification-log envelope from the fixtures."""
        self.notify_log_calls.append(device_id)
        return copy.deepcopy(load_fixture_data("notify_log"))

    async def notify_config(self) -> Any:
        """Return the account-level notification config from the fixtures."""
        self.notify_config_calls += 1
        return copy.deepcopy(load_fixture_data("notify_config"))


@pytest.fixture
def mock_api() -> Generator[FakeAlwaysFullClient]:
    """Patch the integration's client with a fixture-backed fake."""
    client = FakeAlwaysFullClient()
    with patch(
        "custom_components.alwaysfull.AlwaysFullClient",
        autospec=False,
        side_effect=lambda session, **kwargs: _configure(client, session, **kwargs),
    ):
        yield client


@pytest.fixture
def mock_api_auth_fails(mock_api: FakeAlwaysFullClient) -> FakeAlwaysFullClient:
    """A client whose token is rejected AND whose re-login is rejected too."""
    mock_api.fail_device_list(AlwaysFullAuthError("Token expired"))
    mock_api.login_error = AlwaysFullAuthError("Account or password error")
    return mock_api


def _configure(
    client: FakeAlwaysFullClient,
    session: Any,
    *,
    token: str = "",
    tz_offset_hours: int | None = None,
) -> FakeAlwaysFullClient:
    """Record the constructor arguments the integration used on the fake."""
    client.session = session
    client.token = token
    client.tz_offset_hours = tz_offset_hours
    client.constructed_tz_offset_hours = tz_offset_hours
    return client
