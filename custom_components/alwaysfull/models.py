"""Domain models and wire<->UI unit conversions for Always Full.

This module has no Home Assistant imports so it can be exercised by plain
pytest with no test harness -- it sits between the HA-free `api.py` client
and the HA entity layer, and staying HA-free keeps it independently testable.

The vendor's wire format and its UI format disagree on almost every
configurable field, and nothing in the API response tells you which is
which. A missed conversion here does not raise an error: it silently sets
a flush interval to 60 seconds instead of 60 minutes, or a filter life to
4 seconds instead of 4 months. Every conversion below has a wire<->UI pair
and is exercised in both directions by `tests/test_models.py`.

Conversion constants are verbatim from the vendor client (see the design
doc's "Traps the implementation must honour" and "Live-verified findings"):
- `cleanCycle` (flush interval): seconds <-> minutes, 60 s/min.
- `filterCanUseTime` (filter life): seconds <-> a 30-day "month",
  2592000 s/month. This is a fixed 30-day unit, not a calendar month.
- `cleanWarnTime`: NO conversion, in either direction. Stored and written
  as raw seconds. It is listed here because its absence from the list is
  exactly the sort of thing a reader assumes is an oversight; it is not.
- `deviceCanUseTime` (maintenance interval): seconds <-> days, 86400 s/day.
- `sleepStart` / `sleepEnd`: minutes since local midnight <-> `time`.
- `capacity` (read) / `filterCapacity` (write): same value, different wire
  name depending on direction -- the single easiest thing to get wrong.
- `deviceType`: an INVERTED enum -- 0 means a 9" bowl, 1 means a 7" bowl.
- `units`: 1 = millilitres, 2 = fluid ounces.

Deliberately NOT modeled:
- `filterDueState` -- `/app/device/detail` never actually returns this
  field, even though the vendor app reads it (which is why its filter-life
  tile is permanently green in the wild). Filter life is instead computed
  here from `filterUsedTime` / `filterCanUseTime` via `filter_life_percent`.
- `headLength`, `bodyLength`, `seq`, `msgCode` -- raw protocol-framing
  fields returned by `/app/device/config`. They are not device state, are
  never read by `BowlConfig.from_api`, and therefore can never leak back
  out through any `to_*_payload()` method.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any

# -- Conversion constants (verbatim from the vendor client) -----------------

SECONDS_PER_MINUTE = 60
SECONDS_PER_MONTH = 2592000  # a vendor "month" is exactly 30 days, not calendar
SECONDS_PER_DAY = 86400

UNITS_MILLILITRES = 1
UNITS_FLUID_OUNCES = 2

# The vendor's bowl-size enum is inverted relative to what anyone would guess.
DEVICE_TYPE_9_INCH = 0
DEVICE_TYPE_7_INCH = 1

# The same two bowls as the sizes a human reads off the product, which is what
# the UI side of the conversion deals in. Named so the inversion above stays
# legible at each call site.
BOWL_SIZE_9_INCH = 9
BOWL_SIZE_7_INCH = 7

SLAVE_TYPE_NONE = 0
SLAVE_TYPE_BOTTLE_PUMP = 1
SLAVE_TYPE_WALL_UNIT = 2


# -- Small standalone conversions --------------------------------------------


def minutes_to_time(minutes: int) -> datetime.time:
    """Convert minutes-since-local-midnight (wire) to a `datetime.time` (UI)."""
    hours, mins = divmod(minutes, 60)
    return datetime.time(hour=hours % 24, minute=mins)


def time_to_minutes(value: datetime.time) -> int:
    """Convert a `datetime.time` (UI) to minutes-since-local-midnight (wire)."""
    return value.hour * 60 + value.minute


def device_type_to_bowl_size_inches(device_type: int) -> int:
    """Convert the vendor's INVERTED `deviceType` enum to a size in inches.

    0 -> 9", 1 -> 7". Any other value is treated as the 9" default rather
    than raising, since an unrecognised device type should not take down a
    coordinator update.
    """
    return BOWL_SIZE_7_INCH if device_type == DEVICE_TYPE_7_INCH else BOWL_SIZE_9_INCH


def bowl_size_inches_to_device_type(inches: int) -> int:
    """Convert a bowl size in inches (UI) to the vendor's inverted enum (wire)."""
    return DEVICE_TYPE_7_INCH if inches == BOWL_SIZE_7_INCH else DEVICE_TYPE_9_INCH


def filter_life_percent(state: dict[str, Any]) -> float | None:
    """Return remaining filter life as a percentage, or `None` if disabled.

    Computed from `filterUsedTime` / `filterCanUseTime` because
    `/app/device/detail` does not return a usable `filterDueState` field.
    `filterCanUseTime == 0` means the feature is disabled on this bowl (the
    live-observed value); dividing by it would raise `ZeroDivisionError` and
    take down the whole coordinator update, so this returns `None` instead
    of raising or fabricating a 0/100 value.
    """
    can_use = state.get("filterCanUseTime") or 0
    if not can_use:
        return None
    used = state.get("filterUsedTime") or 0
    remaining = max(can_use - used, 0)
    return remaining / can_use * 100


# -- BowlState: read-only device/detail & device/list rows -------------------


@dataclasses.dataclass(slots=True)
class BowlState:
    """Read-only device state, as returned by `/app/device/detail` or `/app/device/list`.

    Detail and list rows share the same shape (per the design doc), so this
    parses either. `filterDueState` is intentionally not modeled -- see the
    module docstring.
    """

    device_id: str | None = None
    device_name: str | None = None
    status: int = 0
    slave_type: int = SLAVE_TYPE_NONE
    device_type_raw: int = DEVICE_TYPE_9_INCH
    units: int = UNITS_MILLILITRES
    filter_used_time: int = 0
    device_used_time: int = 0
    filter_state: int = 1
    system_suspended: int = 0
    hardware_failure: int = 0
    injection_alarm: int = 0
    pump_alarm: int = 0
    horizontal_alarm: int = 0
    offline: bool = False

    @classmethod
    def from_api(cls, row: dict[str, Any]) -> BowlState:
        """Build a `BowlState` from a raw `/app/device/detail`-or-`/list` row."""
        return cls(
            device_id=row.get("deviceId"),
            device_name=row.get("deviceName"),
            status=row.get("status", 0),
            slave_type=row.get("slaveType", SLAVE_TYPE_NONE),
            device_type_raw=row.get("deviceType", DEVICE_TYPE_9_INCH),
            units=row.get("units", UNITS_MILLILITRES),
            filter_used_time=row.get("filterUsedTime", 0),
            device_used_time=row.get("deviceUsedTime", 0),
            filter_state=row.get("filterState", 1),
            system_suspended=row.get("systemSuspended", 0),
            hardware_failure=row.get("hardwareFailure", 0),
            injection_alarm=row.get("injectionAlarm", 0),
            pump_alarm=row.get("pumpAlarm", 0),
            horizontal_alarm=row.get("horizontalAlarm", 0),
            offline=bool(row.get("offline", False)),
        )

    @property
    def bowl_size_inches(self) -> int:
        """9 or 7, decoded from the vendor's inverted `deviceType` enum."""
        return device_type_to_bowl_size_inches(self.device_type_raw)

    @property
    def is_online(self) -> bool:
        """`status == 1` means online; anything else (including unknown) is offline."""
        return self.status == 1

    @property
    def has_system_problem(self) -> bool:
        """The vendor app only ever tests these `== 1`, never as a bitmask."""
        return self.system_suspended == 1 or self.hardware_failure == 1

    @property
    def has_injection_alarm(self) -> bool:
        """Return whether the bowl reports a water-fill alarm."""
        return self.injection_alarm == 1

    @property
    def has_pump_alarm(self) -> bool:
        """Return whether the bowl reports a pump alarm."""
        return self.pump_alarm == 1

    @property
    def has_horizontal_alarm(self) -> bool:
        """Return whether the bowl reports being off level."""
        return self.horizontal_alarm == 1

    @property
    def has_filter_fault(self) -> bool:
        """`filterState == 0` is a fault; `1` is healthy."""
        return self.filter_state == 0


# -- BowlConfig: read-modify-write device/config -----------------------------


@dataclasses.dataclass(slots=True)
class BowlConfig:
    """Mutable device config, as returned by `/app/device/config`.

    Each `to_*_payload()` method reconstructs the FULL wire object for its
    endpoint from this dataclass's current state, because `sleepConfig`,
    `waterConfig`, `logConfig` (and every other config writer here, to be
    safe) are read-modify-write over the whole object -- never send a
    partial. Change a UI-format property (e.g. `flush_interval_minutes`)
    then call the matching `to_*_payload()`; the wire-format field it backs
    (`cleanCycle`) is what actually gets sent.

    `headLength`, `bodyLength`, `seq`, `msgCode` (raw protocol framing) are
    never read into this dataclass, so they can never be echoed back by any
    `to_*_payload()` method.
    """

    device_id: str | None = None

    # flushConfig group
    clean_cycle_seconds: int = 0  # cleanCycle: flush interval
    clean_time: int | None = None  # cleanTime: flush duration (no unit conversion)
    fill_wash_state: int | None = None  # fillWashState: flush-after-fill switch

    # sleepConfig group
    sleep_start_minutes: int = 0  # sleepStart
    sleep_end_minutes: int = 0  # sleepEnd
    sleep_state: int | None = None  # sleepState

    # filterConfig group -- verbatim from the vendor app: {devNo, filterCanUseTime, filterCapacity}
    filter_can_use_time_seconds: int = 0  # filterCanUseTime
    filter_capacity_raw: int | None = None  # capacity (read) / filterCapacity (write)

    # maintenanceConfig group -- verbatim from the vendor app: {devNo, deviceCanUseTime, cleanWarnTime}
    device_can_use_time_seconds: int = 0  # deviceCanUseTime
    clean_warn_time_seconds: int | None = None  # cleanWarnTime

    # waterConfig group (units is echoed in from device state, not stored here)
    day_min_water: int = 0  # dayMinWater
    day_max_water: int = 0  # dayMaxWater

    # logConfig group
    log_state: int | None = None  # logState

    # Also present in device/config; not part of any *_payload() here --
    # bowl size is set via the dedicated `set_device_type` writer, not a
    # config group -- but exposed as a convenience read property.
    device_type_raw: int | None = None  # deviceType

    @classmethod
    def from_api(cls, cfg: dict[str, Any]) -> BowlConfig:
        """Build a `BowlConfig` from a raw `/app/device/config` object.

        Accepts partial dicts (only the keys under test) as well as the
        full fixture -- every field not present defaults sensibly.
        """
        return cls(
            device_id=cfg.get("devNo") or cfg.get("deviceId"),
            clean_cycle_seconds=cfg.get("cleanCycle", 0),
            clean_time=cfg.get("cleanTime"),
            fill_wash_state=cfg.get("fillWashState"),
            sleep_start_minutes=cfg.get("sleepStart", 0),
            sleep_end_minutes=cfg.get("sleepEnd", 0),
            sleep_state=cfg.get("sleepState"),
            filter_can_use_time_seconds=cfg.get("filterCanUseTime", 0),
            filter_capacity_raw=cfg.get("capacity"),
            clean_warn_time_seconds=cfg.get("cleanWarnTime"),
            device_can_use_time_seconds=cfg.get("deviceCanUseTime", 0),
            day_min_water=cfg.get("dayMinWater", 0),
            day_max_water=cfg.get("dayMaxWater", 0),
            log_state=cfg.get("logState"),
            device_type_raw=cfg.get("deviceType"),
        )

    # -- UI-format properties: flush ---------------------------------------

    @property
    def flush_interval_minutes(self) -> int:
        """Return the flush interval in minutes (stored as seconds)."""
        return self.clean_cycle_seconds // SECONDS_PER_MINUTE

    @flush_interval_minutes.setter
    def flush_interval_minutes(self, minutes: int) -> None:
        self.clean_cycle_seconds = minutes * SECONDS_PER_MINUTE

    # -- UI-format properties: filter ---------------------------------------

    @property
    def filter_life_months(self) -> int:
        """Return the filter life in months (stored as seconds)."""
        return self.filter_can_use_time_seconds // SECONDS_PER_MONTH

    @filter_life_months.setter
    def filter_life_months(self, months: int) -> None:
        self.filter_can_use_time_seconds = months * SECONDS_PER_MONTH

    @property
    def filter_capacity_ml(self) -> int | None:
        """Return the filter capacity in millilitres, or `None` if unset."""
        return self.filter_capacity_raw

    @filter_capacity_ml.setter
    def filter_capacity_ml(self, value: int) -> None:
        self.filter_capacity_raw = value

    # -- UI-format properties: maintenance -----------------------------------

    @property
    def maintenance_interval_days(self) -> int:
        """Return the maintenance interval in days (stored as seconds)."""
        return self.device_can_use_time_seconds // SECONDS_PER_DAY

    @maintenance_interval_days.setter
    def maintenance_interval_days(self, days: int) -> None:
        self.device_can_use_time_seconds = days * SECONDS_PER_DAY

    # -- UI-format properties: sleep -----------------------------------------

    @property
    def sleep_start(self) -> datetime.time:
        """Return the sleep-window start as a time of day."""
        return minutes_to_time(self.sleep_start_minutes)

    @sleep_start.setter
    def sleep_start(self, value: datetime.time) -> None:
        self.sleep_start_minutes = time_to_minutes(value)

    @property
    def sleep_end(self) -> datetime.time:
        """Return the sleep-window end as a time of day."""
        return minutes_to_time(self.sleep_end_minutes)

    @sleep_end.setter
    def sleep_end(self, value: datetime.time) -> None:
        self.sleep_end_minutes = time_to_minutes(value)

    # -- UI-format properties: bowl size (config also carries deviceType) ---

    @property
    def bowl_size_inches(self) -> int | None:
        """Return the bowl size in inches, or `None` if the config omits it."""
        if self.device_type_raw is None:
            return None
        return device_type_to_bowl_size_inches(self.device_type_raw)

    # -- Wire payload builders -----------------------------------------------
    #
    # Each returns exactly the dict meant to be splatted into the matching
    # `AlwaysFullClient` writer, e.g.
    # `await client.set_flush_config(**cfg.to_flush_payload(device_id))`.
    # The `device_id` key matches that writer's `device_id` parameter name;
    # Task 2's writers own translating it to the wire's `devNo`/`deviceId`
    # key for that specific endpoint.

    def to_flush_payload(self, device_id: str) -> dict[str, Any]:
        """Whole-object payload for `set_flush_config` (`/app/device/flushConfig`)."""
        return {
            "device_id": device_id,
            "cleanCycle": self.clean_cycle_seconds,
            "cleanTime": self.clean_time,
            "fillWashState": self.fill_wash_state,
        }

    def to_sleep_payload(self, device_id: str) -> dict[str, Any]:
        """Whole-object payload for `set_sleep_config` (`/app/device/sleepConfig`)."""
        return {
            "device_id": device_id,
            "sleepStart": self.sleep_start_minutes,
            "sleepEnd": self.sleep_end_minutes,
            "sleepState": self.sleep_state,
        }

    def to_filter_payload(self, device_id: str) -> dict[str, Any]:
        """Whole-object payload for `set_filter_config` (`/app/device/filterConfig`).

        Sends `filterCapacity`, never `capacity` -- the single easiest thing
        to get wrong in this whole integration. Verbatim from the vendor's
        decompiled app, this endpoint's body is exactly
        `{devNo, filterCanUseTime, filterCapacity}` -- `cleanWarnTime` is
        NOT part of it (that belongs to `maintenanceConfig` -- see
        `to_maintenance_payload`).
        """
        return {
            "device_id": device_id,
            "filterCanUseTime": self.filter_can_use_time_seconds,
            "filterCapacity": self.filter_capacity_raw,
        }

    def to_maintenance_payload(self, device_id: str) -> dict[str, Any]:
        """Whole-object payload for `set_maintenance_config` (`/app/device/maintenanceConfig`).

        Verbatim from the vendor's decompiled app, this endpoint's body is
        exactly `{devNo, deviceCanUseTime, cleanWarnTime}`.

        `cleanWarnTime` has NO unit conversion in this module, in either
        direction: it is read as raw seconds and written back as the same
        raw seconds. An earlier version of this docstring claimed a
        seconds<->30-day-months conversion existed here; it never did, and
        anyone who believed it and divided by 2592000 on the way out would
        turn a one-week warning into 0. Nothing exposes this field to the
        user, so handing the device's own value straight back is the only
        correct behaviour. If a future task DOES expose it, the conversion
        has to be written -- it cannot be assumed.

        `deviceCanUseTime`, by contrast, IS converted (seconds <-> days)
        via `maintenance_interval_days`.
        """
        return {
            "device_id": device_id,
            "deviceCanUseTime": self.device_can_use_time_seconds,
            "cleanWarnTime": self.clean_warn_time_seconds,
        }

    def to_log_payload(self, device_id: str) -> dict[str, Any]:
        """Whole-object payload for `set_log_config` (`/app/device/logConfig`).

        `logState` is the only setting this endpoint carries, which makes
        "whole object" and "just this field" identical TODAY. It is still
        built here rather than inline at the call site, so that the day the
        vendor adds a second field to the group there is one place to add
        it and every caller picks it up.
        """
        return {
            "device_id": device_id,
            "logState": self.log_state,
        }

    def to_water_payload(self, device_id: str, units: int) -> dict[str, Any]:
        """Whole-object payload for `set_water_config` (`/app/device/waterConfig`).

        `units` must be echoed from the device's own state (`BowlState.units`
        / `device/detail`'s `units`), never guessed, or the server misreads
        the thresholds.

        Raises `ValueError` if `dayMinWater >= dayMaxWater` unless BOTH are
        exactly 0 (which disables the thresholds) -- the server rejects this
        combination, and silently no-op'ing it would leave the user thinking
        they'd set a threshold that was never applied.
        """
        min_water, max_water = self.day_min_water, self.day_max_water
        both_disabled = min_water == 0 and max_water == 0
        if not both_disabled and not (min_water < max_water):
            msg = (
                f"dayMinWater ({min_water}) must be < dayMaxWater ({max_water}), "
                "or both must be exactly 0 to disable the thresholds"
            )
            raise ValueError(msg)
        return {
            "device_id": device_id,
            "dayMinWater": min_water,
            "dayMaxWater": max_water,
            "units": units,
        }
