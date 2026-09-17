"""Button platform for Always Full: resetting the filter's elapsed life.

`/app/device/reset/filter` zeroes `filterUsedTime`, which is what the
filter-life sensors are computed from. It is NOT the same thing as a
filter-config write: setting `filterCanUseTime` changes the filter's TOTAL
lifetime, and setting it to zero disables filter tracking altogether. The
two are easy to confuse and the server accepts both, so the distinction is
pinned by a test rather than only stated here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription

from .entity import AlwaysFullWriteEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import AlwaysFullConfigEntry

# One request at a time -- see `number.py`.
PARALLEL_UPDATES = 1

BUTTONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(
        key="reset_filter",
        translation_key="reset_filter",
        # No device class: `ButtonDeviceClass` offers only `restart` and
        # `identify`, and this is neither.
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlwaysFullConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one filter-reset button per bowl found by the first poll."""
    coordinator = entry.runtime_data
    async_add_entities(
        AlwaysFullResetFilterButton(coordinator, device_id, description)
        for device_id in coordinator.data
        for description in BUTTONS
    )


class AlwaysFullResetFilterButton(AlwaysFullWriteEntity, ButtonEntity):
    """Zeroes one bowl's elapsed filter life."""

    async def async_press(self) -> None:
        """Reset the filter's elapsed life and re-read the bowl."""
        await self.async_write(
            lambda: self.coordinator.client.reset_filter(self._device_id)
        )
