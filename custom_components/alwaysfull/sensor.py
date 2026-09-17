"""Sensor platform for Always Full.

Built as a description table with a `value_fn` per entity rather than one
class per entity: the per-entity differences here are entirely data (which
field, which unit, which device class), and the class-per-entity shape is
what turns a six-sensor integration into a four-thousand-line file nobody
can audit.

Two device-class facts were verified by introspecting the installed Home
Assistant, and both fail SILENTLY (a log warning plus broken long-term
statistics, not an exception), so they are recorded here rather than
rediscovered later:

- `SensorDeviceClass.WATER` REJECTS millilitres. Its permitted units are
  CCF, L, MCF, ft3, gal and m3 -- it is the water-METER class for the
  Energy dashboard. `SensorDeviceClass.VOLUME` accepts both `mL` and
  `fl. oz.`, which is exactly the vendor's `units` enum, so VOLUME is what
  water consumed uses.
- The only legal state classes for VOLUME are `TOTAL_INCREASING` and
  `TOTAL`. `MEASUREMENT` is NOT legal on VOLUME (it is legal on
  `VOLUME_STORAGE`, a different class for a different meaning).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime, UnitOfVolume
from homeassistant.core import callback

from .const import ALERT_OPTIONS, ALERT_TYPE_OPTIONS, ATTR_RAW_TYPE, UNKNOWN
from .entity import AlwaysFullEntity
from .models import (
    SLAVE_TYPE_BOTTLE_PUMP,
    SLAVE_TYPE_NONE,
    SLAVE_TYPE_WALL_UNIT,
    UNITS_FLUID_OUNCES,
    filter_life_percent,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from .coordinator import AlwaysFullConfigEntry, AlwaysFullCoordinator, BowlData

# `UNKNOWN`, `ALERT_TYPE_OPTIONS` and `ATTR_RAW_TYPE` are imported from
# `const` rather than defined here: the event platform reports the very same
# alert options, and one mapping shared by both is the only way they cannot
# drift apart.

# Read-only platform: nothing here talks to the device, every value comes
# from one shared coordinator poll. Declared explicitly because the
# integration quality scale expects it stated rather than inferred.
PARALLEL_UPDATES = 0

WATER_SOURCE_OPTIONS = {
    SLAVE_TYPE_NONE: "none",
    SLAVE_TYPE_BOTTLE_PUMP: "bottle_pump",
    SLAVE_TYPE_WALL_UNIT: "wall_unit",
}

# Attributes carrying the vendor's untouched value behind a normalised enum
# state, including for a value this integration does not know yet: a user
# can match on it the day the vendor ships a new one, without waiting for
# us. Without these, an unrecognised value is indistinguishable from no
# value at all, because Home Assistant renders the `unknown` OPTION exactly
# like a missing state.
ATTR_RAW_SLAVE_TYPE = "raw_slave_type"


def _wall_unit_only(
    value_fn: Callable[[BowlData], StateType],
) -> Callable[[BowlData], StateType]:
    """Return `value_fn`, but reporting nothing unless this is a wall unit.

    Filter life is meaningless on a bottle-pump bowl -- there is no filter
    -- and the vendor's own app hides its filter tiles entirely unless
    `slaveType == 2`. The entities stay REGISTERED regardless, because
    adding and removing entities on a runtime value churns the entity
    registry and orphans history; they just report `None`.
    """

    def _value(bowl: BowlData) -> StateType:
        if bowl.state.slave_type != SLAVE_TYPE_WALL_UNIT:
            return None
        return value_fn(bowl)

    return _value


def _filter_state(bowl: BowlData) -> dict[str, Any]:
    """Return the two fields filter life is computed from.

    `filterUsedTime` comes from the device row and `filterCanUseTime` from
    the device CONFIG -- two different endpoints -- so they have to be
    recombined here.
    """
    return {
        "filterCanUseTime": bowl.config.filter_can_use_time_seconds,
        "filterUsedTime": bowl.state.filter_used_time,
    }


def _filter_seconds_remaining(bowl: BowlData) -> StateType:
    """Return `filterCanUseTime - filterUsedTime`, floored at zero.

    Returns `None` when `filterCanUseTime` is 0, which is the live-observed
    value for "filter tracking is disabled on this bowl" -- a bare
    subtraction there would report a large negative number as if the filter
    were catastrophically overdue.
    """
    fields = _filter_state(bowl)
    can_use = fields["filterCanUseTime"] or 0
    if not can_use:
        return None
    return max(can_use - (fields["filterUsedTime"] or 0), 0)


def _newest_notification(bowl: BowlData) -> dict[str, Any] | None:
    """Return the newest notify-log row, or `None` if the bowl has none.

    The newest row is chosen by `createTime`, NOT by taking `rows[0]`. The
    captured page happens to arrive newest-first, so trusting the order
    would look correct against every fixture and silently report a
    days-old alert the first time the server paginated differently.
    `createTime` is a fixed-width UTC `%Y-%m-%dT%H:%M:%SZ` string, so
    lexicographic order IS chronological order and no parsing (which could
    raise mid-update) is needed.
    """
    rows = [row for row in bowl.notifications if isinstance(row, dict)]
    if not rows:
        return None
    return max(rows, key=lambda row: row.get("createTime") or "")


def _last_alert(bowl: BowlData) -> StateType:
    """Return the newest alert's type, normalised into the enum's options.

    An alert type the vendor has added since this table was written maps to
    `unknown` rather than being passed through: an option outside the
    declared list makes Home Assistant reject the state outright. The raw
    string stays available through `_last_alert_attributes`.
    """
    row = _newest_notification(bowl)
    if row is None:
        return None
    return ALERT_TYPE_OPTIONS.get(row.get("type"), UNKNOWN)


def _last_alert_attributes(bowl: BowlData) -> dict[str, Any]:
    """Return the vendor's own `type` string for the newest alert.

    Always present, so an automation can rely on the key existing; `None`
    when the bowl has never alerted.
    """
    row = _newest_notification(bowl)
    return {ATTR_RAW_TYPE: None if row is None else row.get("type")}


def _water_source_attributes(bowl: BowlData) -> dict[str, Any]:
    """Return the vendor's own `slaveType` int for this bowl."""
    return {ATTR_RAW_SLAVE_TYPE: bowl.state.slave_type}


def _water_unit(bowl: BowlData) -> str:
    """Return the bowl's own volume unit (`units`: 1 = mL, 2 = fl oz)."""
    if bowl.state.units == UNITS_FLUID_OUNCES:
        return UnitOfVolume.FLUID_OUNCES
    return UnitOfVolume.MILLILITERS


@dataclass(frozen=True, kw_only=True)
class AlwaysFullSensorEntityDescription(SensorEntityDescription):
    """Describes one Always Full sensor."""

    value_fn: Callable[[BowlData], StateType]
    # Set only where the unit is a property of the bowl rather than of the
    # sensor -- i.e. water consumed, which the user can switch between
    # millilitres and fluid ounces on the device itself.
    unit_fn: Callable[[BowlData], str] | None = None
    # Set only where normalising the state throws away something a user
    # might need -- currently just the vendor's raw alert type.
    attributes_fn: Callable[[BowlData], dict[str, Any]] | None = None


SENSORS: tuple[AlwaysFullSensorEntityDescription, ...] = (
    AlwaysFullSensorEntityDescription(
        key="water_today",
        translation_key="water_today",
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL_INCREASING,
        # VOLUME is convertible, so Home Assistant renders it in the user's
        # own unit system -- a bowl set to fluid ounces reads in millilitres
        # on a metric Home Assistant. The fractional millilitres that falls
        # out of that conversion are noise for a pet's water bowl.
        suggested_display_precision=0,
        unit_fn=_water_unit,
        value_fn=lambda bowl: bowl.water_today,
    ),
    AlwaysFullSensorEntityDescription(
        key="filter_life",
        translation_key="filter_life",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_wall_unit_only(lambda bowl: filter_life_percent(_filter_state(bowl))),
    ),
    AlwaysFullSensorEntityDescription(
        key="filter_remaining",
        translation_key="filter_remaining",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        value_fn=_wall_unit_only(_filter_seconds_remaining),
    ),
    AlwaysFullSensorEntityDescription(
        key="water_source",
        translation_key="water_source",
        device_class=SensorDeviceClass.ENUM,
        options=[*WATER_SOURCE_OPTIONS.values(), UNKNOWN],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda bowl: WATER_SOURCE_OPTIONS.get(bowl.state.slave_type, UNKNOWN),
        attributes_fn=_water_source_attributes,
    ),
    AlwaysFullSensorEntityDescription(
        key="last_alert",
        translation_key="last_alert",
        device_class=SensorDeviceClass.ENUM,
        options=ALERT_OPTIONS,
        # Deliberately NOT diagnostic. This is the entity that says what
        # actually went wrong with the bowl, and Home Assistant collapses
        # diagnostic entities by default -- which would bury the one reading
        # the owner most wants to see.
        value_fn=_last_alert,
        attributes_fn=_last_alert_attributes,
    ),
    AlwaysFullSensorEntityDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        # The device page already shows this; an entity for it is for
        # automations and templates, so it is opt-in.
        entity_registry_enabled_default=False,
        value_fn=lambda bowl: bowl.firmware_version,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlwaysFullConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one set of sensors per bowl found by the first poll."""
    coordinator = entry.runtime_data
    async_add_entities(
        AlwaysFullSensor(coordinator, device_id, description)
        for device_id in coordinator.data
        for description in SENSORS
    )


class AlwaysFullSensor(AlwaysFullEntity, SensorEntity):
    """One value read off a bowl."""

    entity_description: AlwaysFullSensorEntityDescription

    def __init__(
        self,
        coordinator: AlwaysFullCoordinator,
        device_id: str,
        description: AlwaysFullSensorEntityDescription,
    ) -> None:
        """Bind the description and take an initial reading of the unit."""
        super().__init__(coordinator, device_id, description)
        self._sync_unit()

    @callback
    def _sync_unit(self) -> None:
        """Track a unit the user changed on the bowl itself.

        Only ever updated from a poll that actually found this bowl, so a
        dropped device keeps its last known unit instead of blanking it and
        making Home Assistant treat the history as a unit change.
        """
        if self.entity_description.unit_fn is None:
            return
        bowl = self.bowl
        if bowl is not None:
            self._attr_native_unit_of_measurement = self.entity_description.unit_fn(bowl)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_unit()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> StateType:
        """Return this sensor's value from the last poll."""
        bowl = self.bowl
        if bowl is None:
            return None
        return self.entity_description.value_fn(bowl)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the sensor's extra attributes, if it has any."""
        attributes_fn = self.entity_description.attributes_fn
        bowl = self.bowl
        if attributes_fn is None or bowl is None:
            return None
        return attributes_fn(bowl)
