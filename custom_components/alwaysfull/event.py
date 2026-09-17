"""Alert event platform for Always Full.

The vendor gates push notifications behind a paid subscription. The same
alerts are readable for free from `notify/log`, which the coordinator
already polls, so this entity turns them into Home Assistant automation
triggers.

Three facts drive the whole design, each verified rather than assumed:

- `EventEntity._trigger_event()` does NOT write state. Reading the
  installed Home Assistant source, it records the event type, its
  attributes and a (strictly increasing) timestamp, and stops there. Every
  call here is therefore followed by `async_write_ha_state()`; without it
  the entity would never publish anything, with no error and no warning.
- Home Assistant RAISES `ValueError` on an event type outside the declared
  `event_types`. An alert type the vendor adds later must therefore be
  normalised to `unknown`, never passed through -- an exception raised
  inside a coordinator listener would surface as a broken update for every
  other entity too. The vendor's own string is kept in the `raw_type`
  attribute so nothing is actually lost.
- `EventDeviceClass` has exactly three members: doorbell, button and
  motion. None of them describes a water bowl, so this entity ships with
  no device class at all.

Two things this platform deliberately does NOT do:

- It does not read `mark` for severity. The vendor's own app maps
  1 = Ordinary and 2 = Alarm, but live rows carry 0 and 1, so the field
  cannot be trusted. Severity comes from `type`, which the app ignores
  entirely and which is a stable machine-readable code.
- It does not dedupe with a high-water mark. What the live capture actually
  shows is the ordinary shape: the page arrives NEWEST FIRST and ids
  ASCEND over time (id 2960335 at 2026-09-16T23:35:36Z, id 2954773 at
  2026-09-15T22:08:57Z), so a watermark would work -- by depending on an
  ordering the vendor has never documented and that we have seen exactly
  once, from one account, over thirteen rows. A watermark that is wrong
  once does not degrade: it seeds itself above every future alert and
  silently suppresses all of them, for ever. A set of seen ids costs a
  bounded dict and cannot fail that way, whichever way ids are assigned.
  `test_a_newer_alert_with_a_lower_id_still_fires` is what keeps it a set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import callback

from .const import ALERT_OPTIONS, ALERT_TYPE_OPTIONS, ATTR_RAW_TYPE, LOGGER, UNKNOWN
from .entity import AlwaysFullEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import AlwaysFullConfigEntry, AlwaysFullCoordinator

# Read-only platform: nothing here talks to the device, every alert comes
# from one shared coordinator poll. Declared explicitly because the
# integration quality scale expects it stated rather than inferred.
PARALLEL_UPDATES = 0

# The vendor's own fields, republished verbatim beside the normalised event
# type so an automation can use the message the bowl's owner would have got
# by text message.
ATTR_MESSAGE = "message"
ATTR_CREATED = "created"
ATTR_ALERT_ID = "id"

# How many row ids to remember for dedupe. `notify/log` is fetched one page
# of twenty at a time, so this is twenty-five pages' worth: a row can never
# age out of this set while it is still visible on the page (which would
# re-fire it), and the memory stays bounded however long Home Assistant
# runs.
MAX_REMEMBERED_ALERT_IDS = 500

ALERT_EVENTS: tuple[EventEntityDescription, ...] = (
    EventEntityDescription(
        key="alert",
        translation_key="alert",
        # The same options Task 6's `last_alert` sensor reports, from the
        # same mapping, so one automation spelling works against both.
        event_types=ALERT_OPTIONS,
        # NO device class: see the module docstring.
    ),
)


def _chronological(row: dict[str, Any]) -> tuple[str, str]:
    """Return a sort key that puts the OLDEST alert first.

    Ordered by `createTime`, not by id. The ids are server-assigned, and
    while they were observed ascending over time, that is an undocumented
    property of one capture; `createTime` is the field that actually MEANS
    when the alert happened, and it costs nothing to use. `createTime` is a
    fixed-width UTC `%Y-%m-%dT%H:%M:%SZ` string, so lexicographic order IS
    chronological order and no parsing (which could raise mid-update) is
    needed. The id only breaks ties, and only to keep the order stable.
    """
    return (str(row.get("createTime") or ""), str(row.get("id")))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlwaysFullConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one alert event entity per bowl found by the first poll."""
    coordinator = entry.runtime_data
    async_add_entities(
        AlwaysFullAlertEvent(coordinator, device_id, description)
        for device_id in coordinator.data
        for description in ALERT_EVENTS
    )


class AlwaysFullAlertEvent(AlwaysFullEntity, EventEntity):
    """Fires one Home Assistant event per new alert the bowl raises."""

    entity_description: EventEntityDescription

    def __init__(
        self,
        coordinator: AlwaysFullCoordinator,
        device_id: str,
        description: EventEntityDescription,
    ) -> None:
        """Bind the description and start with an empty dedupe set."""
        super().__init__(coordinator, device_id, description)
        # Insertion-ordered, used as a bounded set: the value is never read.
        self._seen_ids: dict[Any, None] = {}

    async def async_added_to_hass(self) -> None:
        """Record the alerts already on the page, WITHOUT firing them.

        This is what stops a Home Assistant restart from replaying history.
        The entity is added after the coordinator's first refresh, so the
        rows in hand at this moment are the ones that arrived before Home
        Assistant was watching -- up to twenty of them, some of them alarms.
        Firing those would trigger every bound automation at once, at
        whatever hour the restart happened to be.

        The cost is the honest one: alerts raised while Home Assistant was
        down are never fired. They remain visible on the `last_alert`
        sensor and in the vendor's app.
        """
        for row in self._rows():
            self._remember(row.get("id"))
        await super().async_added_to_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire one event per alert this poll had not seen before."""
        for row in self._unseen_rows():
            self._fire(row)
        super()._handle_coordinator_update()

    def _rows(self) -> list[dict[str, Any]]:
        """Return this bowl's notify-log rows from the last poll.

        Non-dict entries are dropped rather than trusted: this is untyped
        JSON from a cloud API, and one malformed row must not be able to
        take down the update for every other entity.
        """
        bowl = self.bowl
        if bowl is None:
            return []
        return [row for row in bowl.notifications if isinstance(row, dict)]

    def _unseen_rows(self) -> list[dict[str, Any]]:
        """Return the rows not yet fired, oldest first.

        Oldest first so that firing a batch leaves the entity resting on
        the NEWEST alert, which is what a user reading the entity means by
        "the last thing that happened".
        """
        fresh = []
        for row in self._rows():
            row_id = self._dedupe_key(row)
            if row_id is None or row_id in self._seen_ids:
                continue
            fresh.append(row)
        return sorted(fresh, key=_chronological)

    def _dedupe_key(self, row: dict[str, Any]) -> Any | None:
        """Return the row's id, or `None` if it has no usable one.

        A row with no id cannot be deduped, so it is skipped rather than
        fired: firing it would re-fire it on every single poll for as long
        as it stayed on the page, which is precisely the
        paged-every-minute-forever failure this entity exists to avoid.
        """
        row_id = row.get("id")
        if isinstance(row_id, (int, str)):
            return row_id
        LOGGER.debug("Skipping an Always Full alert row with no usable id: %s", row.get("type"))
        return None

    def _remember(self, row_id: Any) -> None:
        """Mark `row_id` as already fired, evicting the oldest if needed."""
        if not isinstance(row_id, (int, str)):
            return
        self._seen_ids[row_id] = None
        while len(self._seen_ids) > MAX_REMEMBERED_ALERT_IDS:
            self._seen_ids.pop(next(iter(self._seen_ids)))

    def _fire(self, row: dict[str, Any]) -> None:
        """Fire one alert and publish it to the state machine."""
        self._remember(row.get("id"))
        raw_type = row.get("type")
        # `.get` on a non-hashable value would raise, and this runs inside a
        # coordinator listener where raising breaks the whole update.
        key = raw_type if isinstance(raw_type, str) else None
        self._trigger_event(
            ALERT_TYPE_OPTIONS.get(key, UNKNOWN),
            {
                ATTR_MESSAGE: row.get("msg"),
                ATTR_CREATED: row.get("createTime"),
                ATTR_ALERT_ID: row.get("id"),
                # The vendor's untouched spelling, so an alert type added
                # after this table was written is still matchable.
                ATTR_RAW_TYPE: raw_type,
            },
        )
        # NOT optional, and NOT redundant with the write `CoordinatorEntity`
        # does at the end of the update: that one publishes only the LAST
        # event of a poll. Without this line, a poll carrying several new
        # alerts records each one and then overwrites it, so every event but
        # the last never reaches the state machine -- the `hardware_fault`
        # a user automated on is swallowed because a chattier alert landed
        # in the same sixty seconds.
        #
        # A poll carrying ONE event behaves identically either way, so only
        # `test_several_new_rows_each_fire_once_oldest_first` fails if this
        # is removed. That one test is the whole guard.
        self.async_write_ha_state()
