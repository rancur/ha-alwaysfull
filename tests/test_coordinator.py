"""Coordinator polling shape, error mapping and post-write refresh."""

from __future__ import annotations

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alwaysfull.const import DOMAIN, NOTIFY_CONFIG_EVERY_N_POLLS
from custom_components.alwaysfull.coordinator import AlwaysFullCoordinator
from custom_components.alwaysfull.exceptions import (
    AlwaysFullAuthError,
    AlwaysFullError,
    AlwaysFullRateLimit,
)

from .conftest import FakeAlwaysFullClient, load_fixture_data

DEVICE_ID = "aabbccddeeff"


async def _setup(hass: HomeAssistant) -> AlwaysFullCoordinator:
    """Load an entry and hand back its coordinator."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": "T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry.runtime_data


async def test_one_device_list_call_plus_per_device_reads(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """One list call carries full state; details are never re-fetched per device."""
    coordinator = await _setup(hass)

    assert mock_api.device_list_calls == 1
    assert mock_api.device_config_calls == [DEVICE_ID]
    assert mock_api.notify_log_calls == [DEVICE_ID]

    bowl = coordinator.data[DEVICE_ID]
    assert bowl.state.device_name == "Test Bowl"
    assert bowl.state.bowl_size_inches == 9
    assert bowl.config.flush_interval_minutes == 60
    assert bowl.water_today == 903
    assert bowl.firmware_version == "3.6.2"
    assert len(bowl.notifications) == 13


async def test_drinking_log_is_snapped_to_today_in_ha_time_zone(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, freezer: FrozenDateTimeFactory
) -> None:
    """Today's log window is a single local day, in HA's zone, not UTC's.

    Time is frozen at an instant where the answer differs by zone: 18:00
    UTC on the 16th is still the 16th in UTC and in the Americas, but
    already the 17th in Tokyo. A hard-coded 2026-09-17 therefore cannot be
    produced by a UTC-based or OS-clock-based implementation.
    """
    freezer.move_to("2026-09-16T18:00:00+00:00")
    await hass.config.async_set_time_zone("Asia/Tokyo")
    await _setup(hass)

    device_id, units, start, end = mock_api.drinking_log_calls[0]
    assert (device_id, units) == (DEVICE_ID, 1)
    assert start == "2026-09-17"
    assert end == "2026-09-17"


async def test_time_zone_offset_is_resynced_every_poll(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A DST transition must not need a Home Assistant restart to take effect.

    The client is built once at setup, so if the coordinator did not push
    the current offset before each poll, the integration would keep signing
    requests with the offset that was correct the day HA last started.
    """
    await hass.config.async_set_time_zone("Asia/Tokyo")
    coordinator = await _setup(hass)

    mock_api.tz_offset_hours = 999
    await coordinator.async_refresh()

    assert mock_api.tz_offset_hours == 9


async def test_notify_config_polled_only_once_every_n_polls(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The account-level notify config is rarely fetched, not fetched per poll."""
    coordinator = await _setup(hass)
    assert mock_api.notify_config_calls == 1

    for _ in range(NOTIFY_CONFIG_EVERY_N_POLLS - 1):
        await coordinator.async_refresh()
    assert mock_api.notify_config_calls == 1

    await coordinator.async_refresh()
    assert mock_api.notify_config_calls == 2
    assert coordinator.notify_config is not None


async def test_auth_error_retries_login_exactly_once(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A stale token is refreshed silently; the user sees nothing."""
    coordinator = await _setup(hass)
    mock_api.fail_device_list(AlwaysFullAuthError("Token expired"), times=1)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert mock_api.login_calls == ["user@example.com"]
    assert mock_api.token == "FRESH-TOKEN"


async def test_second_auth_failure_raises_config_entry_auth_failed(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """If the re-login is rejected too, hand off to HA's reauth flow."""
    coordinator = await _setup(hass)
    mock_api.fail_device_list(AlwaysFullAuthError("Token expired"))
    mock_api.login_error = AlwaysFullAuthError("Account or password error")

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert len(mock_api.login_calls) == 1


async def test_rate_limit_is_never_mapped_to_auth_failure(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """429 means slow down, not "bad credentials" -- mapping it to reauth traps the user."""
    coordinator = await _setup(hass)
    mock_api.fail_device_list(AlwaysFullRateLimit("429"))

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert not isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert mock_api.login_calls == []


@pytest.mark.parametrize(
    "error",
    [
        # `asyncio.TimeoutError` IS the builtin on 3.11+, so one entry covers both.
        TimeoutError(),
        aiohttp.ClientError("boom"),
        ValueError("Expecting value: line 1 column 1 (char 0)"),
        AlwaysFullError("Unexpected response code 500"),
    ],
)
async def test_transport_errors_map_to_update_failed(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, error: Exception
) -> None:
    """Timeouts, client errors and malformed JSON degrade, never reauth."""
    coordinator = await _setup(hass)
    mock_api.fail_device_list(error)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)


async def test_scan_interval_option_is_clamped(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """An out-of-range interval is clamped, never used verbatim."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": "T"},
        options={"scan_interval": 5},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.update_interval.total_seconds() == 30


async def test_refresh_after_write_rereads_that_device_config(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A write is followed by a config re-read so the UI does not bounce back.

    The follow-up poll is sabotaged on purpose: the new value can then only
    have arrived via the scoped re-read, never via a full refresh that
    happened to run at the right moment.
    """
    coordinator = await _setup(hass)
    assert coordinator.data[DEVICE_ID].config.flush_interval_minutes == 60

    mock_api.device_config_override = load_fixture_data("device_config") | {"cleanCycle": 1800}
    mock_api.fail_device_list(AlwaysFullRateLimit("429"))

    await coordinator.async_refresh_after_write(DEVICE_ID)

    assert mock_api.device_config_calls == [DEVICE_ID, DEVICE_ID]
    assert coordinator.data[DEVICE_ID].config.flush_interval_minutes == 30


async def test_refresh_after_write_also_requests_a_full_poll(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """State the write changed still gets picked up by a real poll."""
    coordinator = await _setup(hass)
    before = mock_api.device_list_calls

    await coordinator.async_refresh_after_write(DEVICE_ID)
    await hass.async_block_till_done()

    assert mock_api.device_list_calls == before + 1
