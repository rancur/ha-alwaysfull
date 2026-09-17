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
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.alwaysfull.exceptions import AlwaysFullAuthError

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
        """Return the paginated device-list envelope from the fixtures."""
        self.device_list_calls += 1
        if self.device_list_error is not None and self.device_list_error_remaining != 0:
            if self.device_list_error_remaining is not None:
                self.device_list_error_remaining -= 1
            raise self.device_list_error
        return copy.deepcopy(load_fixture_data("device_list"))

    async def device_config(self, device_id: str) -> Any:
        """Return the device-config object from the fixtures."""
        self.device_config_calls.append(device_id)
        if self.device_config_override is not None:
            return copy.deepcopy(self.device_config_override)
        return copy.deepcopy(load_fixture_data("device_config"))

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
            {"drinkingDate": start, "totalCapacity": 903},
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
