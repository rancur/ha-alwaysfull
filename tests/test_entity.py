"""Base entity identity, device info and availability."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityDescription
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alwaysfull.const import DOMAIN
from custom_components.alwaysfull.entity import AlwaysFullEntity
from custom_components.alwaysfull.exceptions import AlwaysFullRateLimit

from .conftest import DEVICE_ID, FakeAlwaysFullClient

DESCRIPTION = EntityDescription(key="filter_life")


async def _entity(hass: HomeAssistant) -> AlwaysFullEntity:
    """Load an entry and build a bare base entity against its coordinator."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": "T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return AlwaysFullEntity(entry.runtime_data, DEVICE_ID, DESCRIPTION)


async def test_device_info_is_keyed_on_the_vendor_device_id(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Identifiers must survive removing and re-adding the integration.

    Anything derived from the config entry id changes on re-add, which
    orphans every recorder history row for the device.
    """
    entity = await _entity(hass)

    assert entity.unique_id == f"{DEVICE_ID}_filter_life"
    assert entity.has_entity_name is True

    info = entity.device_info
    assert info["identifiers"] == {(DOMAIN, DEVICE_ID)}
    assert info["manufacturer"] == "Always Full"
    assert info["model"] == '9" Bowl'
    assert info["sw_version"] == "3.6.2"
    assert info["name"] == "Test Bowl"


async def test_available_tracks_update_success_and_membership(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Availability needs both a good poll and this device still present."""
    entity = await _entity(hass)
    assert entity.available is True

    mock_api.fail_device_list(AlwaysFullRateLimit("429"))
    await entity.coordinator.async_refresh()
    assert entity.available is False

    mock_api.device_list_error = None
    await entity.coordinator.async_refresh()
    assert entity.available is True

    unknown = AlwaysFullEntity(entity.coordinator, "ffeeddccbbaa", DESCRIPTION)
    assert unknown.available is False
