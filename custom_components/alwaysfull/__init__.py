"""The Always Full pet water bowl integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AlwaysFullClient
from .coordinator import AlwaysFullCoordinator, ha_utc_offset_hours

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import AlwaysFullConfigEntry

# Read platforms. The write platforms are added by a later task.
PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: AlwaysFullConfigEntry) -> bool:
    """Set up Always Full from a config entry."""
    client = AlwaysFullClient(
        async_get_clientsession(hass),
        token=entry.data.get(CONF_TOKEN, ""),
        # Home Assistant's configured zone, not the OS process's -- see
        # `ha_utc_offset_hours`. The coordinator re-syncs this every poll
        # so a DST transition does not need a restart.
        tz_offset_hours=ha_utc_offset_hours(),
    )

    coordinator = AlwaysFullCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlwaysFullConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
