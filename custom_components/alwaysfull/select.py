"""Select platform for Always Full: display units and bowl size.

Neither of these belongs to a config group; each has its own endpoint, and
each carries a trap the transport layer already handles and this module
must not re-implement:

- `setUnits` is the ONE writer that names the device `deviceId` rather than
  `devNo`. `AlwaysFullClient.set_units` owns that; nothing here builds a
  wire body.
- `deviceType` is an INVERTED enum: 0 is the 9-inch bowl and 1 is the
  7-inch one. Both directions go through `models.py`, which is where that
  inversion is stated once and tested.

Both values are read from the DEVICE ROW rather than from `device/config`.
`units` is not in the config object at all, and taking the size from the
row as well keeps the two selects reading from one source. The cost is
honest and small, and it is why these two writes hand no config to
`async_write`: there is no written config object to publish, so after a
change these entities catch up on the poll the write requests rather than
showing the new option at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription

from .entity import AlwaysFullWriteEntity, async_add_bowl_entities
from .models import (
    BOWL_SIZE_7_INCH,
    BOWL_SIZE_9_INCH,
    UNITS_FLUID_OUNCES,
    UNITS_MILLILITRES,
    bowl_size_inches_to_device_type,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import AlwaysFullClient
    from .coordinator import AlwaysFullConfigEntry, BowlData

# One request at a time -- see `number.py`.
PARALLEL_UPDATES = 1

# The option a user picks <-> the vendor's `units` enum.
UNIT_OPTIONS: dict[str, int] = {
    "ml": UNITS_MILLILITRES,
    "fl_oz": UNITS_FLUID_OUNCES,
}

# The option a user picks <-> a bowl size in inches. The inches -> wire
# `deviceType` step is `bowl_size_inches_to_device_type`'s job, NOT a
# second table here: two places to state an inverted enum is one place too
# many.
BOWL_SIZE_OPTIONS: dict[str, int] = {
    "9_inch": BOWL_SIZE_9_INCH,
    "7_inch": BOWL_SIZE_7_INCH,
}

# The same two options against the vendor's own wire values, for READING a
# device row back. Derived from `BOWL_SIZE_OPTIONS` through the one
# conversion, never written out by hand: a second hand-written table is
# how an inverted enum gets inverted twice.
BOWL_SIZE_DEVICE_TYPES: dict[str, int] = {
    option: bowl_size_inches_to_device_type(inches)
    for option, inches in BOWL_SIZE_OPTIONS.items()
}


def _reverse(mapping: dict[str, int]) -> dict[int, str]:
    """Return `mapping` inverted, for reading a wire value back to an option."""
    return {value: option for option, value in mapping.items()}


@dataclass(frozen=True, kw_only=True)
class AlwaysFullSelectEntityDescription(SelectEntityDescription):
    """Describes one writable Always Full select."""

    value_fn: Callable[[BowlData], str | None]
    write_fn: Callable[[AlwaysFullClient, str, str], Awaitable[Any]]


def _current_units(bowl: BowlData) -> str | None:
    """Return the bowl's display unit as an option, or `None` if unrecognised."""
    return _reverse(UNIT_OPTIONS).get(bowl.state.units)


def _current_bowl_size(bowl: BowlData) -> str | None:
    """Return the bowl's size as an option, decoded from the inverted enum.

    Decoded from the RAW `deviceType`, NOT from `state.bowl_size_inches`.
    That property falls back to 9" for any value outside the enum, which is
    right where an unrecognised value must not raise mid-poll and nobody
    acts on the answer -- the device page's model string. Reading it here
    would put that guess in front of the user as a setting they can
    "correct", and correcting it WRITES a size to the bowl.

    So: a `deviceType` this integration does not understand reports
    nothing, exactly as `_current_units` does for a `units` it does not
    understand. Both real options stay selectable, so nothing is lost.
    """
    return _reverse(BOWL_SIZE_DEVICE_TYPES).get(bowl.state.device_type_raw)


def _write_units(client: AlwaysFullClient, device_id: str, option: str) -> Awaitable[Any]:
    """Send the vendor's `units` enum for `option`."""
    return client.set_units(device_id, UNIT_OPTIONS[option])


def _write_bowl_size(client: AlwaysFullClient, device_id: str, option: str) -> Awaitable[Any]:
    """Send the vendor's INVERTED `deviceType` enum for `option`."""
    return client.set_device_type(
        device_id, bowl_size_inches_to_device_type(BOWL_SIZE_OPTIONS[option])
    )


SELECTS: tuple[AlwaysFullSelectEntityDescription, ...] = (
    AlwaysFullSelectEntityDescription(
        key="units",
        translation_key="units",
        options=list(UNIT_OPTIONS),
        value_fn=_current_units,
        write_fn=_write_units,
    ),
    AlwaysFullSelectEntityDescription(
        key="bowl_size",
        translation_key="bowl_size",
        options=list(BOWL_SIZE_OPTIONS),
        value_fn=_current_bowl_size,
        write_fn=_write_bowl_size,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlwaysFullConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one set of selects per bowl on the account, now or on a later poll."""
    coordinator = entry.runtime_data
    async_add_bowl_entities(
        coordinator,
        async_add_entities,
        lambda device_id: (
            AlwaysFullSelect(coordinator, device_id, description) for description in SELECTS
        ),
    )


class AlwaysFullSelect(AlwaysFullWriteEntity, SelectEntity):
    """One multiple-choice setting on one bowl."""

    entity_description: AlwaysFullSelectEntityDescription

    @property
    def current_option(self) -> str | None:
        """Return the current option, or `None` for a value outside the enum.

        `None` rather than a guess: Home Assistant rejects a state that is
        not one of the declared options outright, which would break the
        entity rather than just this reading.
        """
        bowl = self.bowl
        if bowl is None:
            return None
        return self.entity_description.value_fn(bowl)

    async def async_select_option(self, option: str) -> None:
        """Send the newly chosen option."""
        await self.async_write(
            lambda: self.entity_description.write_fn(
                self.coordinator.client, self._device_id, option
            )
        )
