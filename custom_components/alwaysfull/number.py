"""Number platform for Always Full: the seven numeric settings.

Same description-table shape as `sensor.py`, with two extra columns: how a
new value is applied to the cached config, and which whole-object config
group carries it to the vendor.

Nothing here converts a unit. Every conversion lives in `models.py` and is
reached through a UI-format property (`flush_interval_minutes` and
friends), so that the wire value a user's change turns into is decided in
exactly one place and tested in both directions there. Re-deriving even
one of them inline would be invisible until someone noticed their bowl
flushing every thirty seconds.

The one field in these groups with NO conversion is `cleanWarnTime`. No
entity exposes it; it is read as raw seconds and handed straight back by
`to_maintenance_payload`. See that method's docstring -- an earlier version
of it claimed a months conversion existed, and acting on that claim is a
factor-of-2,592,000 mistake.

## Why a no-op set writes the value back unchanged

Three of these settings are stored in seconds and shown in a coarser unit
(minutes, 30-day months, days), so the display is a FLOOR and the
round-trip loses whatever did not divide evenly.

That is not hypothetical. The bowl this integration was built against
reports `filterCanUseTime: 10512000`, which is exactly 4 x 2628000 --
four months of 365/12 days. The vendor's app reads with
`Math.floor(filterCanUseTime / 2592e3)` and writes `30 * months * 24 * 60
* 60`, i.e. a 30-DAY month, so it displays 4 and, on a save that changed
nothing, writes 10368000. The device was provisioned with one month length
and the app saves with another, and the filter lifetime quietly loses 1.67
days every time someone opens that screen and presses save.

`SECONDS_PER_MONTH` stays 2592000: being faithful to the vendor's
arithmetic is right for a real change, because their server and their app
are the ecosystem this integration lives in. What is fixed here is the
no-op. `_unchanged_keeps_raw` leaves the config untouched when the new
value floors to the value already displayed, so the ORIGINAL seconds are
what get written back. A genuine change converts normally and the drift
never accumulates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import UnitOfTime, UnitOfVolume
from homeassistant.core import callback

from .entity import (
    FILTER_GROUP,
    FLUSH_GROUP,
    MAINTENANCE_GROUP,
    WATER_GROUP,
    AlwaysFullWriteEntity,
    ConfigGroup,
)
from .models import UNITS_FLUID_OUNCES

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import AlwaysFullConfigEntry, AlwaysFullCoordinator, BowlData
    from .models import BowlConfig

# Every entity here sends a request of its own. One at a time: a user
# changing four settings from one dashboard card would otherwise fan four
# simultaneous requests at a cloud API that answers 429.
PARALLEL_UPDATES = 1

# Flush interval: the vendor app offers 1 minute to 24 hours.
MAX_FLUSH_INTERVAL_MINUTES = 1440
# Flush duration: seconds on the wire already, and the vendor's own screen
# refuses anything under 5.
MIN_FLUSH_DURATION_SECONDS = 5
MAX_FLUSH_DURATION_SECONDS = 120
MAX_FILTER_LIFE_MONTHS = 48
MAX_FILTER_CAPACITY = 200000000
MAX_MAINTENANCE_DAYS = 365
# A guard rail rather than a vendor-documented ceiling: no bowl holds this,
# and an unbounded box invites a typo that trips the daily-maximum alert
# for ever. Stated in the bowl's OWN units, which is what the wire carries.
MAX_DAILY_WATER = 200000


def _water_unit(bowl: BowlData) -> str:
    """Return the bowl's own volume unit (`units`: 1 = mL, 2 = fl oz).

    The daily thresholds are stored in whatever unit the bowl is set to --
    which is why `waterConfig` has to echo `units` back -- so the entity
    has to follow the device rather than pick one.
    """
    if bowl.state.units == UNITS_FLUID_OUNCES:
        return UnitOfVolume.FLUID_OUNCES
    return UnitOfVolume.MILLILITERS


@dataclass(frozen=True, kw_only=True)
class AlwaysFullNumberEntityDescription(NumberEntityDescription):
    """Describes one writable Always Full number."""

    value_fn: Callable[[BowlConfig], float | None]
    # Applied to a COPY of the cached config; the matching `to_*_payload()`
    # then rebuilds the whole group from it.
    set_fn: Callable[[BowlConfig, int], None]
    group: ConfigGroup
    # Set only where the unit is a property of the bowl rather than of the
    # setting -- i.e. the daily water thresholds.
    unit_fn: Callable[[BowlData], str] | None = None


def _unchanged_keeps_raw(
    read: Callable[[BowlConfig], int],
    write: Callable[[BowlConfig, int], None],
) -> Callable[[BowlConfig, int], None]:
    """Wrap a LOSSY setter so that setting the displayed value changes nothing.

    For a setting stored in seconds and shown in a coarser unit, `read` is
    a floor. Writing the floored value back is a silent downward edit of
    however much did not divide evenly -- see the module docstring, where
    the vendor's own app loses 1.67 days of filter life this way.

    So: if the requested value is the one already on display, return
    without touching the config. The caller holds a COPY of the cached
    config, so leaving it alone means the device's own raw seconds are
    what the whole-object payload carries back. Any other value converts
    normally, because then the user really is asking for a change and the
    vendor's arithmetic is the right arithmetic to use.
    """

    def _set(config: BowlConfig, value: int) -> None:
        if read(config) == value:
            return
        write(config, value)

    return _set


def _set_flush_interval(config: BowlConfig, value: int) -> None:
    config.flush_interval_minutes = value


def _set_flush_duration(config: BowlConfig, value: int) -> None:
    # `cleanTime` is seconds on the wire already: the ONE field in this
    # group that must not be multiplied -- and, being unconverted, the one
    # flush field that needs no no-op guard.
    config.clean_time = value


def _set_filter_life(config: BowlConfig, value: int) -> None:
    config.filter_life_months = value


def _set_filter_capacity(config: BowlConfig, value: int) -> None:
    config.filter_capacity_ml = value


def _set_maintenance_interval(config: BowlConfig, value: int) -> None:
    config.maintenance_interval_days = value


# The three lossy ones. `filter_capacity` and the daily thresholds are
# stored in the unit they are shown in, so they cannot drift.
_SET_FLUSH_INTERVAL = _unchanged_keeps_raw(
    lambda config: config.flush_interval_minutes, _set_flush_interval
)
_SET_FILTER_LIFE = _unchanged_keeps_raw(
    lambda config: config.filter_life_months, _set_filter_life
)
_SET_MAINTENANCE_INTERVAL = _unchanged_keeps_raw(
    lambda config: config.maintenance_interval_days, _set_maintenance_interval
)


def _set_day_min_water(config: BowlConfig, value: int) -> None:
    config.day_min_water = value


def _set_day_max_water(config: BowlConfig, value: int) -> None:
    config.day_max_water = value


NUMBERS: tuple[AlwaysFullNumberEntityDescription, ...] = (
    AlwaysFullNumberEntityDescription(
        key="flush_interval",
        translation_key="flush_interval",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=1,
        native_max_value=MAX_FLUSH_INTERVAL_MINUTES,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda config: config.flush_interval_minutes,
        set_fn=_SET_FLUSH_INTERVAL,
        group=FLUSH_GROUP,
    ),
    AlwaysFullNumberEntityDescription(
        key="flush_duration",
        translation_key="flush_duration",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        native_min_value=MIN_FLUSH_DURATION_SECONDS,
        native_max_value=MAX_FLUSH_DURATION_SECONDS,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda config: config.clean_time,
        set_fn=_set_flush_duration,
        group=FLUSH_GROUP,
    ),
    AlwaysFullNumberEntityDescription(
        key="filter_lifetime",
        translation_key="filter_lifetime",
        # A vendor "month" is a fixed 30 days, not a calendar month.
        native_unit_of_measurement=UnitOfTime.MONTHS,
        native_min_value=0,
        native_max_value=MAX_FILTER_LIFE_MONTHS,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda config: config.filter_life_months,
        set_fn=_SET_FILTER_LIFE,
        group=FILTER_GROUP,
    ),
    AlwaysFullNumberEntityDescription(
        key="filter_capacity",
        translation_key="filter_capacity",
        native_unit_of_measurement=UnitOfVolume.MILLILITERS,
        native_min_value=0,
        native_max_value=MAX_FILTER_CAPACITY,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda config: config.filter_capacity_ml,
        set_fn=_set_filter_capacity,
        group=FILTER_GROUP,
    ),
    AlwaysFullNumberEntityDescription(
        key="maintenance_interval",
        translation_key="maintenance_interval",
        native_unit_of_measurement=UnitOfTime.DAYS,
        native_min_value=0,
        native_max_value=MAX_MAINTENANCE_DAYS,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda config: config.maintenance_interval_days,
        set_fn=_SET_MAINTENANCE_INTERVAL,
        group=MAINTENANCE_GROUP,
    ),
    AlwaysFullNumberEntityDescription(
        key="daily_minimum_water",
        translation_key="daily_minimum_water",
        native_min_value=0,
        native_max_value=MAX_DAILY_WATER,
        native_step=1,
        mode=NumberMode.BOX,
        unit_fn=_water_unit,
        value_fn=lambda config: config.day_min_water,
        set_fn=_set_day_min_water,
        group=WATER_GROUP,
    ),
    AlwaysFullNumberEntityDescription(
        key="daily_maximum_water",
        translation_key="daily_maximum_water",
        native_min_value=0,
        native_max_value=MAX_DAILY_WATER,
        native_step=1,
        mode=NumberMode.BOX,
        unit_fn=_water_unit,
        value_fn=lambda config: config.day_max_water,
        set_fn=_set_day_max_water,
        group=WATER_GROUP,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlwaysFullConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one set of numbers per bowl found by the first poll."""
    coordinator = entry.runtime_data
    async_add_entities(
        AlwaysFullNumber(coordinator, device_id, description)
        for device_id in coordinator.data
        for description in NUMBERS
    )


class AlwaysFullNumber(AlwaysFullWriteEntity, NumberEntity):
    """One numeric setting on one bowl."""

    entity_description: AlwaysFullNumberEntityDescription

    def __init__(
        self,
        coordinator: AlwaysFullCoordinator,
        device_id: str,
        description: AlwaysFullNumberEntityDescription,
    ) -> None:
        """Bind the description and take an initial reading of the unit."""
        super().__init__(coordinator, device_id, description)
        self._sync_unit()

    @callback
    def _sync_unit(self) -> None:
        """Track a unit the user changed on the bowl itself.

        Only ever updated from a poll that actually found this bowl, so a
        dropped device keeps its last known unit instead of blanking it.
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
    def native_value(self) -> float | None:
        """Return this setting's current value from the last poll."""
        bowl = self.bowl
        if bowl is None:
            return None
        return self.entity_description.value_fn(bowl.config)

    async def async_set_native_value(self, value: float) -> None:
        """Send a new value, as the whole config group it belongs to.

        Rounded to an int because every one of these fields is an integer
        on the wire; Home Assistant hands over a float whatever the step
        says, and a bare `int()` would truncate 29.999999 to 29.
        """
        whole = round(value)
        await self.async_write_config(
            self.entity_description.group,
            lambda config: self.entity_description.set_fn(config, whole),
        )
