"""Time platform: the sleep window.

`sleepStart`/`sleepEnd` are MINUTES SINCE LOCAL MIDNIGHT on the wire. A
`datetime.time` sent straight through, or an hours-and-minutes value that
never got multiplied, is accepted by the server and silently moves the
bowl's quiet hours somewhere else entirely.
"""

from __future__ import annotations

from datetime import time

import pytest
from homeassistant.components.time import (
    ATTR_TIME,
    SERVICE_SET_VALUE,
)
from homeassistant.components.time import (
    DOMAIN as TIME_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.alwaysfull.exceptions import AlwaysFullError

from .conftest import (
    DEVICE_ID,
    FakeAlwaysFullClient,
    load_fixture_data,
    only_write,
    setup_platform,
)

WALL = "time.test_bowl_"

# `device_config.json`, verbatim: 22:00 to 06:00.
SLEEP_START = 1320
SLEEP_END = 360
SLEEP_STATE = 0


async def _set(hass: HomeAssistant, entity_id: str, value: time) -> None:
    """Call `time.set_value` and let the write finish."""
    await hass.services.async_call(
        TIME_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_TIME: value},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_all_time_entities(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Entity ids and current values are pinned."""
    entry = await setup_platform(hass, Platform.TIME)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_the_window_is_read_as_a_time_of_day(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """1320 minutes is 22:00, and 360 is 06:00 -- not 13:20 and 03:60."""
    await setup_platform(hass, Platform.TIME)

    assert hass.states.get(f"{WALL}sleep_start").state == "22:00:00"
    assert hass.states.get(f"{WALL}sleep_end").state == "06:00:00"


async def test_sleep_start_writes_minutes_since_midnight(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """23:15 is 1395 minutes, and the rest of the group rides along.

    23:15 rather than a round hour on purpose: an implementation that sent
    only the hour, or that used `hour * 100 + minute`, agrees with the
    right answer at 00:00 and at very little else.
    """
    await setup_platform(hass, Platform.TIME)

    await _set(hass, f"{WALL}sleep_start", time(23, 15))

    assert only_write(mock_api, "set_sleep_config") == {
        "device_id": DEVICE_ID,
        "sleepStart": 1395,
        "sleepEnd": SLEEP_END,
        "sleepState": SLEEP_STATE,
    }


async def test_sleep_end_writes_minutes_since_midnight(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """06:30 is 390 minutes, and the start of the window does not move."""
    await setup_platform(hass, Platform.TIME)

    await _set(hass, f"{WALL}sleep_end", time(6, 30))

    assert only_write(mock_api, "set_sleep_config") == {
        "device_id": DEVICE_ID,
        "sleepStart": SLEEP_START,
        "sleepEnd": 390,
        "sleepState": SLEEP_STATE,
    }


async def test_midnight_writes_zero_not_a_dropped_field(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """00:00 is a real value, and 0 must survive the whole write path.

    The transport drops `None` from a body before signing it; a conversion
    that returned `None` for midnight would therefore delete the field
    rather than set it, leaving the old window in place.
    """
    await setup_platform(hass, Platform.TIME)

    await _set(hass, f"{WALL}sleep_start", time(0, 0))

    payload = only_write(mock_api, "set_sleep_config")
    assert payload["sleepStart"] == 0
    assert payload["sleepStart"] is not None


async def test_a_time_write_shows_the_new_window_immediately(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The UI shows the new window at once, not after the next poll.

    Every config read still serves the fixture's `sleepStart: 1320`
    (22:00), the way the eventually-consistent vendor serves the pre-write
    object for around twenty seconds. So 23:15 can only have come from the
    write.
    """
    await setup_platform(hass, Platform.TIME)
    assert load_fixture_data("device_config")["sleepStart"] == 1320

    await _set(hass, f"{WALL}sleep_start", time(23, 15))

    assert hass.states.get(f"{WALL}sleep_start").state == "23:15:00"


async def test_a_refused_time_write_raises(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A server refusal is an error the user sees."""
    await setup_platform(hass, Platform.TIME)
    mock_api.write_error = AlwaysFullError("Device offline")

    with pytest.raises(HomeAssistantError, match="Device offline"):
        await _set(hass, f"{WALL}sleep_start", time(23, 15))

    assert hass.states.get(f"{WALL}sleep_start").state == "22:00:00"
