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

from custom_components.alwaysfull.models import (
    BowlConfig,
    BowlState,
    bowl_size_inches_to_device_type,
    device_type_to_bowl_size_inches,
    filter_life_percent,
    minutes_to_time,
    time_to_minutes,
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


def test_bowl_state_from_real_device_detail_fixture():
    state = BowlState.from_api(fx("device_detail.json"))
    assert state.is_online is True
    assert state.bowl_size_inches == 9
    assert state.filter_used_time == 184467
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
