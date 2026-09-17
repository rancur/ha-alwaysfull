"""Binary sensor platform for Always Full.

Same description-table shape as `sensor.py`: the entities differ only in
which field they read.

Every alarm the vendor exposes is an int that its own app compares `== 1`
and nothing else. Anything other than 1 is therefore reported as NOT
alarming, not as "probably an alarm": guessing a meaning for a value we
have never observed would raise an alert the device never raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory

from .entity import AlwaysFullEntity
from .models import SLAVE_TYPE_NONE, SLAVE_TYPE_WALL_UNIT

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import AlwaysFullConfigEntry, BowlData


def _filter_fault(bowl: BowlData) -> bool | None:
    """Return whether the filter is faulted, or `None` on a non-wall unit.

    A bottle-pump bowl has no filter at all, so `filterState` carries no
    meaning there -- and because the fault is `filterState == 0`, reading
    it naively would pin a permanent, unclearable filter fault on a bowl
    that cannot have one. The entity stays registered either way; removing
    entities on a runtime value churns the entity registry.
    """
    if bowl.state.slave_type != SLAVE_TYPE_WALL_UNIT:
        return None
    return bowl.state.has_filter_fault


@dataclass(frozen=True, kw_only=True)
class AlwaysFullBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes one Always Full binary sensor."""

    value_fn: Callable[[BowlData], bool | None]


BINARY_SENSORS: tuple[AlwaysFullBinarySensorEntityDescription, ...] = (
    AlwaysFullBinarySensorEntityDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda bowl: bowl.state.is_online,
    ),
    AlwaysFullBinarySensorEntityDescription(
        key="system_problem",
        translation_key="system_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda bowl: bowl.state.has_system_problem,
    ),
    AlwaysFullBinarySensorEntityDescription(
        key="fill_alarm",
        translation_key="fill_alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda bowl: bowl.state.has_injection_alarm,
    ),
    AlwaysFullBinarySensorEntityDescription(
        key="pump_alarm",
        translation_key="pump_alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda bowl: bowl.state.has_pump_alarm,
    ),
    AlwaysFullBinarySensorEntityDescription(
        key="not_level",
        translation_key="not_level",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda bowl: bowl.state.has_horizontal_alarm,
    ),
    AlwaysFullBinarySensorEntityDescription(
        key="filter_fault",
        translation_key="filter_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_filter_fault,
    ),
    AlwaysFullBinarySensorEntityDescription(
        key="water_source_detached",
        translation_key="water_source_detached",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda bowl: bowl.state.slave_type == SLAVE_TYPE_NONE,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlwaysFullConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one set of binary sensors per bowl found by the first poll."""
    coordinator = entry.runtime_data
    async_add_entities(
        AlwaysFullBinarySensor(coordinator, device_id, description)
        for device_id in coordinator.data
        for description in BINARY_SENSORS
    )


class AlwaysFullBinarySensor(AlwaysFullEntity, BinarySensorEntity):
    """One boolean read off a bowl."""

    entity_description: AlwaysFullBinarySensorEntityDescription

    @property
    def is_on(self) -> bool | None:
        """Return this sensor's value from the last poll."""
        bowl = self.bowl
        if bowl is None:
            return None
        return self.entity_description.value_fn(bowl)
