"""Tests for domain models and wire<->UI unit conversions.

Every numeric ground truth here was observed against a real bowl (see the
task brief / design doc "Live-verified findings"), not invented: a missed
conversion does not error, it silently sets a flush interval to 60 seconds
instead of 60 minutes. Each conversion is asserted read (wire -> UI) and
write (UI/stored -> wire) so a one-directional fix can't hide a break in
the other direction.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest
from homeassistant.const import UnitOfVolume

from custom_components.alwaysfull.models import (
    UNITS_FLUID_OUNCES,
    UNITS_MILLILITRES,
    BowlConfig,
    BowlState,
    bowl_size_inches_to_device_type,
    device_type_to_bowl_size_inches,
    filter_life_percent,
    minutes_to_time,
    parse_device_timestamp,
    time_to_minutes,
    water_unit,
)

FIX = pathlib.Path(__file__).parent / "fixtures"


def fx(name: str) -> dict:
    """Load a committed fixture's `data` object by filename."""
    return json.loads((FIX / name).read_text())["data"]


# -- Sleep minutes <-> datetime.time -----------------------------------


def test_sleep_minutes_round_trip():
    assert minutes_to_time(1320) == datetime.time(22, 0)  # observed live
    assert minutes_to_time(360) == datetime.time(6, 0)  # observed live
    assert time_to_minutes(datetime.time(22, 0)) == 1320
    assert time_to_minutes(datetime.time(6, 0)) == 360


def test_bowl_config_sleep_properties_round_trip():
    cfg = BowlConfig.from_api({"sleepStart": 1320, "sleepEnd": 360})
    assert cfg.sleep_start == datetime.time(22, 0)
    assert cfg.sleep_end == datetime.time(6, 0)

    cfg.sleep_start = datetime.time(23, 30)
    cfg.sleep_end = datetime.time(5, 15)
    assert cfg.sleep_start_minutes == 1410
    assert cfg.sleep_end_minutes == 315
    payload = cfg.to_sleep_payload("DEV")
    assert payload["sleepStart"] == 1410
    assert payload["sleepEnd"] == 315
    assert payload["device_id"] == "DEV"


# -- Flush interval: cleanCycle seconds <-> minutes ----------------------


def test_clean_cycle_seconds_to_minutes():
    cfg = BowlConfig.from_api({"cleanCycle": 3600})  # observed live
    assert cfg.flush_interval_minutes == 60
    assert cfg.to_flush_payload("DEV")["cleanCycle"] == 3600


def test_flush_interval_minutes_setter_writes_seconds():
    cfg = BowlConfig.from_api({"cleanCycle": 3600})
    cfg.flush_interval_minutes = 90
    assert cfg.to_flush_payload("DEV")["cleanCycle"] == 5400


def test_clean_time_flush_duration_passes_through_unconverted():
    # The vendor sends cleanTime raw -- no seconds<->minutes conversion.
    # Pinned so nobody later "fixes" this into a unit conversion it doesn't need.
    cfg = BowlConfig.from_api({"cleanTime": 25})  # observed live
    assert cfg.clean_time == 25
    assert cfg.to_flush_payload("DEV")["cleanTime"] == 25

    cfg.clean_time = 99
    assert cfg.to_flush_payload("DEV")["cleanTime"] == 99


# -- Filter life: filterCanUseTime seconds <-> 30-day "months" ----------


def test_filter_life_months_conversion():
    cfg = BowlConfig.from_api({"filterCanUseTime": 10512000})  # observed live
    assert cfg.filter_life_months == 4  # 10512000 // 2592000


def test_filter_life_months_setter_writes_seconds():
    cfg = BowlConfig.from_api({"filterCanUseTime": 10512000})
    cfg.filter_life_months = 6
    assert cfg.to_filter_payload("DEV")["filterCanUseTime"] == 6 * 2592000


# -- Maintenance interval: deviceCanUseTime seconds <-> days ------------


def test_device_can_use_time_seconds_to_days():
    cfg = BowlConfig.from_api({"deviceCanUseTime": 0})  # observed live: disabled
    assert cfg.maintenance_interval_days == 0
    assert cfg.to_maintenance_payload("DEV")["deviceCanUseTime"] == 0


def test_maintenance_interval_days_setter_writes_seconds():
    cfg = BowlConfig.from_api({"deviceCanUseTime": 0})
    cfg.maintenance_interval_days = 30
    assert cfg.to_maintenance_payload("DEV")["deviceCanUseTime"] == 30 * 86400


def test_clean_warn_time_belongs_to_maintenance_not_filter():
    # Verbatim from the vendor's decompiled app:
    #   maintenanceConfig: {devNo, deviceCanUseTime, cleanWarnTime}
    #   filterConfig:      {devNo, filterCanUseTime, filterCapacity}
    # cleanWarnTime is a maintenanceConfig field, not a filterConfig one --
    # the vendor app always hard-codes it to 0 and never shows it on any
    # screen, but the wire endpoint that accepts it is maintenanceConfig.
    cfg = BowlConfig.from_api(
        {"cleanWarnTime": 2592000, "filterCanUseTime": 10512000, "deviceCanUseTime": 0}
    )
    maintenance_payload = cfg.to_maintenance_payload("DEV")
    filter_payload = cfg.to_filter_payload("DEV")

    assert maintenance_payload["cleanWarnTime"] == 2592000
    assert "cleanWarnTime" not in filter_payload


# -- Filter capacity: read `capacity`, write `filterCapacity` -----------


def test_filter_capacity_reads_capacity_writes_filterCapacity():
    cfg = BowlConfig.from_api({"capacity": 378541})  # observed live
    assert cfg.filter_capacity_ml == 378541
    assert cfg.to_filter_payload("DEV")["filterCapacity"] == 378541
    assert "capacity" not in cfg.to_filter_payload("DEV")


# -- Filter life percent --------------------------------------------------


def test_filter_life_percent_from_used_and_total():
    pct = filter_life_percent(
        {"filterUsedTime": 184467, "filterCanUseTime": 10512000}
    )  # observed live
    assert round(pct, 1) == 98.2


def test_filter_life_percent_is_none_without_total():
    assert filter_life_percent({"filterUsedTime": 10, "filterCanUseTime": 0}) is None


def test_filter_life_percent_missing_keys_is_none():
    assert filter_life_percent({}) is None


def test_filter_life_percent_never_raises_on_zero_total():
    # Explicit division-by-zero guard: must not raise, must not fabricate 0/100.
    try:
        result = filter_life_percent({"filterUsedTime": 999, "filterCanUseTime": 0})
    except ZeroDivisionError:
        pytest.fail("filter_life_percent must not raise on filterCanUseTime == 0")
    assert result is None


# -- Water thresholds: dayMinWater/dayMaxWater guard ---------------------


def test_water_payload_requires_min_below_max():
    cfg = BowlConfig.from_api({"dayMinWater": 2000, "dayMaxWater": 7500})
    assert cfg.to_water_payload("DEV", units=1)["dayMinWater"] == 2000
    assert cfg.to_water_payload("DEV", units=1)["dayMaxWater"] == 7500
    assert cfg.to_water_payload("DEV", units=1)["units"] == 1


def test_water_payload_allows_both_zero_to_disable():
    cfg = BowlConfig.from_api({"dayMinWater": 0, "dayMaxWater": 0})
    payload = cfg.to_water_payload("DEV", units=1)
    assert payload["dayMinWater"] == 0
    assert payload["dayMaxWater"] == 0


def test_water_payload_rejects_min_equal_max():
    cfg = BowlConfig.from_api({"dayMinWater": 5000, "dayMaxWater": 5000})
    with pytest.raises(ValueError, match="dayMinWater"):
        cfg.to_water_payload("DEV", units=1)


def test_water_payload_rejects_min_above_max():
    cfg = BowlConfig.from_api({"dayMinWater": 8000, "dayMaxWater": 2000})
    with pytest.raises(ValueError, match="dayMinWater"):
        cfg.to_water_payload("DEV", units=1)


def test_water_payload_rejects_one_sided_zero():
    # Only BOTH exactly 0 disables the guard -- one-sided zero is still bad input.
    cfg = BowlConfig.from_api({"dayMinWater": 0, "dayMaxWater": 5000})
    payload = cfg.to_water_payload("DEV", units=1)
    assert payload["dayMinWater"] == 0  # min == 0 < max is legitimately valid

    cfg2 = BowlConfig.from_api({"dayMinWater": 5000, "dayMaxWater": 0})
    with pytest.raises(ValueError, match="dayMinWater"):
        cfg2.to_water_payload("DEV", units=1)


# -- Exact wire key set per payload (vendor app decompile, verbatim) -----
#
# Presence-only assertions ("the right key is there") don't catch a field
# ending up in the WRONG payload if that payload also happens to have the
# right keys -- which is exactly how cleanWarnTime ended up wrongly emitted
# by to_filter_payload in an earlier pass. These assert the full key set,
# so an extra/misplaced key fails even if every expected key is present.


def test_to_flush_payload_exact_key_set():
    cfg = BowlConfig.from_api(fx("device_config.json"))
    payload = cfg.to_flush_payload("DEV")
    assert set(payload) == {"device_id", "cleanCycle", "cleanTime", "fillWashState"}


def test_to_sleep_payload_exact_key_set():
    cfg = BowlConfig.from_api(fx("device_config.json"))
    payload = cfg.to_sleep_payload("DEV")
    assert set(payload) == {"device_id", "sleepStart", "sleepEnd", "sleepState"}


def test_to_filter_payload_exact_key_set():
    # Vendor decompiled app, verbatim: {devNo, filterCanUseTime, filterCapacity}.
    cfg = BowlConfig.from_api(fx("device_config.json"))
    payload = cfg.to_filter_payload("DEV")
    assert set(payload) == {"device_id", "filterCanUseTime", "filterCapacity"}


def test_to_maintenance_payload_exact_key_set():
    # Vendor decompiled app, verbatim: {devNo, deviceCanUseTime, cleanWarnTime}.
    cfg = BowlConfig.from_api(fx("device_config.json"))
    payload = cfg.to_maintenance_payload("DEV")
    assert set(payload) == {"device_id", "deviceCanUseTime", "cleanWarnTime"}


def test_to_water_payload_exact_key_set():
    cfg = BowlConfig.from_api(fx("device_config.json"))
    payload = cfg.to_water_payload("DEV", units=1)
    assert set(payload) == {"device_id", "dayMinWater", "dayMaxWater", "units"}


def test_no_payload_carries_a_none_for_the_committed_fixture():
    # `api.py` strips None from a body BEFORE signing it, so a payload
    # value of None is not sent as JSON null -- the key vanishes, and the
    # whole-object write silently becomes the partial write that the
    # ConfigGroup design exists to prevent. Latent today (the live capture
    # fills all six optional fields), which is exactly why it needs a test:
    # a future field defaulting to None would ship a partial with nothing
    # red.
    cfg = BowlConfig.from_api(fx("device_config.json"))
    payloads = {
        "flush": cfg.to_flush_payload("DEV"),
        "sleep": cfg.to_sleep_payload("DEV"),
        "filter": cfg.to_filter_payload("DEV"),
        "maintenance": cfg.to_maintenance_payload("DEV"),
        "water": cfg.to_water_payload("DEV", units=1),
        "log": cfg.to_log_payload("DEV"),
    }
    for group, payload in payloads.items():
        for key, value in payload.items():
            assert value is not None, f"{group}: {key} is None and would be dropped on the wire"


def test_a_none_field_would_be_dropped_from_the_signed_body():
    # Negative control for the test above: this is what the transport does
    # to a None, so the rule being asserted is a real one and not a
    # stylistic preference.
    body = {"devNo": "DEV", "cleanCycle": 3600, "cleanTime": None}
    stripped = {key: value for key, value in body.items() if value is not None}
    assert "cleanTime" not in stripped


def test_to_log_payload_exact_key_set():
    # logConfig carries one setting, and it is still a whole-object write.
    cfg = BowlConfig.from_api(fx("device_config.json"))
    payload = cfg.to_log_payload("DEV")
    assert set(payload) == {"device_id", "logState"}
    assert payload["logState"] == 0


def test_to_log_payload_round_trips_the_flag_as_an_int():
    # The vendor's flags are ints. `True`/`False` are not the same wire
    # values, and JSON would serialise them as `true`/`false`.
    cfg = BowlConfig.from_api({"logState": 1})
    payload = cfg.to_log_payload("DEV")
    assert payload["logState"] == 1
    assert payload["logState"] is not True


def test_clean_warn_time_is_raw_seconds_with_no_month_conversion():
    # There is NO seconds<->months conversion for cleanWarnTime anywhere in
    # this module, and a docstring once implied there was. One week in must
    # be one week out: a 30-day-month conversion applied here would send 0
    # instead of 604800, an error of a factor of 2,592,000.
    cfg = BowlConfig.from_api({"cleanWarnTime": 604800, "deviceCanUseTime": 864000})
    assert cfg.clean_warn_time_seconds == 604800
    assert cfg.to_maintenance_payload("DEV")["cleanWarnTime"] == 604800


# -- deviceType: inverted 9"/7" enum --------------------------------------


def test_device_type_enum_is_inverted():
    assert BowlState.from_api({"deviceType": 0}).bowl_size_inches == 9
    assert BowlState.from_api({"deviceType": 1}).bowl_size_inches == 7


def test_device_type_conversion_functions_both_directions():
    assert device_type_to_bowl_size_inches(0) == 9
    assert device_type_to_bowl_size_inches(1) == 7
    assert bowl_size_inches_to_device_type(9) == 0
    assert bowl_size_inches_to_device_type(7) == 1


def test_bowl_config_also_exposes_bowl_size_from_device_type():
    cfg = BowlConfig.from_api({"deviceType": 1})
    assert cfg.bowl_size_inches == 7


# -- BowlState: status / alarms / slaveType -------------------------------


def test_bowl_state_online_flag():
    assert BowlState.from_api({"status": 1}).is_online is True
    assert BowlState.from_api({"status": 0}).is_online is False


def test_bowl_state_alarm_ints_tested_strictly_equal_one():
    state = BowlState.from_api(
        {
            "systemSuspended": 1,
            "hardwareFailure": 0,
            "injectionAlarm": 1,
            "pumpAlarm": 0,
            "horizontalAlarm": 1,
        }
    )
    assert state.has_system_problem is True  # systemSuspended alone trips it
    assert state.has_injection_alarm is True
    assert state.has_pump_alarm is False
    assert state.has_horizontal_alarm is True


def test_bowl_state_no_alarms_when_all_zero():
    state = BowlState.from_api(
        {
            "systemSuspended": 0,
            "hardwareFailure": 0,
            "injectionAlarm": 0,
            "pumpAlarm": 0,
            "horizontalAlarm": 0,
        }
    )
    assert state.has_system_problem is False
    assert state.has_injection_alarm is False
    assert state.has_pump_alarm is False
    assert state.has_horizontal_alarm is False


def test_bowl_state_filter_fault_from_filter_state_zero():
    assert BowlState.from_api({"filterState": 0}).has_filter_fault is True
    assert BowlState.from_api({"filterState": 1}).has_filter_fault is False


# -- Protocol-framing fields must never be modeled or echoed back --------


def test_protocol_framing_fields_are_not_echoed_in_any_payload():
    cfg = BowlConfig.from_api(fx("device_config.json"))
    for payload in (
        cfg.to_flush_payload("DEV"),
        cfg.to_sleep_payload("DEV"),
        cfg.to_filter_payload("DEV"),
        cfg.to_maintenance_payload("DEV"),
        cfg.to_water_payload("DEV", units=1),
        cfg.to_log_payload("DEV"),
    ):
        for framing_key in ("headLength", "bodyLength", "seq", "msgCode"):
            assert framing_key not in payload


# -- Full-fixture integration: real captured values ----------------------


def test_bowl_config_from_real_device_config_fixture():
    cfg = BowlConfig.from_api(fx("device_config.json"))
    assert cfg.flush_interval_minutes == 60
    assert cfg.filter_life_months == 4
    assert cfg.filter_capacity_ml == 378541
    assert cfg.maintenance_interval_days == 0
    assert cfg.sleep_start == datetime.time(22, 0)
    assert cfg.sleep_end == datetime.time(6, 0)
    assert cfg.bowl_size_inches == 9

    filter_payload = cfg.to_filter_payload("aabbccddeeff")
    assert filter_payload["filterCapacity"] == 378541
    assert "capacity" not in filter_payload


def test_bowl_state_carries_the_connection_stamps_verbatim():
    """`onlineTime` / `offlineTime` are kept as the vendor's own strings.

    The model stays the record of what arrived on the wire; interpreting
    it is `parse_device_timestamp`'s job, one layer up. Keeping the raw
    string here means the diagnostics dump and the parsed sensor state can
    be compared against each other if the vendor ever changes the format.
    """
    state = BowlState.from_api(
        {"onlineTime": "2026-09-15 22:08:57", "offlineTime": "2026-09-14 03:10:00"}
    )
    assert state.online_time == "2026-09-15 22:08:57"
    assert state.offline_time == "2026-09-14 03:10:00"


def test_bowl_state_connection_stamps_default_to_none():
    """`offlineTime` is null while a bowl is connected, and null is not "".

    A missing key and an explicit `null` must both read as `None`: the
    vendor sends `null` for whichever of the pair does not apply, and a
    never-connected bowl carries `null` for both.
    """
    assert BowlState.from_api({}).online_time is None
    assert BowlState.from_api({}).offline_time is None
    assert BowlState.from_api({"onlineTime": None, "offlineTime": None}).online_time is None
    assert BowlState.from_api({"onlineTime": None, "offlineTime": None}).offline_time is None


def test_bowl_state_from_real_device_detail_fixture():
    state = BowlState.from_api(fx("device_detail.json"))
    assert state.is_online is True
    assert state.bowl_size_inches == 9
    assert state.filter_used_time == 184467
    assert state.device_used_time == 184468
    assert state.online_time == "2026-09-15 22:08:57"
    assert state.offline_time is None
    assert state.has_system_problem is False
    assert state.has_injection_alarm is False
    assert state.has_pump_alarm is False
    assert state.has_horizontal_alarm is False
    assert state.has_filter_fault is False


def test_filter_life_percent_from_combined_real_fixtures():
    detail = fx("device_detail.json")
    config = fx("device_config.json")
    pct = filter_life_percent(
        {
            "filterUsedTime": detail["filterUsedTime"],
            "filterCanUseTime": config["filterCanUseTime"],
        }
    )
    assert round(pct, 1) == 98.2


@pytest.mark.parametrize(
    ("units", "expected"),
    [
        (UNITS_MILLILITRES, UnitOfVolume.MILLILITERS),
        (UNITS_FLUID_OUNCES, UnitOfVolume.FLUID_OUNCES),
        # Anything else falls back to millilitres rather than raising: an
        # unrecognised `units` must not take down a coordinator update, and
        # millilitres is the vendor's own default (`units` 1).
        (0, UnitOfVolume.MILLILITERS),
        (99, UnitOfVolume.MILLILITERS),
    ],
)
def test_water_unit_maps_the_vendor_enum(units: int, expected: str) -> None:
    """The bowl's `units` enum decides the unit, in ONE place."""
    assert water_unit(units) == expected
    assert BowlState(units=units).water_unit == expected


@pytest.mark.parametrize(
    ("device_type", "expected"),
    [
        (0, 9),
        (1, 7),
        # Everything else falls back to 9", the vendor's own `deviceType`
        # 0. Deliberate, and now pinned: an unrecognised device type
        # arriving mid-poll must not raise inside a coordinator update,
        # where it would break every entity on the bowl. The fallback is
        # only reached by a value the vendor has never sent.
        (2, 9),
        (-1, 9),
        (99, 9),
    ],
)
def test_an_unrecognised_device_type_falls_back_to_the_nine_inch_bowl(
    device_type: int, expected: int
) -> None:
    """The inverted enum decodes 0/1, and never raises on anything else."""
    assert device_type_to_bowl_size_inches(device_type) == expected
    assert BowlState(device_type_raw=device_type).bowl_size_inches == expected
    assert BowlConfig(device_type_raw=device_type).bowl_size_inches == expected


@pytest.mark.parametrize(
    ("inches", "expected"),
    [
        (9, 0),
        (7, 1),
        # The write direction falls back the same way, and is reachable
        # only from a caller that invented a size: the select platform
        # offers exactly two options.
        (12, 0),
        (0, 0),
    ],
)
def test_an_unrecognised_bowl_size_writes_the_nine_inch_enum(inches: int, expected: int) -> None:
    """Inches -> the vendor's INVERTED enum, with the same 9" fallback."""
    assert bowl_size_inches_to_device_type(inches) == expected


def test_the_bowl_size_helpers_round_trip_both_real_bowls() -> None:
    """Both directions agree for the two sizes that exist.

    The fallbacks above are each other's blind spot: "always return 9" and
    "always return 0" satisfy one of those tests apiece.
    """
    for inches in (9, 7):
        assert device_type_to_bowl_size_inches(bowl_size_inches_to_device_type(inches)) == inches


def test_a_config_that_omits_the_device_type_reports_no_bowl_size() -> None:
    """`None` is NOT the fallback case: the config simply did not say.

    `BowlConfig.device_type_raw` defaults to `None` for a config object
    that omitted `deviceType`, and that is distinct from an unrecognised
    value -- nothing was claimed, so nothing is reported.
    """
    assert BowlConfig.from_api({}).bowl_size_inches is None


# -- The connection stamps are UTC ----------------------------------------
#
# The zone was an open question until it was measured; the evidence and the
# negative result that pins it are written up in `docs/VENDOR-API.md` §3.3.
# These tests are what stops the parse drifting back to a local reading,
# which is a silent hours-off error rather than a visible failure.


def test_parse_device_timestamp_reads_a_naive_vendor_stamp_as_utc():
    """The vendor's naive `"YYYY-MM-DD HH:MM:SS"` is UTC, verified.

    Asserted on the OFFSET as well as the instant, and both halves are
    needed. A parse in the host's local zone gives a different instant, so
    the equality catches it. A parse in UTC followed by a conversion to
    local gives the SAME instant with a non-zero offset, which the
    equality would happily accept -- and which would then be published to
    Home Assistant as a local-zone timestamp, re-rendered against the
    viewer's zone.
    """
    parsed = parse_device_timestamp("2026-09-15 22:08:57")
    assert parsed == datetime.datetime(2026, 9, 15, 22, 8, 57, tzinfo=datetime.UTC)
    assert parsed is not None
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_parse_device_timestamp_returns_an_aware_datetime():
    """A naive datetime must never leave this function.

    Home Assistant RAISES on a naive value under `device_class: timestamp`
    ("which is missing timezone information"), and it raises inside the
    state write -- so this is the check that keeps the awareness from being
    dropped by a later edit that still looks correct.
    """
    parsed = parse_device_timestamp("2026-09-15 22:08:57")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.tzinfo.utcoffset(parsed) is not None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not a timestamp",
        # Matches the shape and is still not a date. Worth its own case:
        # `datetime.fromisoformat` raises `ValueError` from the FIELD
        # values here rather than from the format, which is a second
        # failure path through the same call.
        "2026-13-45 99:99:99",
        "2026",
        # Non-strings. `fromisoformat` raises `TypeError`, not
        # `ValueError`, so a `try/except ValueError` alone would let a
        # vendor field that arrived as a number or a container take down
        # the whole coordinator update.
        1758000537,
        ["2026-09-15 22:08:57"],
        {"onlineTime": "2026-09-15 22:08:57"},
    ],
)
def test_parse_device_timestamp_reports_nothing_rather_than_raising(value):
    """Anything unparseable is `None` -- never an exception, never epoch.

    `None` is what makes Home Assistant say `unknown`. The two wrong
    answers this rules out are an exception (which fires inside a
    coordinator listener and breaks the update for every other entity on
    the bowl) and a fallback instant such as the epoch or `utcnow()`,
    which reads as a real connection time that never happened.
    """
    assert parse_device_timestamp(value) is None


def test_parse_device_timestamp_keeps_an_explicit_zone_when_one_is_sent():
    """A stamp that carries its own zone is trusted over the UTC default.

    The vendor already uses an explicitly-UTC format on its notify rows
    (`2026-09-15T22:08:57Z`), so the device row acquiring an offset one day
    is a realistic change rather than a hypothetical. UTC is the default
    for a stamp that states NO zone; it does not override one that does.
    """
    assert parse_device_timestamp("2026-09-15T22:08:57Z") == datetime.datetime(
        2026, 9, 15, 22, 8, 57, tzinfo=datetime.UTC
    )
    # Same instant, stated as an offset rather than as `Z`.
    assert parse_device_timestamp("2026-09-16T00:08:57+02:00") == datetime.datetime(
        2026, 9, 15, 22, 8, 57, tzinfo=datetime.UTC
    )


def test_parse_device_timestamp_agrees_with_the_vendors_own_utc_format():
    """The two evidence rows that settled the zone, asserted against each other.

    `onlineTime` reads `2026-09-15 22:08:57` on the live account, and the
    bowl's first-ever `Operation_Confirmation` notify row -- which the
    vendor stamps with an explicit `Z` -- reads `2026-09-15T22:08:57Z`.
    Same second, same event, one format carrying a zone and one not. If
    this integration's reading of the naive form ever stops matching the
    vendor's own zoned form, the parse is wrong.
    """
    assert parse_device_timestamp("2026-09-15 22:08:57") == parse_device_timestamp(
        "2026-09-15T22:08:57Z"
    )
