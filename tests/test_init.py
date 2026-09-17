"""Config-entry setup, unload and reauth behaviour."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alwaysfull.const import DOMAIN
from custom_components.alwaysfull.coordinator import AlwaysFullCoordinator

from .conftest import FakeAlwaysFullClient, zone_unlike_host


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

    The zone is picked at runtime (see `zone_unlike_host`) as one that is
    DST-free AND whose offset differs from this host's. Hard-coding one
    would make the test host-dependent in both directions: vacuous on a
    runner in that zone, and failing on correct code on a runner that had
    been compared against.
    """
    zone, expected_offset = zone_unlike_host()
    await hass.config.async_set_time_zone(zone)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": "STORED"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert mock_api.token == "STORED"
    # A fixed, known value for the chosen zone -- which by construction is
    # NOT this host's offset, so an OS-clock implementation cannot produce it.
    assert mock_api.constructed_tz_offset_hours == expected_offset
