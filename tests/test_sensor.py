"""Sensor platform.

The snapshot covers entity ids, registry metadata and states in bulk; the
explicit tests below cover the things a snapshot cannot prove on its own --
that a missing day is reported as unknown rather than fabricated as zero,
that the filter sensors go quiet on a bottle-pump bowl, that each enum maps
the way the vendor means it, and that an outage recovers.
"""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.alwaysfull.const import ATTR_RAW_TYPE, UNKNOWN
from custom_components.alwaysfull.exceptions import AlwaysFullRateLimitError
from custom_components.alwaysfull.sensor import ATTR_RAW_SLAVE_TYPE, SENSORS

from .conftest import (
    DEVICE_ID,
    FakeAlwaysFullClient,
    bowl_data,
    device_row,
    load_fixture_data,
    setup_platform,
)

# The wall-unit bowl (`slaveType == 2`, millilitres) and the bottle-pump
# bowl (`slaveType == 1`, fluid ounces) from `device_list_multi.json`.
WALL = "sensor.test_bowl_"
PUMP = "sensor.second_bowl_"

# `device_config.json`: filterCanUseTime 10512000 s, and the wall bowl's
# row carries filterUsedTime 184458 s.
FILTER_CAN_USE = 10512000
FILTER_USED = 184458
FILTER_REMAINING_DAYS = (FILTER_CAN_USE - FILTER_USED) / 86400


async def test_all_sensor_entities(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    enable_all_entities: None,
) -> None:
    """Entity ids, units, device classes and states are all pinned."""
    entry = await setup_platform(hass, Platform.SENSOR)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_water_today_native_unit_follows_the_device_units_field(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """`units` 1 is millilitres and 2 is fluid ounces, per bowl, not per account.

    Home Assistant then converts a convertible device class into the
    user's own unit system, so under the default metric system BOTH bowls
    display millilitres. That conversion is what makes this assertion
    sharp rather than vague: the second bowl reports 250 fluid ounces, so
    a correct native unit shows ~7393 mL, while an implementation that
    assumed millilitres for every bowl would show 250.
    """
    await setup_platform(hass, Platform.SENSOR)

    wall = hass.states.get(f"{WALL}water_today")
    pump = hass.states.get(f"{PUMP}water_today")
    assert wall is not None
    assert pump is not None
    assert wall.state == "903"
    assert wall.attributes["unit_of_measurement"] == "mL"
    assert float(pump.state) == pytest.approx(250 * 29.5735295625, abs=0.01)
    assert pump.attributes["unit_of_measurement"] == "mL"


async def test_water_today_is_shown_in_fluid_ounces_under_us_customary(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The same two bowls under a US-customary Home Assistant.

    Mirror image of the test above, and the pair is the point: whichever
    unit system is in force, the bowl reporting fluid ounces and the bowl
    reporting millilitres end up at DIFFERENT numbers, which can only
    happen if each one's native unit came from its own `units` field.
    """
    hass.config.units = US_CUSTOMARY_SYSTEM
    await setup_platform(hass, Platform.SENSOR)

    wall = hass.states.get(f"{WALL}water_today")
    pump = hass.states.get(f"{PUMP}water_today")
    assert wall is not None
    assert pump is not None
    assert float(wall.state) == pytest.approx(903 / 29.5735295625, abs=0.01)
    assert wall.attributes["unit_of_measurement"] == "fl. oz."
    assert pump.state == "250"
    assert pump.attributes["unit_of_measurement"] == "fl. oz."


async def test_water_today_unit_follows_a_units_change_between_polls(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Flipping `units` on the bowl moves the sensor's NATIVE unit.

    `units` is writable (Task 8 exposes it), so a user switching the bowl
    between millilitres and fluid ounces mid-session is a real scenario,
    not a hypothetical. Without the re-sync on the update path the entity
    would keep the unit it was constructed with and silently report fluid
    ounces as though they were millilitres.

    The harm is mislabelling, not arithmetic: the vendor keeps sending the
    same daily total, and if the unit does not follow, 903 fluid ounces is
    presented to the user as 903 millilitres -- a thirty-fold error in what
    they read, with no error anywhere to notice.

    Verified against the installed Home Assistant rather than assumed: this
    entity stored no display-unit option (its native unit already matched
    the metric system when it was created), so its displayed unit follows
    its native unit. Delete the re-sync on the update path and this stays
    `mL`.
    """
    entry = await setup_platform(hass, Platform.SENSOR)
    before = hass.states.get(f"{WALL}water_today")
    assert before.state == "903"
    assert before.attributes["unit_of_measurement"] == "mL"

    mock_api.device_rows_override = [device_row(units=2)]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(f"{WALL}water_today")
    assert state is not None
    assert state.attributes["unit_of_measurement"] == "fl. oz."
    assert state.state == "903"


async def test_water_today_is_unknown_when_today_has_no_row_yet(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A day the server has no data for is unknown, never a fabricated 0.

    A zero here reads as "the pet drank nothing today", which is exactly
    the sort of thing someone sets a low-water alert on.
    """
    mock_api.drinking_rows_override = [{"drinkingDate": "1999-12-31", "totalCapacity": 4242}]
    await setup_platform(hass, Platform.SENSOR)

    state = hass.states.get(f"{WALL}water_today")
    assert state is not None
    assert state.state == STATE_UNKNOWN


async def test_filter_sensors_report_values_on_a_wall_unit(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """On a wall unit the filter numbers come from config minus used time."""
    await setup_platform(hass, Platform.SENSOR)

    life = hass.states.get(f"{WALL}filter_life")
    remaining = hass.states.get(f"{WALL}filter_time_remaining")
    assert life is not None
    assert remaining is not None
    assert float(life.state) == pytest.approx(
        (FILTER_CAN_USE - FILTER_USED) / FILTER_CAN_USE * 100, abs=0.01
    )
    # Reported in days because `suggested_unit_of_measurement` is days; the
    # native value is the vendor's seconds.
    assert float(remaining.state) == pytest.approx(FILTER_REMAINING_DAYS, abs=0.01)
    assert remaining.attributes["unit_of_measurement"] == "d"


async def test_filter_sensors_are_unknown_when_filter_tracking_is_disabled(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """`filterCanUseTime == 0` is the live-observed "feature disabled" value.

    Dividing by it raises `ZeroDivisionError` and takes down the whole
    coordinator update, and subtracting from it reports a large NEGATIVE
    time remaining, which reads as a filter that is catastrophically
    overdue on a bowl that is not tracking one at all.
    """
    mock_api.device_config_override = load_fixture_data("device_config") | {
        "filterCanUseTime": 0
    }
    await setup_platform(hass, Platform.SENSOR)

    for key in ("filter_life", "filter_time_remaining"):
        state = hass.states.get(f"{WALL}{key}")
        assert state is not None
        assert state.state == STATE_UNKNOWN


async def test_filter_sensors_are_unknown_on_a_bottle_pump_bowl(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Filter state is meaningless unless `slaveType == 2` (wall unit).

    The vendor's own app hides these tiles entirely on a bottle-pump bowl.
    The entities stay registered -- dropping them on a runtime value churns
    the entity registry and breaks history -- but report nothing.
    """
    await setup_platform(hass, Platform.SENSOR)

    for key in ("filter_life", "filter_time_remaining"):
        state = hass.states.get(f"{PUMP}{key}")
        assert state is not None, f"{key} must stay registered on a bottle-pump bowl"
        assert state.state == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("slave_type", "expected"),
    [(0, "none"), (1, "bottle_pump"), (2, "wall_unit"), (7, "unknown")],
)
async def test_water_source_enum(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    slave_type: int,
    expected: str,
) -> None:
    """`slaveType` decodes to the vendor's three sources, else unknown."""
    mock_api.device_rows_override = [device_row(slaveType=slave_type)]
    await setup_platform(hass, Platform.SENSOR)

    state = hass.states.get(f"{WALL}water_source")
    assert state is not None
    assert state.state == expected
    # The FULL list, not just membership: an accidental extra or renamed
    # option passes a membership check while breaking every automation and
    # every translated label that depends on it.
    assert state.attributes["options"] == ["none", "bottle_pump", "wall_unit", "unknown"]
    # The vendor's own value, so an unrecognised source is distinguishable
    # from a bowl that is simply not reporting.
    assert state.attributes[ATTR_RAW_SLAVE_TYPE] == slave_type


@pytest.mark.parametrize(
    ("key", "overrides", "expected"),
    [
        ("water_source", {"slaveType": 7}, "unknown"),
        ("water_source", {"slaveType": 0}, "none"),
    ],
)
def test_enum_value_fn_returns_an_option_not_none(
    key: str, overrides: dict[str, int], expected: str
) -> None:
    """The `unknown` OPTION is a value, not the absence of one.

    Home Assistant renders the string `"unknown"` exactly like a missing
    value, so the state-machine assertions above cannot tell an
    unrecognised `slaveType` from a sensor that gave up and returned
    `None`. Calling the `value_fn` can.
    """
    description = next(d for d in SENSORS if d.key == key)
    assert description.value_fn(bowl_data(**overrides)) == expected


def test_last_alert_value_fn_distinguishes_unknown_from_no_alerts() -> None:
    """An unrecognised type is the `unknown` option; no rows at all is `None`."""
    description = next(d for d in SENSORS if d.key == "last_alert")

    bowl = bowl_data()
    bowl.notifications = [{"type": "Some_New_Vendor_Alert", "createTime": "2026-09-16T10:00:00Z"}]
    assert description.value_fn(bowl) == UNKNOWN

    bowl.notifications = []
    assert description.value_fn(bowl) is None


async def test_last_alert_is_the_newest_row_not_the_first(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The newest row wins even when the server sends them oldest-first.

    Trusting the server's ordering is what makes this silently wrong: the
    committed capture happens to arrive newest-first, so an implementation
    that just reads `rows[0]` passes against it forever.
    """
    mock_api.notify_rows_override = [
        {"id": 1, "type": "Tilted", "createTime": "2026-09-14T10:00:00Z"},
        {"id": 2, "type": "Fill_Failed", "createTime": "2026-09-16T10:00:00Z"},
        {"id": 3, "type": "Pump_Alarm_Not_A_Real_Type", "createTime": "2026-09-15T10:00:00Z"},
    ]
    await setup_platform(hass, Platform.SENSOR)

    state = hass.states.get(f"{WALL}last_alert")
    assert state is not None
    assert state.state == "fill_failed"
    assert state.attributes[ATTR_RAW_TYPE] == "Fill_Failed"


@pytest.mark.parametrize(
    ("vendor_type", "expected"),
    [
        ("Tilted", "tilted"),
        ("Daily_Maximum", "daily_maximum"),
        ("Fill_Failed", "fill_failed"),
        ("Not_Attached", "not_attached"),
        ("High_Water_Level", "high_water_level"),
        ("Replace_Wall_Filter", "replace_wall_filter"),
        ("Replace_Bowl_Filter", "replace_bowl_filter"),
        ("Daily_Decreased", "daily_decreased"),
        ("Operation_Confirmation", "operation_confirmation"),
        ("Hardware_Fault", "hardware_fault"),
        # Not one of the ten: normalised away, but still recoverable.
        ("Some_New_Vendor_Alert", "unknown"),
    ],
)
async def test_last_alert_normalises_and_keeps_the_raw_vendor_string(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    vendor_type: str,
    expected: str,
) -> None:
    """The state is snake_case; the vendor's own spelling survives verbatim.

    Both halves matter. The state is what automations and the recorder see,
    and it follows Home Assistant's enum convention (and Task 7's event
    types). The `raw_type` attribute is the escape hatch: normalising is
    lossy, and for an alert type the vendor adds AFTER this table was
    written, that attribute is the only place the real name appears.
    """
    mock_api.notify_rows_override = [
        {"id": 1, "type": vendor_type, "createTime": "2026-09-16T10:00:00Z"}
    ]
    await setup_platform(hass, Platform.SENSOR)

    state = hass.states.get(f"{WALL}last_alert")
    assert state is not None
    assert state.state == expected
    assert state.attributes["options"] == [
        "tilted",
        "daily_maximum",
        "fill_failed",
        "not_attached",
        "high_water_level",
        "replace_wall_filter",
        "replace_bowl_filter",
        "daily_decreased",
        "operation_confirmation",
        "hardware_fault",
        "unknown",
    ]
    assert state.attributes[ATTR_RAW_TYPE] == vendor_type


# The `ALERT_TYPES` drift guard that used to live here moved to
# `test_descriptions.py::test_the_alert_mapping_is_the_only_one` when Task 7
# made the mapping shared: it now has to hold for the event platform's
# `event_types` as well, and one mapping deserves one guard.


async def test_last_alert_is_unknown_with_no_rows(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A bowl that has never alerted reports nothing, not a stale alert."""
    mock_api.notify_rows_override = []
    await setup_platform(hass, Platform.SENSOR)

    state = hass.states.get(f"{WALL}last_alert")
    assert state is not None
    assert state.state == STATE_UNKNOWN


async def test_firmware_is_diagnostic_and_disabled_by_default(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, entity_registry: er.EntityRegistry
) -> None:
    """Firmware duplicates the device page, so it is off unless asked for."""
    await setup_platform(hass, Platform.SENSOR)

    entry = entity_registry.async_get(f"{WALL}firmware_version")
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(f"{WALL}firmware_version") is None


async def test_firmware_reports_the_device_version_when_enabled(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, enable_all_entities: None
) -> None:
    """Enabled, it reports the version off the device row."""
    await setup_platform(hass, Platform.SENSOR)

    state = hass.states.get(f"{WALL}firmware_version")
    assert state is not None
    assert state.state == "3.6.2"


async def test_sensors_go_unavailable_and_come_back(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A failed poll blanks the sensors; the next good poll restores them."""
    entry = await setup_platform(hass, Platform.SENSOR)
    coordinator = entry.runtime_data
    assert hass.states.get(f"{WALL}water_today").state == "903"

    mock_api.fail_device_list(AlwaysFullRateLimitError("429"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(f"{WALL}water_today").state == STATE_UNAVAILABLE
    assert hass.states.get(f"{WALL}water_source").state == STATE_UNAVAILABLE

    mock_api.device_list_error = None
    mock_api.drinking_totals[DEVICE_ID] = 1000
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(f"{WALL}water_today").state == "1000"
