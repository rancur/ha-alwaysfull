"""Number platform: the seven writable settings and their unit conversions.

Every assertion here is on the EXACT payload the client method received --
wire field names and converted values both. That is deliberate. A test
asserting only that `set_flush_config` was called passes just as happily
when the platform sends 30 minutes into a field the vendor reads as 30
seconds, which is the failure this platform exists to avoid and which the
device reports no error for.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.number import (
    ATTR_VALUE,
    SERVICE_SET_VALUE,
)
from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.alwaysfull.const import DOMAIN
from custom_components.alwaysfull.exceptions import AlwaysFullError

from .conftest import (
    DEVICE_ID,
    SECOND_DEVICE_ID,
    FakeAlwaysFullClient,
    device_row,
    load_fixture_data,
    only_write,
    setup_platform,
)

WALL = "number.test_bowl_"
PUMP = "number.second_bowl_"

# `device_config.json`, verbatim.
CLEAN_CYCLE = 3600
CLEAN_TIME = 25
FILL_WASH_STATE = 0
FILTER_CAN_USE = 10512000
CAPACITY = 378541
DAY_MIN = 2000
DAY_MAX = 7500

SECONDS_PER_MONTH = 2592000
SECONDS_PER_DAY = 86400


def entry_coordinator(hass: HomeAssistant) -> Any:
    """Return the one loaded entry's coordinator."""
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    return entry.runtime_data


async def _set(hass: HomeAssistant, entity_id: str, value: float) -> None:
    """Call `number.set_value` and let the write finish."""
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_all_number_entities(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Entity ids, ranges, units and current values are all pinned."""
    entry = await setup_platform(hass, Platform.NUMBER)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_flush_interval_is_read_in_minutes_from_seconds(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """3600 seconds on the wire is 60 minutes in the UI, not 3600."""
    await setup_platform(hass, Platform.NUMBER)

    state = hass.states.get(f"{WALL}flush_interval")
    assert state is not None
    assert float(state.state) == 60
    assert state.attributes["unit_of_measurement"] == "min"


async def test_flush_interval_writes_minutes_as_seconds(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """30 minutes must reach the vendor as `cleanCycle: 1800`.

    Sending 30 would be accepted by the server and silently flush the bowl
    every thirty SECONDS. The rest of the group is asserted too: the
    endpoint is a whole-object write, so a payload carrying only the field
    that changed would blank `cleanTime` and `fillWashState`.
    """
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}flush_interval", 30)

    assert only_write(mock_api, "set_flush_config") == {
        "device_id": DEVICE_ID,
        "cleanCycle": 1800,
        "cleanTime": CLEAN_TIME,
        "fillWashState": FILL_WASH_STATE,
    }


async def test_flush_duration_writes_seconds_with_no_conversion(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """`cleanTime` is already seconds on the wire, so 40 stays 40.

    Paired with the test above on purpose: one field of this endpoint is
    converted and the other is not, and a conversion applied to both (or
    to neither) breaks exactly one of the two.
    """
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}flush_duration", 40)

    assert only_write(mock_api, "set_flush_config") == {
        "device_id": DEVICE_ID,
        "cleanCycle": CLEAN_CYCLE,
        "cleanTime": 40,
        "fillWashState": FILL_WASH_STATE,
    }


async def test_filter_lifetime_writes_months_as_thirty_day_seconds(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Six months is 15552000 seconds, and capacity rides along unchanged."""
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}filter_lifetime", 6)

    assert only_write(mock_api, "set_filter_config") == {
        "device_id": DEVICE_ID,
        "filterCanUseTime": 6 * SECONDS_PER_MONTH,
        "filterCapacity": CAPACITY,
    }


async def test_filter_capacity_is_written_as_filterCapacity_never_capacity(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The field is READ as `capacity` and must be WRITTEN as `filterCapacity`.

    Echoing back the name it was read under is accepted by the server and
    changes nothing on the device: the capacity silently never moves.
    """
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}filter_capacity", 500000)

    payload = only_write(mock_api, "set_filter_config")
    assert payload["filterCapacity"] == 500000
    assert "capacity" not in payload
    assert payload == {
        "device_id": DEVICE_ID,
        "filterCanUseTime": FILTER_CAN_USE,
        "filterCapacity": 500000,
    }


async def test_maintenance_interval_writes_days_as_seconds(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Ten days is 864000 seconds.

    Ten, not thirty: thirty days and one 30-day "month" are the same number
    of seconds, so a days/months mix-up would pass unnoticed at 30.
    """
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}maintenance_interval", 10)

    payload = only_write(mock_api, "set_maintenance_config")
    assert payload["deviceCanUseTime"] == 10 * SECONDS_PER_DAY
    assert payload["deviceCanUseTime"] != 10 * SECONDS_PER_MONTH


async def test_clean_warn_time_is_echoed_as_raw_seconds(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """`cleanWarnTime` has NO unit conversion anywhere in this integration.

    A neighbouring setting in the same group must hand the device's own
    value straight back. A 30-day-month conversion applied on the way out
    would turn one week into 0 -- a factor-of-2,592,000 error that the
    fixture's own `cleanWarnTime: 0` cannot show, which is why this test
    overrides it with a non-zero value.
    """
    mock_api.device_config_override = load_fixture_data("device_config") | {
        "cleanWarnTime": 604800
    }
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}maintenance_interval", 10)

    assert only_write(mock_api, "set_maintenance_config") == {
        "device_id": DEVICE_ID,
        "deviceCanUseTime": 10 * SECONDS_PER_DAY,
        "cleanWarnTime": 604800,
    }


async def test_the_maintenance_warning_reads_raw_seconds(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The entity shows the vendor's seconds, unconverted.

    604800 is one week. A entity that divided by a 30-day "month" on the
    way in would show 0, and one that divided by a day would show 7 -- both
    of which look like a plausible setting and are neither of them the
    number the device holds.
    """
    mock_api.device_config_override = load_fixture_data("device_config") | {
        "cleanWarnTime": 604800
    }
    await setup_platform(hass, Platform.NUMBER)

    state = hass.states.get(f"{WALL}maintenance_warning_lead_time")
    assert state is not None
    assert float(state.state) == 604800
    assert state.attributes["unit_of_measurement"] == "s"


async def test_the_maintenance_warning_writes_raw_seconds_to_its_own_group(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The value the user types is the value the wire carries, in SECONDS.

    Two separate ways to get this wrong, and both are asserted. Converting
    on the way out is the factor-of-2,592,000 mistake `models.py` warns
    about at length: a user asking for one week would be sending 604800
    thirty-day months, or 0, depending on the direction of the error.
    Sending it in the FILTER group is the other: `cleanWarnTime` is a
    `maintenanceConfig` field, and the filter group's body is exactly
    `{devNo, filterCanUseTime, filterCapacity}` -- a write that landed
    there would blank the filter settings and never touch the warning.
    """
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}maintenance_warning_lead_time", 604800)

    assert only_write(mock_api, "set_maintenance_config") == {
        "device_id": DEVICE_ID,
        # Unchanged from the fixture: the OTHER field of the group has to
        # ride along untouched, because these endpoints replace the whole
        # object they are sent.
        "deviceCanUseTime": 0,
        "cleanWarnTime": 604800,
    }
    assert [name for name, _payload in mock_api.writes] == ["set_maintenance_config"]


async def test_daily_minimum_water_echoes_the_bowls_own_units(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """`waterConfig` must carry `units` from the device row, per bowl.

    Without it the server misreads the thresholds. Asserting it on BOTH
    bowls is what makes this sharp: the wall bowl is millilitres (1) and
    the bottle-pump bowl is fluid ounces (2), so a hard-coded `units: 1`
    passes the first assertion and fails the second.
    """
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}daily_minimum_water", 1000)
    assert only_write(mock_api, "set_water_config") == {
        "device_id": DEVICE_ID,
        "dayMinWater": 1000,
        "dayMaxWater": DAY_MAX,
        "units": 1,
    }

    mock_api.writes.clear()
    await _set(hass, f"{PUMP}daily_minimum_water", 1000)
    assert only_write(mock_api, "set_water_config") == {
        "device_id": SECOND_DEVICE_ID,
        "dayMinWater": 1000,
        "dayMaxWater": DAY_MAX,
        "units": 2,
    }


async def test_daily_maximum_water_writes_the_whole_group(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Raising the maximum leaves the minimum exactly where it was."""
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}daily_maximum_water", 9000)

    assert only_write(mock_api, "set_water_config") == {
        "device_id": DEVICE_ID,
        "dayMinWater": DAY_MIN,
        "dayMaxWater": 9000,
        "units": 1,
    }


async def test_a_minimum_above_the_maximum_is_refused_before_any_write(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The user gets a readable message, and nothing reaches the vendor.

    The rule lives in `to_water_payload`; what this pins is that the
    platform surfaces it as a `HomeAssistantError` the user can read rather
    than letting a bare `ValueError` become an "Unknown error" toast with a
    traceback in the log.
    """
    await setup_platform(hass, Platform.NUMBER)

    with pytest.raises(HomeAssistantError, match="must be <"):
        await _set(hass, f"{WALL}daily_minimum_water", 9000)

    assert mock_api.writes == []


async def test_both_thresholds_at_zero_is_allowed(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Zero/zero is the vendor's disabled sentinel, not a violation.

    A guard written as a plain `min < max` rejects it, which would leave
    the user with no way to turn the thresholds off at all.
    """
    mock_api.device_config_override = load_fixture_data("device_config") | {"dayMinWater": 0}
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}daily_maximum_water", 0)

    assert only_write(mock_api, "set_water_config") == {
        "device_id": DEVICE_ID,
        "dayMinWater": 0,
        "dayMaxWater": 0,
        "units": 1,
    }


async def test_a_write_shows_the_new_value_although_the_vendor_still_lags(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The UI must not bounce back to the pre-write value.

    The vendor is eventually consistent, so every config read here goes on
    serving `cleanCycle: 3600` -- sixty minutes, the pre-write value --
    exactly as the live server does for around twenty seconds after a
    write. The new value can therefore only have come from the write
    itself, which is the whole point.
    """
    await setup_platform(hass, Platform.NUMBER)
    assert float(hass.states.get(f"{WALL}flush_interval").state) == 60
    assert load_fixture_data("device_config")["cleanCycle"] == 3600

    await _set(hass, f"{WALL}flush_interval", 30)

    assert float(hass.states.get(f"{WALL}flush_interval").state) == 30


async def test_a_refused_write_raises_and_leaves_the_cache_alone(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A server refusal is an error the user sees, never a silent no-op.

    The cached value must also stay as it was: showing the value the user
    asked for after the device refused it is the worst of both worlds.
    """
    entry = await setup_platform(hass, Platform.NUMBER)
    mock_api.write_error = AlwaysFullError("Device offline")

    with pytest.raises(HomeAssistantError, match="Device offline"):
        await _set(hass, f"{WALL}flush_interval", 30)

    assert float(hass.states.get(f"{WALL}flush_interval").state) == 60
    # And the CACHE behind it, which the state machine does not re-read
    # after a failed service call: a write that edited the coordinator's
    # own config object rather than a copy leaves 30 sitting there, to
    # surface as the truth on the next update that touches this entity.
    assert entry.runtime_data.data[DEVICE_ID].config.flush_interval_minutes == 60


# -- Lossy conversions: a no-op set must be a genuine no-op ----------------
#
# The vendor's own app gets this wrong. It reads with
# `Math.floor(filterCanUseTime / 2592e3)` and writes `30 * months * 86400`,
# while the live bowl was provisioned with 10512000 == 4 * 2628000 (a
# 365/12-day month). Open its filter screen, press save, change nothing,
# and the lifetime drops by 1.67 days. It does that every time.


async def test_setting_the_filter_lifetime_it_already_shows_sends_the_raw_seconds(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Re-selecting 4 months must write 10512000 back, not 10368000.

    10512000 floors to 4 under the vendor's 30-day month but is not a
    whole number of them, so converting 4 back would silently shorten the
    filter's life by 1.67 days -- and again on the next save, for ever.
    """
    await setup_platform(hass, Platform.NUMBER)
    assert float(hass.states.get(f"{WALL}filter_lifetime").state) == 4

    await _set(hass, f"{WALL}filter_lifetime", 4)

    payload = only_write(mock_api, "set_filter_config")
    assert payload["filterCanUseTime"] == FILTER_CAN_USE
    assert payload["filterCanUseTime"] != 4 * SECONDS_PER_MONTH


async def test_a_real_filter_lifetime_change_still_converts(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The guard is for no-ops only; 5 months is still 5 vendor months.

    Paired with the test above deliberately: an implementation that never
    converted would pass that one and fail this, and the vendor's own
    arithmetic is the right arithmetic when the user really is changing
    something.
    """
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}filter_lifetime", 5)

    assert only_write(mock_api, "set_filter_config")["filterCanUseTime"] == (
        5 * SECONDS_PER_MONTH
    )


async def test_the_same_guard_covers_the_other_two_floored_settings(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Flush interval and maintenance interval floor to a coarser unit too.

    The captured bowl divides evenly on both, so this uses a config that
    does not: 3661 s reads as 61 minutes and 90061 s as 1 day, and writing
    the displayed value back would quietly discard the remainder.
    """
    mock_api.device_config_override = load_fixture_data("device_config") | {
        "cleanCycle": 3661,
        "deviceCanUseTime": 90061,
    }
    await setup_platform(hass, Platform.NUMBER)
    assert float(hass.states.get(f"{WALL}flush_interval").state) == 61
    assert float(hass.states.get(f"{WALL}maintenance_interval").state) == 1

    await _set(hass, f"{WALL}flush_interval", 61)
    assert only_write(mock_api, "set_flush_config")["cleanCycle"] == 3661

    mock_api.writes.clear()
    await _set(hass, f"{WALL}maintenance_interval", 1)
    assert only_write(mock_api, "set_maintenance_config")["deviceCanUseTime"] == 90061


async def test_a_real_change_to_those_two_still_converts(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Same pairing as the filter: the guard must not swallow real edits."""
    mock_api.device_config_override = load_fixture_data("device_config") | {
        "cleanCycle": 3661,
        "deviceCanUseTime": 90061,
    }
    await setup_platform(hass, Platform.NUMBER)

    await _set(hass, f"{WALL}flush_interval", 45)
    assert only_write(mock_api, "set_flush_config")["cleanCycle"] == 45 * 60

    mock_api.writes.clear()
    await _set(hass, f"{WALL}maintenance_interval", 7)
    assert only_write(mock_api, "set_maintenance_config")["deviceCanUseTime"] == (
        7 * SECONDS_PER_DAY
    )


async def test_the_daily_threshold_unit_follows_a_units_change_on_the_bowl(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Switching the bowl to fluid ounces relabels the threshold boxes.

    The same defect Task 6 fixed on the sensor platform, and the harm here
    is worse than a wrong label: these boxes are WRITABLE, and the value
    goes to the vendor in whatever unit the bowl is on. An owner who flips
    the units select and then types 60 into a box still saying `mL` has
    just set a 60 fluid-ounce threshold while reading `mL` off the screen.

    Delete `_handle_coordinator_update` and this stays `mL`: the unit is
    read once at construction and never again.
    """
    await setup_platform(hass, Platform.NUMBER)
    before = hass.states.get(f"{WALL}daily_minimum_water")
    assert before.attributes["unit_of_measurement"] == "mL"

    mock_api.device_rows_override = [device_row(units=2)]
    await entry_coordinator(hass).async_refresh()
    await hass.async_block_till_done()

    after = hass.states.get(f"{WALL}daily_minimum_water")
    assert after is not None
    assert after.attributes["unit_of_measurement"] == "fl. oz."
    # The stored number is the bowl's own, unconverted: the vendor keeps
    # these thresholds in whatever unit the bowl is set to.
    assert float(after.state) == DAY_MIN


async def test_a_dropped_bowl_keeps_its_last_known_unit(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A poll that no longer lists this bowl must not blank the unit.

    Blanking it would make Home Assistant treat the recorder history as a
    unit change. The re-sync only fires for a poll that actually found the
    bowl, so the entity goes unavailable with its unit intact.
    """
    await setup_platform(hass, Platform.NUMBER)
    assert hass.states.get(f"{WALL}daily_minimum_water").attributes[
        "unit_of_measurement"
    ] == "mL"

    mock_api.device_rows_override = [device_row(SECOND_DEVICE_ID)]
    await entry_coordinator(hass).async_refresh()
    await hass.async_block_till_done()

    gone = hass.states.get(f"{WALL}daily_minimum_water")
    assert gone.state == STATE_UNAVAILABLE
    assert gone.attributes["unit_of_measurement"] == "mL"
