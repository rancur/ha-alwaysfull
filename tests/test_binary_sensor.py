"""Binary sensor platform.

Every alarm here is an int the vendor only ever compares `== 1`. The
parametrised cases below pin that literally: 1 alarms, 0 does not, and a
value nobody has ever seen does NOT alarm either, because inventing a
meaning for it would raise an alert on a field we do not understand.
"""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.alwaysfull.exceptions import AlwaysFullRateLimitError

from .conftest import FakeAlwaysFullClient, device_row, setup_platform

WALL = "binary_sensor.test_bowl_"
PUMP = "binary_sensor.second_bowl_"

# (wire field, entity id suffix) for every field the vendor tests `== 1`.
ALARM_FIELDS = [
    ("injectionAlarm", "water_fill_alarm"),
    ("pumpAlarm", "pump_alarm"),
    ("horizontalAlarm", "not_level"),
    ("systemSuspended", "system_problem"),
    ("hardwareFailure", "system_problem"),
]


async def test_all_binary_sensor_entities(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Entity ids, device classes and states are all pinned."""
    entry = await setup_platform(hass, Platform.BINARY_SENSOR)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


@pytest.mark.parametrize(("field", "suffix"), ALARM_FIELDS)
# The (value, expected) pair is passed as ONE argument so this test stays
# inside the argument-count limit the project's ruff configuration enforces,
# rather than switching that rule off for the whole suite.
@pytest.mark.parametrize("reading", [(1, STATE_ON), (0, STATE_OFF), (2, STATE_OFF)])
async def test_alarm_fields(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    field: str,
    suffix: str,
    reading: tuple[int, str],
) -> None:
    """Only `== 1` alarms; an unseen value must not be guessed at."""
    value, expected = reading
    mock_api.device_rows_override = [device_row(**{field: value})]
    await setup_platform(hass, Platform.BINARY_SENSOR)

    state = hass.states.get(f"{WALL}{suffix}")
    assert state is not None
    assert state.state == expected


@pytest.mark.parametrize(
    "row",
    [
        pytest.param({"systemSuspended": 1, "hardwareFailure": 0}, id="suspended_only"),
        pytest.param({"systemSuspended": 0, "hardwareFailure": 1}, id="hardware_only"),
    ],
)
async def test_system_problem_takes_either_field(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, row: dict[str, int]
) -> None:
    """EITHER field alone raises the problem; neither masks the other.

    Deliberately one field at a time. Setting both would make `or` and
    `and` produce the same ON, which is a test that cannot fail -- the
    exact failure mode this project has already shipped three times.
    """
    mock_api.device_rows_override = [device_row(**row)]
    await setup_platform(hass, Platform.BINARY_SENSOR)

    state = hass.states.get(f"{WALL}system_problem")
    assert state is not None
    assert state.state == STATE_ON


@pytest.mark.parametrize(("status", "expected"), [(1, STATE_ON), (0, STATE_OFF), (3, STATE_OFF)])
async def test_online(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, status: int, expected: str
) -> None:
    """`status == 1` is online; anything else, including unknown, is not."""
    mock_api.device_rows_override = [device_row(status=status)]
    await setup_platform(hass, Platform.BINARY_SENSOR)

    state = hass.states.get(f"{WALL}online")
    assert state is not None
    assert state.state == expected


@pytest.mark.parametrize(("filter_state", "expected"), [(0, STATE_ON), (1, STATE_OFF)])
async def test_filter_fault_on_a_wall_unit(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, filter_state: int, expected: str
) -> None:
    """`filterState == 0` is the fault; `1` is healthy."""
    mock_api.device_rows_override = [device_row(filterState=filter_state)]
    await setup_platform(hass, Platform.BINARY_SENSOR)

    state = hass.states.get(f"{WALL}filter_fault")
    assert state is not None
    assert state.state == expected


async def test_filter_fault_is_unknown_on_a_bottle_pump_bowl(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A bottle-pump bowl has no filter, so a fault flag means nothing.

    `filterState` is forced to 0 here precisely because the naive reading
    of it would raise a permanent, unclearable filter fault on a bowl that
    has no filter at all.
    """
    mock_api.device_rows_override = [device_row(slaveType=1, filterState=0)]
    await setup_platform(hass, Platform.BINARY_SENSOR)

    state = hass.states.get(f"{WALL}filter_fault")
    assert state is not None
    assert state.state == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("slave_type", "expected"), [(0, STATE_ON), (1, STATE_OFF), (2, STATE_OFF)]
)
async def test_water_source_detached(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, slave_type: int, expected: str
) -> None:
    """`slaveType == 0` means nothing is feeding the bowl."""
    mock_api.device_rows_override = [device_row(slaveType=slave_type)]
    await setup_platform(hass, Platform.BINARY_SENSOR)

    state = hass.states.get(f"{WALL}water_source_detached")
    assert state is not None
    assert state.state == expected


async def test_binary_sensors_go_unavailable_and_come_back(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A failed poll blanks them; the next good poll restores the real value."""
    entry = await setup_platform(hass, Platform.BINARY_SENSOR)
    coordinator = entry.runtime_data
    assert hass.states.get(f"{WALL}online").state == STATE_ON
    assert hass.states.get(f"{PUMP}online").state == STATE_ON

    mock_api.fail_device_list(AlwaysFullRateLimitError("429"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(f"{WALL}online").state == STATE_UNAVAILABLE
    assert hass.states.get(f"{WALL}pump_alarm").state == STATE_UNAVAILABLE

    mock_api.device_list_error = None
    mock_api.device_rows_override = [device_row(pumpAlarm=1)]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(f"{WALL}online").state == STATE_ON
    assert hass.states.get(f"{WALL}pump_alarm").state == STATE_ON
