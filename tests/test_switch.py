"""Switch platform: three per-bowl settings and thirteen account-level ones.

The notification switches are the interesting half. `notify/saveConfig` is
a whole-object read-modify-write over an object carrying TWO parallel
arrays -- `notifyItems` and `notifyList` -- and the vendor has never
documented which one it reads back. Sending one, or sending only the row
that changed, is accepted by the server; what comes back afterwards is
whatever survived. So the assertions below are on the entire object, not
on the field that was toggled.
"""

from __future__ import annotations

import pytest
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.alwaysfull.const import ALERT_TYPE_OPTIONS
from custom_components.alwaysfull.exceptions import AlwaysFullError
from custom_components.alwaysfull.switch import (
    NOTIFY_SWITCHES,
    SWITCHES,
    AlwaysFullNotifySwitch,
)

from .conftest import (
    DEVICE_ID,
    ENTRY_UNIQUE_ID,
    FakeAlwaysFullClient,
    entity_id_for,
    load_fixture_data,
    only_write,
    setup_platform,
)


def bowl_switch(hass: HomeAssistant, key: str) -> str:
    """Return the first bowl's switch with this description key."""
    return entity_id_for(hass, SWITCH_DOMAIN, f"{DEVICE_ID}_{key}")


def account_switch(hass: HomeAssistant, key: str) -> str:
    """Return the account-level switch with this description key."""
    return entity_id_for(hass, SWITCH_DOMAIN, f"{ENTRY_UNIQUE_ID}_{key}")

# `device_config.json`, verbatim.
CLEAN_CYCLE = 3600
CLEAN_TIME = 25
SLEEP_START = 1320
SLEEP_END = 360


async def _turn(hass: HomeAssistant, entity_id: str, *, on: bool) -> None:
    """Call `switch.turn_on`/`turn_off` and let the write finish."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_all_switch_entities(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Entity ids, categories and current states are all pinned."""
    entry = await setup_platform(hass, Platform.SWITCH)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_flush_after_filling_writes_the_whole_flush_group(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Turning the switch on sends `fillWashState: 1` AND the rest of the group.

    A partial `{devNo, fillWashState}` would be accepted and would blank
    the flush interval and duration on the device.
    """
    await setup_platform(hass, Platform.SWITCH)

    await _turn(hass, bowl_switch(hass, "flush_after_filling"), on=True)

    assert only_write(mock_api, "set_flush_config") == {
        "device_id": DEVICE_ID,
        "cleanCycle": CLEAN_CYCLE,
        "cleanTime": CLEAN_TIME,
        "fillWashState": 1,
    }


async def test_flush_after_filling_off_writes_zero_not_false(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The vendor's flags are ints. `False` is not the same wire value as 0."""
    mock_api.device_config_override = load_fixture_data("device_config") | {"fillWashState": 1}
    await setup_platform(hass, Platform.SWITCH)
    assert hass.states.get(bowl_switch(hass, "flush_after_filling")).state == STATE_ON

    await _turn(hass, bowl_switch(hass, "flush_after_filling"), on=False)

    payload = only_write(mock_api, "set_flush_config")
    assert payload["fillWashState"] == 0
    assert payload["fillWashState"] is not False


async def test_sleep_mode_writes_the_whole_sleep_group(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Enabling sleep must carry the window with it, or the window is lost."""
    await setup_platform(hass, Platform.SWITCH)

    await _turn(hass, bowl_switch(hass, "sleep_mode"), on=True)

    assert only_write(mock_api, "set_sleep_config") == {
        "device_id": DEVICE_ID,
        "sleepStart": SLEEP_START,
        "sleepEnd": SLEEP_END,
        "sleepState": 1,
    }


async def test_drinking_log_recording_writes_log_state(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """`logConfig` is its own endpoint, with its own whole-object payload.

    The int check is not pedantry and the `==` above cannot make it:
    `True == 1` in Python, so a payload carrying a bool satisfies that
    assertion and then serialises as JSON `true`, which is not what the
    vendor stores -- and which changes the signed body as well.
    """
    await setup_platform(hass, Platform.SWITCH)

    await _turn(hass, bowl_switch(hass, "drinking_log"), on=True)

    payload = only_write(mock_api, "set_log_config")
    assert payload == {"device_id": DEVICE_ID, "logState": 1}
    assert payload["logState"] is not True


async def test_text_alerts_saves_the_entire_notification_object(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Toggling one flag must round-trip every other field untouched.

    Both arrays, all ten rows in each, and the account-level bookkeeping
    fields. `notify/saveConfig` takes what it is given, so anything missing
    here is a setting the user silently loses.
    """
    await setup_platform(hass, Platform.SWITCH)
    original = load_fixture_data("notify_config")

    await _turn(hass, account_switch(hass, "text_alerts"), on=False)

    saved = only_write(mock_api, "save_notify_config")
    assert saved == original | {"isTextNotify": 0}
    assert saved["isEmailNotify"] == 1
    assert len(saved["notifyItems"]) == 10
    assert len(saved["notifyList"]) == 10


async def test_email_alerts_saves_the_entire_notification_object(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The email flag is a separate field from the text flag."""
    await setup_platform(hass, Platform.SWITCH)
    original = load_fixture_data("notify_config")

    await _turn(hass, account_switch(hass, "email_alerts"), on=False)

    assert only_write(mock_api, "save_notify_config") == original | {"isEmailNotify": 0}


async def test_one_alert_type_flips_in_both_arrays_and_nothing_else_moves(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """`notifyItems` and `notifyList` must both survive a save, in step.

    Updating only the array the entity happened to read leaves the other
    stale, and the vendor reads back whichever one it likes -- so the
    switch would appear to work and then revert.
    """
    await setup_platform(hass, Platform.SWITCH)

    await _turn(hass, account_switch(hass, "alert_hardware_fault"), on=True)

    saved = only_write(mock_api, "save_notify_config")
    for key in ("notifyItems", "notifyList"):
        enabled = {row["type"]: row["enabled"] for row in saved[key]}
        assert enabled["Hardware_Fault"] is True, f"{key} did not record the change"
        assert sum(enabled.values()) == 1, f"{key}: something other than the target moved"
    # The descriptions ride along untouched; they are the vendor's own text.
    assert saved["notifyItems"] == saved["notifyList"]


async def test_each_alert_switch_targets_its_own_vendor_type(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Ten switches, ten distinct vendor types, no two pointing at one row.

    A mapping built by index rather than by name -- or one copied and
    pasted with a stale type -- would leave two switches driving the same
    alert and one alert unreachable.
    """
    await setup_platform(hass, Platform.SWITCH)

    flipped: dict[str, str] = {}
    for vendor_type, option in ALERT_TYPE_OPTIONS.items():
        mock_api.writes.clear()
        await _turn(hass, account_switch(hass, f"alert_{option}"), on=True)
        saved = only_write(mock_api, "save_notify_config")
        changed = [row["type"] for row in saved["notifyItems"] if row["enabled"]]
        assert changed == [vendor_type], f"alert_{option} flipped {changed}"
        flipped[option] = vendor_type
        # Undo, so each switch starts from the same all-off object.
        await _turn(hass, account_switch(hass, f"alert_{option}"), on=False)

    assert len(set(flipped.values())) == 10


async def test_an_alert_switch_reflects_the_saved_value(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """After a save, the switch reads the account's new state, not the old one."""
    await setup_platform(hass, Platform.SWITCH)
    assert hass.states.get(account_switch(hass, "alert_tilted")).state == STATE_OFF

    await _turn(hass, account_switch(hass, "alert_tilted"), on=True)

    assert hass.states.get(account_switch(hass, "alert_tilted")).state == STATE_ON


async def test_notification_switches_are_unavailable_until_the_config_is_known(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A switch with no config behind it must not claim to be off.

    `notify/getConfig` is polled once every ten cycles, so "not fetched
    yet" is a real state, and reporting it as off would invite a user to
    turn on an alert that was already on.
    """
    entry = await setup_platform(hass, Platform.SWITCH)
    coordinator = entry.runtime_data
    coordinator.notify_config = None
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert hass.states.get(account_switch(hass, "alert_tilted")).state == STATE_UNAVAILABLE
    # The per-bowl switches come from a different call and are unaffected.
    assert hass.states.get(bowl_switch(hass, "sleep_mode")).state == STATE_OFF

    # Home Assistant SILENTLY SKIPS an unavailable entity rather than
    # refusing the call, so the service layer cannot show what a write
    # attempted in this state. The entity is asked directly instead.
    switch = AlwaysFullNotifySwitch(coordinator, NOTIFY_SWITCHES[0])
    with pytest.raises(HomeAssistantError, match="not been read"):
        await switch.async_turn_on()
    assert mock_api.writes == []


async def test_a_refused_notification_save_raises(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A server refusal surfaces as a readable error, not a silent no-op."""
    await setup_platform(hass, Platform.SWITCH)
    mock_api.write_error = AlwaysFullError("Account suspended")

    with pytest.raises(HomeAssistantError, match="Account suspended"):
        await _turn(hass, account_switch(hass, "alert_tilted"), on=True)

    assert hass.states.get(account_switch(hass, "alert_tilted")).state == STATE_OFF


async def test_a_refused_bowl_write_raises(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The per-bowl switches map failures the same way."""
    await setup_platform(hass, Platform.SWITCH)
    mock_api.write_error = AlwaysFullError("Device offline")

    with pytest.raises(HomeAssistantError, match="Device offline"):
        await _turn(hass, bowl_switch(hass, "sleep_mode"), on=True)

    assert hass.states.get(bowl_switch(hass, "sleep_mode")).state == STATE_OFF


async def test_a_bowl_write_refreshes_that_bowls_config(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The scoped re-read is what stops the switch flicking back."""
    await setup_platform(hass, Platform.SWITCH)
    assert hass.states.get(bowl_switch(hass, "sleep_mode")).state == STATE_OFF

    mock_api.device_config_override = load_fixture_data("device_config") | {"sleepState": 1}
    await _turn(hass, bowl_switch(hass, "sleep_mode"), on=True)

    assert hass.states.get(bowl_switch(hass, "sleep_mode")).state == STATE_ON


def test_the_ten_alert_switches_come_from_the_shared_mapping() -> None:
    """No second copy of the alert table, in this platform or anywhere.

    Task 7 consolidated the vendor spelling -> option mapping into `const`
    precisely so the sensor, the event entity and now these switches cannot
    drift. A hand-written list here would pass every other test in this
    file while quietly disagreeing with the other two platforms.
    """
    alert_keys = [d.key for d in NOTIFY_SWITCHES if d.key.startswith("alert_")]
    assert alert_keys == [f"alert_{option}" for option in ALERT_TYPE_OPTIONS.values()]


def test_switch_keys_are_unique_across_both_tables() -> None:
    """A duplicate key would collide two switches onto one unique id."""
    keys = [d.key for d in (*SWITCHES, *NOTIFY_SWITCHES)]
    assert len(keys) == len(set(keys))
