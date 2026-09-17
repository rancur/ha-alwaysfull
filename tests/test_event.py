"""Alert event platform.

This is the entity the bowl's owner actually cares about: the vendor gates
push notifications behind a subscription, and this turns the same alerts
into free Home Assistant automation triggers.

Three behaviours carry the whole feature, and all three fail SILENTLY if
they regress -- no exception, no log line, just a user who is either paged
every minute forever or never paged at all:

- dedupe, or every poll re-fires every alert still on the page;
- no replay on startup, or every Home Assistant restart re-triggers up to
  twenty historical alerts at once;
- chronological firing, or the entity settles on the wrong "last" alert.

Every test below therefore asserts against the STATE MACHINE rather than
against the entity object. `EventEntity._trigger_event()` does not write
state (verified in the installed Home Assistant source), so a test that
only inspected the entity would pass with the state write deleted and the
shipped entity would never update.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.event import ATTR_EVENT_TYPE, ATTR_EVENT_TYPES, EventDeviceClass
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.alwaysfull import event as event_module
from custom_components.alwaysfull.const import (
    ALERT_OPTIONS,
    ALERT_TYPE_OPTIONS,
    ATTR_RAW_TYPE,
    UNKNOWN,
)
from custom_components.alwaysfull.event import (
    ALERT_EVENTS,
    ATTR_ALERT_ID,
    ATTR_CREATED,
    ATTR_MESSAGE,
)
from custom_components.alwaysfull.exceptions import AlwaysFullRateLimitError

from .conftest import (
    DEVICE_ID,
    SECOND_DEVICE_ID,
    FakeAlwaysFullClient,
    load_fixture_data,
    setup_platform,
)

ALERT = "event.test_bowl_alert"
SECOND_ALERT = "event.second_bowl_alert"

# The committed capture: thirteen rows, newest first.
#
# Observed reality, from the unsanitised capture of the real bowl: the server
# returns rows NEWEST FIRST and assigns ASCENDING ids over time, so the
# highest id is at index 0 and the ids descend down the array. Observed once,
# from one account, over thirteen rows -- see
# `test_a_newer_alert_with_a_lower_id_still_fires`. The synthetic ids in the
# fixture preserve that relationship (5012 newest .. 5000 oldest); an earlier
# sanitisation pass inverted it, which is a fact about the fixture worth
# keeping here so nobody re-derives vendor behaviour from a renumbering.
#
# New alerts invented by the tests below therefore take ids ABOVE 5012, which
# is what the real server would assign, EXCEPT where a test is deliberately
# probing the opposite.
HISTORY: list[dict[str, Any]] = load_fixture_data("notify_log")["data"]


def alert_row(
    row_id: Any,
    vendor_type: str = "Hardware_Fault",
    created: str = "2026-09-17T01:00:00Z",
    *,
    device_id: str = DEVICE_ID,
    drop: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return one synthetic notify-log row, optionally missing some keys."""
    row = {
        "id": row_id,
        "userId": 1001,
        "deviceId": device_id,
        "type": vendor_type,
        "mark": 1,
        "msg": f"Bowl {DEVICE_ID} raised alert {row_id}.",
        "createTime": created,
        "updateTime": created,
    }
    return {key: value for key, value in row.items() if key not in drop}


async def poll(
    hass: HomeAssistant, entry: Any, mock_api: FakeAlwaysFullClient, rows: list[dict[str, Any]]
) -> None:
    """Run one more coordinator poll that returns `rows` from `notify/log`."""
    mock_api.notify_rows_override = rows
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def fired(
    events: list[Event], attribute: str = ATTR_EVENT_TYPE, entity_id: str = ALERT
) -> list[Any]:
    """Return `attribute` from every state change that carried a fired event.

    Reading the state-change stream rather than the final state is what
    makes "fired exactly once" and "fired oldest first" provable: the end
    state alone cannot tell one event from five.

    An event entity's STATE is the timestamp of its last event, so a state
    change whose value is `unknown` (the entity being added) or
    `unavailable` (a failed poll) is not a fired event and is excluded. An
    availability RECOVERY does republish a timestamp without a new event,
    which this helper cannot distinguish -- the outage test below therefore
    asserts on the timestamp itself, which a re-fire always moves forward.
    """
    return [
        event.data["new_state"].attributes.get(attribute)
        for event in events
        if event.data["entity_id"] == entity_id
        and event.data["new_state"] is not None
        and event.data["new_state"].state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        and (
            event.data["old_state"] is None
            or event.data["old_state"].state != event.data["new_state"].state
        )
    ]


async def test_all_event_entities(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Entity ids, registry metadata and the declared event types are pinned."""
    entry = await setup_platform(hass, Platform.EVENT)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_startup_does_not_replay_history(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The first poll after a restart records the log WITHOUT firing.

    The committed capture holds thirteen alerts. Without this suppression
    every Home Assistant restart re-fires all thirteen -- including
    `hardware_fault` and `tilted` -- and every automation bound to them
    runs at once, at whatever hour the restart happened.

    The second poll is not padding, and mutation testing is what showed it:
    an un-primed entity fires nothing while it is merely being ADDED,
    because events are fired from the coordinator-update callback. Its
    replay lands on the first poll after startup instead, so a test that
    stopped at setup would watch the wrong moment and stay green.
    """
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    entry = await setup_platform(hass, Platform.EVENT)

    state = hass.states.get(ALERT)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert fired(events) == []

    await poll(hass, entry, mock_api, HISTORY)

    assert fired(events) == []
    assert hass.states.get(ALERT).state == STATE_UNKNOWN


async def test_a_new_row_fires_exactly_one_event_into_the_state_machine(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """One new alert produces one state change carrying the vendor's detail.

    Asserted on `hass.states`, not on the entity object, because that is
    where a user, an automation and the recorder read this from.

    Honest limit, found by mutating the implementation: this test does NOT
    catch a deleted `async_write_ha_state()` on its own. `CoordinatorEntity`
    writes state at the end of every update anyway, so a poll carrying a
    SINGLE event looks identical either way. What the per-event write buys
    is every event in a BATCH, and
    `test_several_new_rows_each_fire_once_oldest_first` is the test that
    pins it.
    """
    entry = await setup_platform(hass, Platform.EVENT)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    new = alert_row(5100, "Tilted", "2026-09-17T02:00:00Z")
    await poll(hass, entry, mock_api, [new, *HISTORY])

    assert fired(events) == ["tilted"]
    state = hass.states.get(ALERT)
    assert state is not None
    assert state.state != STATE_UNKNOWN
    assert state.attributes[ATTR_EVENT_TYPE] == "tilted"
    assert state.attributes[ATTR_MESSAGE] == new["msg"]
    assert state.attributes[ATTR_CREATED] == "2026-09-17T02:00:00Z"
    assert state.attributes[ATTR_ALERT_ID] == 5100
    assert state.attributes[ATTR_RAW_TYPE] == "Tilted"


async def test_polling_twice_with_identical_rows_fires_nothing_the_second_time(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Re-reading the same page must be silent.

    The coordinator re-reads the same twenty rows every single poll. Without
    dedupe on the row id, a user with an automation on `hardware_fault` is
    paged every scan interval, forever, for one alert that happened once.
    """
    entry = await setup_platform(hass, Platform.EVENT)
    rows = [alert_row(5100, "Hardware_Fault"), *HISTORY]
    await poll(hass, entry, mock_api, rows)

    first = hass.states.get(ALERT)
    assert first is not None
    assert first.attributes[ATTR_EVENT_TYPE] == "hardware_fault"

    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    await poll(hass, entry, mock_api, rows)
    await poll(hass, entry, mock_api, rows)

    assert fired(events) == []
    second = hass.states.get(ALERT)
    assert second is not None
    # The state IS the trigger timestamp, so an un-deduped re-fire moves it.
    assert second.state == first.state
    assert second.last_changed == first.last_changed


async def test_several_new_rows_each_fire_once_oldest_first(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A gap between polls fires every missed alert, in chronological order.

    The three new rows are deliberately built so that the server's order,
    their id order and their chronological order all DISAGREE -- a
    synthetic arrangement, not something the vendor was observed doing.
    Firing in page order, in id order, or newest-first each produces a
    different sequence from the one asserted here, and each would leave
    the entity resting on an alert that is not the newest.

    This is also the ONLY test that fails if `async_write_ha_state()` is
    dropped after `_trigger_event()`: `CoordinatorEntity` writes state once
    at the end of an update regardless, so the loss only shows up as the
    intermediate events of a batch never reaching the state machine --
    i.e. the `hardware_fault` a user automated on is swallowed whenever a
    newer alert lands in the same poll.
    """
    entry = await setup_platform(hass, Platform.EVENT)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    await poll(
        hass,
        entry,
        mock_api,
        [
            alert_row(5100, "Tilted", "2026-09-17T03:00:00Z"),
            alert_row(5101, "Fill_Failed", "2026-09-17T01:00:00Z"),
            alert_row(5102, "Not_Attached", "2026-09-17T02:00:00Z"),
            *HISTORY,
        ],
    )

    assert fired(events) == ["fill_failed", "not_attached", "tilted"]
    assert fired(events, ATTR_ALERT_ID) == [5101, 5102, 5100]
    state = hass.states.get(ALERT)
    assert state is not None
    assert state.attributes[ATTR_EVENT_TYPE] == "tilted"


async def test_a_newer_alert_with_a_lower_id_still_fires(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Dedupe is a set of seen ids, NOT a high-water mark.

    This tests a HYPOTHETICAL, and says so rather than dressing it up.
    Observed reality is the opposite: in the unsanitised capture of the
    real bowl the server assigns ASCENDING ids over time (id 2960335 at
    2026-09-16T23:35:36Z, id 2954773 at 2026-09-15T22:08:57Z) and returns
    the page newest-first. A high-water mark would work against that
    server.

    It would work by relying on an ordering the vendor has never
    documented and that we have observed exactly once, from one account,
    over thirteen rows. If that assumption is ever wrong -- a backfilled
    row, a re-keyed table, a second server assigning ids from its own
    sequence -- a watermark does not degrade, it seeds itself above every
    future alert and silently suppresses all of them for ever. A seen-id
    set costs nothing and cannot fail that way.

    So this is the test that stops someone "optimising" the set into a
    watermark later, and it is the ONLY test that does: every other test
    here gives its new alerts a realistic id above the newest on the page,
    which a watermark handles correctly.
    """
    entry = await setup_platform(hass, Platform.EVENT)
    highest_seen = max(row["id"] for row in HISTORY)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    new = alert_row(highest_seen - 100, "Fill_Failed", "2026-09-17T04:00:00Z")
    assert new["id"] < highest_seen
    await poll(hass, entry, mock_api, [new, *HISTORY])

    assert fired(events) == ["fill_failed"]


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
    ],
)
async def test_every_vendor_alert_type_fires_its_own_event_type(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, vendor_type: str, expected: str
) -> None:
    """Each of the ten vendor spellings maps to its declared event type.

    These are the strings automations are written against, and they are
    exactly the options Task 6's `last_alert` sensor reports, so someone
    writing one automation on the sensor and one on the event does not have
    to learn two spellings of the same alert.
    """
    entry = await setup_platform(hass, Platform.EVENT)
    await poll(hass, entry, mock_api, [alert_row(5100, vendor_type), *HISTORY])

    state = hass.states.get(ALERT)
    assert state is not None
    assert state.attributes[ATTR_EVENT_TYPE] == expected
    assert state.attributes[ATTR_RAW_TYPE] == vendor_type


async def test_an_unrecognised_type_fires_unknown_and_keeps_the_raw_string(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A type the vendor adds later must normalise, never pass through.

    Home Assistant RAISES on an event type outside the declared list, which
    would take down the whole coordinator update -- so an unknown alert
    would break every other entity too. The raw vendor string survives in
    the attributes, so a user can match on it the day it appears.
    """
    entry = await setup_platform(hass, Platform.EVENT)
    await poll(hass, entry, mock_api, [alert_row(5100, "Bowl_Abducted"), *HISTORY])

    state = hass.states.get(ALERT)
    assert state is not None
    assert state.attributes[ATTR_EVENT_TYPE] == "unknown"
    assert state.attributes[ATTR_RAW_TYPE] == "Bowl_Abducted"


async def test_a_row_with_no_type_fires_unknown(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A row missing `type` entirely is an unknown alert, not a crash."""
    entry = await setup_platform(hass, Platform.EVENT)
    await poll(hass, entry, mock_api, [alert_row(5100, drop=("type",)), *HISTORY])

    state = hass.states.get(ALERT)
    assert state is not None
    assert state.attributes[ATTR_EVENT_TYPE] == "unknown"
    assert state.attributes[ATTR_RAW_TYPE] is None


async def test_a_row_with_no_id_is_skipped_without_blocking_the_rest(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """An id-less row cannot be deduped, so it is skipped rather than fired.

    Firing it would re-fire it on every poll for as long as it stayed on
    the page, which is the pager-every-minute failure this entity exists to
    avoid. The rest of the poll must be unaffected: the valid row alongside
    it still fires, exactly once.
    """
    entry = await setup_platform(hass, Platform.EVENT)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    rows = [
        alert_row(None, "Hardware_Fault", "2026-09-17T05:00:00Z", drop=("id",)),
        alert_row(5100, "Tilted", "2026-09-17T04:00:00Z"),
        *HISTORY,
    ]
    await poll(hass, entry, mock_api, rows)
    await poll(hass, entry, mock_api, rows)

    assert fired(events) == ["tilted"]


async def test_an_empty_log_is_fine(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A bowl that has never alerted still gets a usable entity."""
    mock_api.notify_rows_override = []
    entry = await setup_platform(hass, Platform.EVENT)

    state = hass.states.get(ALERT)
    assert state is not None
    assert state.state == STATE_UNKNOWN

    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    await poll(hass, entry, mock_api, [])
    assert fired(events) == []

    # And an alert arriving on a previously empty bowl still fires.
    await poll(hass, entry, mock_api, [alert_row(1, "Tilted")])
    assert fired(events) == ["tilted"]


async def test_an_outage_does_not_replay_the_page_on_recovery(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A failed poll blanks the entity; recovering must not re-fire history.

    The coordinator keeps its last data through a failure, so the recovery
    poll hands the entity the very same rows again.

    Asserted on the timestamp rather than on the state-change stream: the
    recovery itself legitimately republishes the entity's last event, and
    the two are only distinguishable by the value. Home Assistant forces
    each new event's timestamp to be strictly greater than the last, so a
    re-fire CANNOT leave this equal.
    """
    entry = await setup_platform(hass, Platform.EVENT)
    await poll(hass, entry, mock_api, [alert_row(5100, "Tilted"), *HISTORY])
    before = hass.states.get(ALERT)
    assert before is not None
    assert before.state != STATE_UNKNOWN

    mock_api.fail_device_list(AlwaysFullRateLimitError("429"))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ALERT).state == STATE_UNAVAILABLE

    mock_api.device_list_error = None
    await poll(hass, entry, mock_api, [alert_row(5100, "Tilted"), *HISTORY])

    after = hass.states.get(ALERT)
    assert after is not None
    assert after.state == before.state
    assert after.attributes[ATTR_EVENT_TYPE] == "tilted"


async def test_declared_event_types_are_the_sensor_options(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The published list is the full, ordered set -- including `unknown`.

    A membership check would pass with an option renamed or an extra one
    added; both break automations and translated labels, so the whole list
    is pinned.
    """
    await setup_platform(hass, Platform.EVENT)

    state = hass.states.get(ALERT)
    assert state is not None
    assert state.attributes[ATTR_EVENT_TYPES] == [
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
    # One source of truth: the same mapping Task 6's sensor reports with.
    assert tuple(ALERT_TYPE_OPTIONS.values()) == ALERT_OPTIONS[:-1]
    assert ALERT_OPTIONS[-1] == UNKNOWN


def test_the_alert_event_ships_no_device_class() -> None:
    """None of Home Assistant's event device classes describes a water bowl.

    `EventDeviceClass` is doorbell/button/motion and nothing else. The
    membership assertion is a tripwire, not decoration: if a Home Assistant
    upgrade adds a class that WOULD fit, this fails and the decision gets
    revisited instead of quietly staying wrong. Asserted on the description
    rather than on a snapshot, because `--snapshot-update` would silently
    accept a device class someone added by mistake.
    """
    assert {member.value for member in EventDeviceClass} == {"doorbell", "button", "motion"}
    for description in ALERT_EVENTS:
        assert description.device_class is None


async def test_an_older_alert_in_a_batch_still_reaches_the_state_machine(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A serious alert must not be swallowed by a chattier one beside it.

    This bowl announces every fill as `Operation_Confirmation` -- nine of
    the thirteen rows in the live capture are exactly that -- so a
    `Hardware_Fault` followed a few seconds later by a routine fill is an
    ORDINARY sequence, not a contrived one. Both land in the same sixty-
    second poll. If only the newest event reaches the state machine, the
    automation the owner wrote on `hardware_fault` never runs and nothing
    anywhere reports an error.

    Deliberately asserts nothing about ORDER, only that the fault is
    published exactly once. That is what makes this an independent guard
    rather than the ordering test under another name: it passes unchanged
    if the firing order is reversed, and fails only if an event goes
    missing.
    """
    entry = await setup_platform(hass, Platform.EVENT)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    await poll(
        hass,
        entry,
        mock_api,
        [
            alert_row(5101, "Operation_Confirmation", "2026-09-17T01:00:09Z"),
            alert_row(5100, "Hardware_Fault", "2026-09-17T01:00:00Z"),
            *HISTORY,
        ],
    )

    assert fired(events).count("hardware_fault") == 1
    assert fired(events, ATTR_ALERT_ID).count(5100) == 1


async def test_a_string_alert_id_is_deduped_like_any_other(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """An id that arrives as a string still fires once, and only once.

    JSON ids are not guaranteed to stay numeric: a vendor that grows past
    what a JavaScript `Number` holds exactly, or moves to a UUID, ships
    them as strings and nothing announces the change. Dedupe is a set
    lookup, so it does not care -- but any ordering comparison against the
    ids WOULD care, and would break on the first such row.

    This is the second, non-hypothetical guard on the seen-set: a
    high-water mark has to compare this row's `"5100"` against the int ids
    already seen, which raises `TypeError` inside a coordinator listener.
    """
    entry = await setup_platform(hass, Platform.EVENT)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    rows = [alert_row("5100", "Hardware_Fault", "2026-09-17T01:00:00Z"), *HISTORY]
    await poll(hass, entry, mock_api, rows)
    await poll(hass, entry, mock_api, rows)

    assert fired(events) == ["hardware_fault"]
    assert fired(events, ATTR_ALERT_ID) == ["5100"]


async def test_two_alerts_in_the_same_second_fire_in_numeric_id_order(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Within one `createTime` second, the lower id is the older alert.

    `createTime` is stamped to whole seconds, so two alerts sharing one is
    ordinary -- the live capture has a `Tilted` and a `Not_Attached` six
    seconds apart. Tie-breaking on `str(id)` puts `"10000"` before
    `"9999"`, which fires the batch backwards and leaves the entity resting
    on the OLDER alert while looking entirely correct.
    """
    entry = await setup_platform(hass, Platform.EVENT)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)
    same_second = "2026-09-17T01:00:00Z"

    await poll(
        hass,
        entry,
        mock_api,
        [
            alert_row(10000, "Tilted", same_second),
            alert_row(9999, "Fill_Failed", same_second),
            *HISTORY,
        ],
    )

    assert fired(events, ATTR_ALERT_ID) == [9999, 10000]
    assert fired(events) == ["fill_failed", "tilted"]


async def test_the_dedupe_set_evicts_the_oldest_id_never_the_newest(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound must forget the OLDEST id, and it must actually bind.

    Both halves are load-bearing and each one alone is vacuous.

    Evicting the newest instead -- which is all that `popitem()` does in
    place of `pop(next(iter(...)))`, an entirely plausible tidy-up --
    forgets the id it has just remembered, so every alert on the page
    re-fires on every poll for ever. That is the pager-every-minute failure
    coming back through a side door, and only once the set has filled,
    which on a real bowl is weeks of uptime after the change shipped. The
    re-presented newest id is what catches it.

    Never evicting at all is the opposite bug and the reason the second
    half is here: the oldest id must be gone, or this test would pass
    without the bound ever engaging and prove nothing.

    The cap is monkeypatched down rather than exercised at its real 500,
    because the property under test is the DIRECTION, not the number.
    """
    monkeypatch.setattr(event_module, "MAX_REMEMBERED_ALERT_IDS", 3)
    mock_api.notify_rows_override = []
    entry = await setup_platform(hass, Platform.EVENT)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    rows = {n: alert_row(n, "Tilted", f"2026-09-17T0{n}:00:00Z") for n in (1, 2, 3, 4)}

    # A page of two that rolls forward, so no row is still on the page by
    # the time its id is evicted -- otherwise it would re-fire legitimately
    # and this test would be measuring the page size, not the eviction.
    await poll(hass, entry, mock_api, [rows[1]])
    await poll(hass, entry, mock_api, [rows[2], rows[1]])
    await poll(hass, entry, mock_api, [rows[3], rows[2]])
    await poll(hass, entry, mock_api, [rows[4], rows[3]])
    assert fired(events, ATTR_ALERT_ID) == [1, 2, 3, 4]

    # The set now holds 2, 3, 4. The id remembered most recently must still
    # be remembered.
    await poll(hass, entry, mock_api, [rows[4], rows[3]])
    assert fired(events, ATTR_ALERT_ID) == [1, 2, 3, 4]

    # And the id remembered longest ago must be the one that was dropped.
    await poll(hass, entry, mock_api, [rows[4], rows[1]])
    assert fired(events, ATTR_ALERT_ID) == [1, 2, 3, 4, 1]


async def test_each_bowl_fires_only_its_own_alerts(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Two bowls, two entities, and no crosstalk between them.

    The first half is ordinary coverage: the second bowl's entity is a real
    entity that has to work, not a row in a snapshot.

    The second half is the defensive half. `notify/log` is requested per
    device, and the live capture answers with only that device's rows -- so
    the filter is guarding against the vendor ignoring its own `deviceId`
    parameter, which we have not seen it do. The cost of being wrong is
    what justifies it: a two-bowl owner would get every alert duplicated
    onto the other bowl's entity, and an automation that shuts off the
    water to a tilted bowl would act on the wrong bowl, in the wrong room.
    """
    mock_api.notify_rows_override = {DEVICE_ID: [], SECOND_DEVICE_ID: []}
    entry = await setup_platform(hass, Platform.EVENT)
    events = async_capture_events(hass, EVENT_STATE_CHANGED)

    mock_api.notify_rows_override = {
        DEVICE_ID: [alert_row(5100, "Tilted", "2026-09-17T01:00:00Z")],
        SECOND_DEVICE_ID: [
            alert_row(
                6100,
                "Hardware_Fault",
                "2026-09-17T01:00:00Z",
                device_id=SECOND_DEVICE_ID,
            )
        ],
    }
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert fired(events) == ["tilted"]
    assert fired(events, ATTR_EVENT_TYPE, SECOND_ALERT) == ["hardware_fault"]

    # Now the server ignores `deviceId` and hands bowl ONE's alert to both.
    await poll(hass, entry, mock_api, [alert_row(5200, "Fill_Failed", "2026-09-17T02:00:00Z")])

    assert fired(events) == ["tilted", "fill_failed"]
    assert fired(events, ATTR_EVENT_TYPE, SECOND_ALERT) == ["hardware_fault"]
