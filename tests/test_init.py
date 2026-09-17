"""Config-entry setup, unload and reauth behaviour."""

from __future__ import annotations

from datetime import datetime

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alwaysfull.const import DOMAIN
from custom_components.alwaysfull.coordinator import AlwaysFullCoordinator

from .conftest import FakeAlwaysFullClient


async def test_setup_and_unload(hass: HomeAssistant, mock_api: FakeAlwaysFullClient) -> None:
    """A good entry loads, exposes its coordinator, and unloads cleanly."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": "T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, AlwaysFullCoordinator)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_api_auth_fails: FakeAlwaysFullClient
) -> None:
    """A rejected token that cannot be refreshed sends the user to reauth."""
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "user@example.com", "password": "pw"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )


async def test_client_gets_stored_token_and_ha_time_zone(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The client is built with the stored token and HA's configured zone.

    The offset must come from `hass.config.time_zone`, never the OS clock:
    a container running `TZ=UTC` under an HA configured for another zone
    would otherwise mis-bucket every drinking-log day.

    Tokyo is chosen because it observes no DST (so the expected value is
    stable year-round) and is almost certainly not the developer's or the
    CI runner's own zone -- an assertion that happens to match the host
    clock would prove nothing.
    """
    await hass.config.async_set_time_zone("Asia/Tokyo")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": "STORED"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert mock_api.token == "STORED"
    assert mock_api.constructed_tz_offset_hours == 9
    # And it really is HA's zone, not the machine's.
    assert mock_api.constructed_tz_offset_hours != int(
        datetime.now().astimezone().utcoffset().total_seconds() / 3600
    )
