"""Polling coordinator for the Always Full cloud API.

Polling shape, per cycle:

- exactly ONE `device/list` call. Live captures confirm list rows carry the
  full device state, byte-identical in shape to `device/detail`, so the
  N extra detail calls the vendor app makes buy nothing here.
- per device: `device/config`, today's `drinking/log`, and `notify/log`.
- per device, only on the first poll and then once a day: the
  `drinking/log` for every completed day not yet folded into the lifetime
  total, in windows of at most `HISTORY_CHUNK_DAYS`. See `BowlHistory`.
- `notify/getConfig` only once every `NOTIFY_CONFIG_EVERY_N_POLLS` cycles.
  It is account-level, not per-device, and changes only when the user edits
  it.

A rejected token is a ROUTINE event here, not a once-in-days expiry. The
vendor allows one active session per account: logging in a second time
invalidates the first token immediately (verified against the live server
-- token A answers 200, a second login mints token B, and token A then
answers 651 "token expiration" while token B answers 200). So every time
the owner opens the Always Full phone app while Home Assistant is running,
the next request Home Assistant makes is rejected. That is why the silent
re-login below exists, and why `async_relogin` is shared with the write
path rather than private to this poll.

Error mapping is deliberately asymmetric and must stay that way:

| Failure                            | Raised                  |
| ---------------------------------- | ----------------------- |
| session rejected, re-login works   | (nothing -- silent)     |
| session rejected, re-login fails   | `ConfigEntryAuthFailed` |
| re-login budget spent              | `UpdateFailed`          |
| LOGIN answered 652/602             | `ConfigEntryAuthFailed` |
| rate limit (603, or HTTP 429)      | `UpdateFailed`          |
| timeout / client error / bad JSON  | `UpdateFailed`          |

"Session rejected" is `651` from anywhere, and `652`/`602` from a
TOKEN-BEARING endpoint. The same 652 from the LOGIN endpoint is a
different row entirely, and is not a retry: the server has said the stored
email/password pair is wrong, so re-sending that same pair cannot succeed.
It goes straight to reauth. Which endpoint answered is `api.py`'s to
decide; this module only ever sees the class.

A rate limit must NEVER become `ConfigEntryAuthFailed`: the credentials are
fine, so the reauth flow the user is pushed into would succeed, the next
poll would be rate-limited again, and they would be trapped in a reauth
loop with no way to fix it. The same goes for the re-login budget in
`_spend_relogin_budget`: being held back from re-authenticating says
nothing about the credentials, so it degrades the poll and leaves the
entry loaded.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EMPTY_DEVICE_LIST_EVERY_N_POLLS,
    HISTORY_CHUNK_DAYS,
    LOGGER,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    NOTIFY_CONFIG_EVERY_N_POLLS,
    RELOGIN_MAX_ATTEMPTS,
    RELOGIN_WINDOW_SECONDS,
)
from .exceptions import (
    AlwaysFullAuthError,
    AlwaysFullCredentialsError,
    AlwaysFullError,
    AlwaysFullRateLimitError,
    AlwaysFullReloginThrottledError,
)
from .models import BowlConfig, BowlState, parse_device_timestamp

CREDENTIALS_REJECTED_MESSAGE = "Always Full rejected the stored credentials"

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import AlwaysFullClient

    type AlwaysFullConfigEntry = ConfigEntry[AlwaysFullCoordinator]


@dataclasses.dataclass(slots=True)
class BowlHistory:
    """What the coordinator remembers about one bowl BETWEEN polls.

    `BowlData` is rebuilt from the vendor's answers on every poll; this is
    the part that cannot be, because it is accumulated: the sum of every
    completed day's drinking log, and the last reading of today's total so
    the next one can be compared with it. It lives on the coordinator,
    keyed by device id, and is thrown away whenever the bowl's `units`
    change, because every drinking-log total comes back in the units the
    request named and a sum across two units is not a number.
    """

    units: int
    # The last COMPLETED local day whose total is folded into `total`.
    # Days after it are not yet known; days up to it will not be asked
    # for again. A day the server has no row for is covered too, so a
    # permanently missing day costs one request, not one per poll.
    through: date
    # Sum of `totalCapacity` over every day up to and including `through`.
    total: int = 0
    # The previous poll's `water_today`, with the local date it belonged
    # to, so a rise between two polls can be told from a day rolling over.
    observed_date: str | None = None
    observed_value: int | None = None
    # When a rise in today's total was last SEEN. Not when the pet drank:
    # the vendor's log carries daily aggregates only, so this is the poll
    # that first saw the total go up, at the poll interval's resolution.
    last_drink: datetime | None = None


@dataclasses.dataclass(slots=True)
class BowlData:
    """Everything one poll learned about a single bowl."""

    device_id: str
    state: BowlState
    config: BowlConfig
    water_today: int | None
    notifications: list[dict[str, Any]]
    raw: dict[str, Any]
    # Every completed day's total plus today's, or `None` when the bowl's
    # registration date is unknown and there is no lifetime to sum over.
    water_total: int | None = None
    last_drink: datetime | None = None

    @property
    def firmware_version(self) -> str | None:
        """Firmware version straight off the device row, or `None`."""
        return self.raw.get("version")


def scan_interval_seconds(entry: ConfigEntry) -> int:
    """Return the entry's poll interval, clamped into the supported band.

    Clamping rather than trusting the stored value matters because options
    written before the bounds existed -- or by a YAML/storage edit that
    never went through the options flow -- would otherwise let a user
    hammer a rate-limited cloud API every second.
    """
    configured = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    try:
        seconds = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_SCAN_INTERVAL
    return min(max(seconds, MIN_SCAN_INTERVAL), MAX_SCAN_INTERVAL)


def ha_utc_offset_hours() -> int | None:
    """Return Home Assistant's configured UTC offset in whole hours.

    `dt_util.now()` resolves against `dt_util.DEFAULT_TIME_ZONE`, which
    Home Assistant sets from `hass.config.time_zone` -- it is NOT the OS
    clock's zone. That distinction is the whole point: a HAOS or Docker
    host commonly runs `TZ=UTC` while Home Assistant is configured for the
    user's real zone, and the vendor buckets drinking logs by the
    `timeZone` value we send, so reading the process's zone would silently
    shift every daily total by the difference.

    Truncated toward zero (not floored) so a half-hour zone like -03:30
    becomes -3, matching the vendor's whole-hour field.
    """
    offset = dt_util.now().utcoffset()
    if offset is None:
        return None
    return int(offset.total_seconds() / 3600)


class AlwaysFullCoordinator(DataUpdateCoordinator[dict[str, BowlData]]):
    """Fetch every bowl's state, config, today's water and alerts."""

    config_entry: AlwaysFullConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AlwaysFullConfigEntry,
        client: AlwaysFullClient,
    ) -> None:
        """Set up the poll timer from the entry's (clamped) scan interval."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval_seconds(entry)),
        )
        self.client = client
        self.notify_config: dict[str, Any] | None = None
        # Serialises the write platforms ACROSS platforms, so a user
        # changing several settings at once produces sequential vendor
        # requests instead of a burst at a cloud API that answers 429.
        #
        # Precisely what this adds over `PARALLEL_UPDATES = 1`, because it
        # is easy to get backwards in both directions:
        #
        # `PARALLEL_UPDATES = 1` DOES serialise service calls, not merely
        # entity updates -- `helpers/service.py::entity_service_call` runs
        # every call through `Entity.async_request_call`, which acquires
        # the platform's semaphore. But that semaphore belongs to an
        # `EntityPlatform`, and this integration has FIVE write platforms,
        # each with its own. A number write and a switch write are gated by
        # different semaphores and overlap freely; measured, not assumed.
        # The vendor's rate limit is per ACCOUNT, so that overlap is
        # exactly what it counts.
        #
        # Hence one lock per coordinator -- per account, not per bowl --
        # covering all five. The `PARALLEL_UPDATES` declarations stay: the
        # quality scale expects them, and within a platform they are doing
        # real work. `tests/test_write_serialisation.py` holds the
        # experiment that settles this.
        self.write_lock = asyncio.Lock()
        self._poll_count = 0
        # When each re-login was ATTEMPTED, most recent last, pruned to
        # the last `RELOGIN_WINDOW_SECONDS`. Attempts, not successes: a
        # login that the vendor refused still cost the account a request,
        # which is the thing being budgeted. See `_spend_relogin_budget`.
        self._relogin_attempts: list[float] = []
        # Consecutive polls that found no bowls at all. Drives
        # `_warn_if_no_devices`; zero means the last poll found some.
        self._empty_polls = 0
        # Per-bowl memory that outlives a poll. See `BowlHistory`.
        self._history: dict[str, BowlHistory] = {}

    async def _async_update_data(self) -> dict[str, BowlData]:
        """Poll every bowl, mapping vendor failures onto HA's error model."""
        try:
            try:
                return await self._async_fetch_all()
            except AlwaysFullCredentialsError as err:
                # ORDER IS LOAD-BEARING: this is a SUBCLASS of
                # `AlwaysFullAuthError`, so it must be caught first or the
                # handler below silently swallows it. Swapping these two
                # clauses looks harmless and is not.
                #
                # No retry here. The server has just told us the stored
                # email/password pair is wrong; re-sending the same pair
                # cannot succeed, and doing it anyway would spend an extra
                # vendor request on every poll for as long as the entry
                # stays broken. Straight to reauth, where a human can fix it.
                raise ConfigEntryAuthFailed(CREDENTIALS_REJECTED_MESSAGE) from err
            except AlwaysFullAuthError:
                # Exactly one silent retry. Tokens expire routinely; making
                # the user re-enter a password that is still correct would
                # be noise, not security.
                LOGGER.debug("Token rejected, attempting one silent re-login")
                await self.async_relogin()
                return await self._async_fetch_all()
        except AlwaysFullAuthError as err:
            # Second failure: the stored credentials really are wrong.
            raise ConfigEntryAuthFailed(CREDENTIALS_REJECTED_MESSAGE) from err
        except AlwaysFullRateLimitError as err:
            msg = "Always Full rate-limited this request"
            raise UpdateFailed(msg) from err
        except (TimeoutError, aiohttp.ClientError, ValueError, AlwaysFullError) as err:
            # ValueError covers json.JSONDecodeError from a malformed body.
            msg = f"Error talking to Always Full: {err}"
            raise UpdateFailed(msg) from err

    async def async_relogin(self) -> None:
        """Re-login with the stored credentials, refreshing the client token.

        Public because the WRITE path needs the same recovery and there
        must be exactly one thing that knows how to re-login: the poll path
        calls it above, `entity.async_send_write` calls it for a write
        whose token was rejected. Two copies would drift, and the one that
        drifted would be the one nobody was watching.

        The new token is kept in memory only. Writing it back to the config
        entry here would fire the entry's update listeners mid-poll, which
        reloads the entry underneath the very coordinator doing the work.
        """
        data = self.config_entry.data
        email = data.get(CONF_EMAIL)
        password = data.get(CONF_PASSWORD)
        if not email or not password:
            msg = "No stored credentials to re-authenticate with"
            raise AlwaysFullAuthError(msg)
        self._spend_relogin_budget()
        await self.client.login(email, password)

    def _spend_relogin_budget(self) -> None:
        """Take one re-login from the budget, or refuse the re-login outright.

        THE CHOKEPOINT, and the reason `async_relogin` is the one place
        that knows how to log in: the poll path and all five write
        platforms come through here, so the budget is per ACCOUNT -- which
        is the unit the vendor's own rate limit counts.

        Why a bound exists at all: the vendor allows one session per
        account, so a second client evicts this one every time it talks to
        the server, and a re-login on every rejected session turns routine
        contention into two clients invalidating each other's tokens as
        fast as the network allows. The vendor answers a burst of logins
        with `603` -- measured at roughly eight in a few seconds -- and
        then NEITHER client works. The bound degrades this integration to
        "stale but recovering" instead, which is the outcome that leaves
        the user with a working phone app.

        Not a retry loop and not a sleep: nothing here waits. The refusal
        is immediate, the caller fails this one operation, and the next
        scheduled poll tries again. See `RELOGIN_MAX_ATTEMPTS` for why the
        numbers are what they are.

        `time.monotonic`, never the wall clock: an NTP correction or a DST
        change must not be able to hand back a budget that was spent, or
        freeze one that was not.
        """
        now = time.monotonic()
        self._relogin_attempts = [
            at for at in self._relogin_attempts if now - at < RELOGIN_WINDOW_SECONDS
        ]
        if len(self._relogin_attempts) >= RELOGIN_MAX_ATTEMPTS:
            LOGGER.debug(
                "Not re-authenticating with Always Full: %s attempts already made in the last %s seconds",
                len(self._relogin_attempts),
                RELOGIN_WINDOW_SECONDS,
            )
            msg = (
                f"Always Full has been re-authenticated {RELOGIN_MAX_ATTEMPTS} times in the "
                f"last {RELOGIN_WINDOW_SECONDS} seconds, so this attempt was held back to "
                "avoid being rate-limited. This usually means something else is signed in to "
                "the same Always Full account; the next poll will try again."
            )
            raise AlwaysFullReloginThrottledError(msg)
        # Recorded BEFORE the attempt, not after: a login that fails still
        # spent a request against the account, and a login that hangs must
        # not leave the budget looking untouched.
        self._relogin_attempts.append(now)

    async def _async_fetch_all(self) -> dict[str, BowlData]:
        """Do the actual reads for one poll cycle."""
        # Re-sync the offset every poll so a DST transition is picked up
        # without waiting for a Home Assistant restart.
        self.client.tz_offset_hours = ha_utc_offset_hours()

        envelope = await self.client.device_list() or {}
        rows: list[dict[str, Any]] = envelope.get("data") or []
        now = dt_util.now()
        today = now.strftime("%Y-%m-%d")

        data: dict[str, BowlData] = {}
        for row in rows:
            state = BowlState.from_api(row)
            device_id = state.device_id
            if not device_id:
                LOGGER.debug("Skipping a device row with no deviceId")
                continue
            water_today = await self._async_water_today(device_id, state.units, today)
            history = await self._async_history(device_id, state.units, row.get("createTime"), now)
            if history is not None:
                self._observe_drink(history, today, water_today, now)
            data[device_id] = BowlData(
                device_id=device_id,
                state=state,
                config=BowlConfig.from_api(await self.client.device_config(device_id) or {}),
                water_today=water_today,
                notifications=self._notifications_for(
                    device_id, (await self.client.notify_log(device_id) or {}).get("data") or []
                ),
                raw=row,
                water_total=None if history is None else history.total + (water_today or 0),
                last_drink=None if history is None else history.last_drink,
            )

        if self._poll_count % NOTIFY_CONFIG_EVERY_N_POLLS == 0:
            self.notify_config = await self.client.notify_config()
        self._poll_count += 1

        self._warn_if_no_devices(data)
        return data

    def _warn_if_no_devices(self, data: dict[str, BowlData]) -> None:
        """Say so when a SUCCESSFUL poll found no bowls at all.

        An empty device list is not an error anywhere in this integration's
        error model, and it should not become one: "this account has no
        bowls" is a legitimate answer, and raising `UpdateFailed` for it
        would put a genuinely empty account into a permanent retry loop.

        But the silence around it is what made a real fault
        undiagnosable. The entry reported `loaded`, `last_update_success`
        was true, no exception was raised and the log had nothing in it at
        all -- so from the outside, an integration working normally and one
        that had created no bowl entities looked exactly alike. Dynamic
        entity addition means the entities now appear as soon as the vendor
        lists the bowl; it does not tell anybody why they are missing in the
        meantime, and that is what this line is for.

        WARNING, not DEBUG: for an account that is supposed to have a bowl
        this is always worth reading, and WARNING is what reaches a
        default-level `home-assistant.log` -- the file a person actually
        opens -- without them having to know to turn debug logging on first.

        Rate-limited rather than per-poll, and re-armed on recovery, so a
        list that fills and empties again is reported as the new event it
        is. See `EMPTY_DEVICE_LIST_EVERY_N_POLLS` for why it is neither
        every poll nor once ever.

        The line carries NOTHING identifying -- no device id, no address, no
        token. There is nothing to identify when the list is empty, which
        makes this the easy case of a rule that holds either way.
        """
        if data:
            self._empty_polls = 0
            return

        self._empty_polls += 1
        if self._empty_polls % EMPTY_DEVICE_LIST_EVERY_N_POLLS != 1:
            return

        LOGGER.warning(
            "Always Full signed in successfully but the account returned no bowls, "
            "so no bowl entities will be created. If a bowl is set up in the Always "
            "Full app, this is usually temporary: the next poll that lists it will "
            "create its entities, with no reload needed."
        )

    @staticmethod
    def _notifications_for(device_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop any notify-log row that names a DIFFERENT device.

        `notify/log` is requested per device and the live capture answers
        with only that device's rows, so on the vendor's current behaviour
        this changes nothing. It is here because the cost of being wrong is
        asymmetric: if the endpoint ever ignores its `deviceId` parameter --
        or starts ignoring it after a server-side change nobody tells us
        about -- an owner with two bowls gets every alert duplicated on both
        bowls' entities, with the second bowl's alert entity firing
        automations for a bowl in another room.

        A row with NO `deviceId` is kept. We asked this endpoint for this
        device; an absent field is not evidence that the row belongs to
        another one, and discarding it would lose a real alert.
        """
        return [
            row
            for row in rows
            if not isinstance(row, dict)
            or row.get("deviceId") is None
            or row.get("deviceId") == device_id
        ]

    async def _async_water_today(self, device_id: str, units: int, today: str) -> int | None:
        """Return today's total consumption, or `None` if the day is missing.

        `None`, not `0`: the server omits days it has no data for, and a
        fabricated zero would look like "the pet drank nothing today"
        rather than "we do not know yet".
        """
        rows = await self.client.drinking_log(device_id, units, today, today) or []
        for entry in rows:
            if entry.get("drinkingDate") == today:
                return entry.get("totalCapacity")
        return None

    async def _async_history(
        self,
        device_id: str,
        units: int,
        create_time: Any,
        now: datetime,
    ) -> BowlHistory | None:
        """Bring this bowl's completed-day history up to yesterday, and return it.

        The lifetime total is `history.total` plus today's reading, and
        this is what keeps `history.total` current. On the first poll for a
        bowl it reads every completed day since the bowl was registered;
        after that it reads nothing until a day rolls over, and then only
        the one day that just completed. So the steady-state cost is one
        extra request per bowl per day, not per poll.

        The starting point is the device row's `createTime`, which is UTC
        (docs/VENDOR-API.md §3.3) while the log is bucketed by the local
        day the signed `timeZone` names. Rather than reason about which
        local day the registration instant falls in, the read starts one
        day EARLIER than the local date of that instant. The server
        zero-fills days before the bowl existed (§7), so starting early
        adds nothing to the sum and removes the boundary from the problem.

        Windows are capped at `HISTORY_CHUNK_DAYS` because a window of a
        year or more comes back EMPTY (§7) -- a bowl that old would report
        a lifetime of zero, with no error anywhere.

        Returns `None`, and reports no lifetime, when the row has no
        usable `createTime`: there is no honest starting point to sum
        from, and a total that silently began at an arbitrary date would
        be a smaller number presented as the whole.

        Rebuilt from scratch when `units` change, keeping only
        `last_drink`: the sums are in the old unit and cannot be carried
        over, but when the pet last drank does not depend on the unit.
        """
        history = self._history.get(device_id)
        if history is None or history.units != units:
            created = parse_device_timestamp(create_time)
            if created is None:
                self._history.pop(device_id, None)
                return None
            first = created.astimezone(now.tzinfo).date() - timedelta(days=1)
            history = BowlHistory(
                units=units,
                through=first - timedelta(days=1),
                last_drink=None if history is None else history.last_drink,
            )
            self._history[device_id] = history

        yesterday = now.date() - timedelta(days=1)
        start = history.through + timedelta(days=1)
        while start <= yesterday:
            end = min(start + timedelta(days=HISTORY_CHUNK_DAYS - 1), yesterday)
            rows = await self.client.drinking_log(device_id, units, start.isoformat(), end.isoformat())
            # `total` and `through` advance together, so a failure on a
            # later chunk leaves a consistent partial sum for the next
            # poll to continue from rather than a sum that double-counts.
            history.total += self._sum_rows(rows or [], start.isoformat(), end.isoformat())
            history.through = end
            start = end + timedelta(days=1)
        return history

    @staticmethod
    def _sum_rows(rows: list[Any], start: str, end: str) -> int:
        """Sum `totalCapacity` over the rows dated inside `start`..`end`.

        The window is enforced here as well as in the request because the
        server has been seen answering with a row for TOMORROW (a zero,
        dated one day past the requested `endTime`), and a row outside the
        window would otherwise be summed now and summed again when its own
        day is asked for. `drinkingDate` is `YYYY-MM-DD`, so string
        comparison is date comparison.
        """
        total = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = row.get("drinkingDate")
            value = row.get("totalCapacity")
            if isinstance(day, str) and start <= day <= end and isinstance(value, int | float):
                total += int(value)
        return total

    @staticmethod
    def _observe_drink(history: BowlHistory, today: str, water_today: int | None, now: datetime) -> None:
        """Note whether today's total rose since the previous poll.

        A rise within the same local day is a drink seen at `now`. The
        first poll of a NEW day compares against zero instead of against
        yesterday's closing total, which is higher than any early-morning
        reading and would otherwise hide the first drink of every day.

        The very first observation for a bowl claims nothing: a total of
        600 at startup says the pet drank today, not when. A missing row
        (`None`) is skipped and leaves the previous observation in place,
        so a transient "no row" between two real readings cannot turn the
        second one into a false rise from zero.
        """
        if water_today is None:
            return
        if history.observed_date == today:
            if history.observed_value is not None and water_today > history.observed_value:
                history.last_drink = now
        elif history.observed_date is not None and water_today > 0:
            history.last_drink = now
        history.observed_date = today
        history.observed_value = water_today

    async def async_refresh_after_write(
        self, device_id: str, config: BowlConfig | None = None
    ) -> None:
        """Publish what a successful write changed, without asking the vendor.

        THE VENDOR IS EVENTUALLY CONSISTENT, and that is the whole reason
        this method looks the way it does. Observed on real hardware: the
        owner turned "Flush only after filling" on, the write succeeded
        (`fillWashState` went 0 -> 1 with its siblings untouched), and
        `/app/device/config` read back immediately afterwards still
        returned the PRE-write object. It caught up about twenty seconds
        later.

        This method used to do exactly that re-read and push the result to
        listeners as though it were fresh, so the control the user had just
        operated actively reverted to its old value for those twenty
        seconds -- the very snap-back the re-read was added to prevent. The
        re-read is GONE rather than merely reordered, because it has no
        case left in which it is right: for a config write it returns known
        stale data, and for a write that is not a config write
        (`set_units`, `set_device_type`, `reset/filter`) it re-reads an
        object that write did not touch.

        What replaces it is the value the write itself carried. `config` is
        the caller's already-mutated copy -- the same object the payload
        was built from, so what is cached is exactly what was sent. It is
        copied again here so that a caller reusing its own object later
        cannot silently edit the coordinator's cache.

        This is optimistic, and it is deliberately not authoritative. The
        next scheduled poll rebuilds every bowl from the vendor's own
        answer, so a write the vendor ACCEPTED BUT DID NOT APPLY reverts
        within one poll interval and the user learns the truth. No extra
        poll is requested for that: a refresh fired seconds after the write
        would read the same stale object the re-read did, and re-introduce
        the bounce through the back door.

        A write with no `config` -- the device-row writers above -- has
        nothing to apply optimistically, because the values those entities
        read come from `device/list` rather than from the config object. A
        real poll is the only thing that can show their result, so those
        still request one.
        """
        if config is not None and self.data and device_id in self.data:
            self.data[device_id].config = dataclasses.replace(config)
            # `async_update_listeners`, NOT `async_set_updated_data`, and
            # the difference is the whole confirmation story above.
            # `async_set_updated_data` re-arms the poll timer to
            # now + interval, so a user adjusting settings faster than the
            # interval would push the poll that CHECKS those settings out
            # ahead of themselves indefinitely, and the one thing that can
            # catch a write the vendor accepted but ignored would never
            # run. It also forces `last_update_success` true, which a
            # successful write is not evidence of -- the poll is a
            # different request, and a bowl whose polls are failing must
            # not be made to look healthy by someone flipping a switch.
            #
            # `self.data` is already correct: the config above was mutated
            # in place on the object listeners are holding. All that is
            # left is to tell them to re-read it.
            self.async_update_listeners()
            return

        await self.async_request_refresh()

    async def async_refresh_notify_config(self) -> None:
        """Re-read the ACCOUNT's notification config right after writing it.

        This one DOES re-read, unlike `async_refresh_after_write` above,
        and the difference is evidence rather than taste. The staleness
        that method works around was OBSERVED on `/app/device/config`;
        nothing of the sort has been seen on `notify/getConfig`, and
        removing a read on the strength of a guess about a different
        endpoint would be inventing a vendor behaviour. If a notification
        switch is ever seen snapping back the same way, the fix is the same
        one: cache the object that was saved and drop this read.

        It exists because `notify/getConfig` is polled only once every
        `NOTIFY_CONFIG_EVERY_N_POLLS` cycles, so without this a
        notification switch would sit on its pre-write value for up to ten
        poll intervals before catching up.

        Deliberately does NOT request a full poll: nothing in the device
        data depends on this object, so a full poll would spend a request
        per bowl to learn nothing.
        """
        try:
            self.notify_config = await self.client.notify_config()
        except (
            AlwaysFullError,
            TimeoutError,
            aiohttp.ClientError,
            ValueError,
        ) as err:
            # The save itself already succeeded; failing here only means
            # the switch may lag until the next scheduled fetch.
            LOGGER.warning("Could not re-read the notification config after a write: %s", err)
        else:
            self.async_update_listeners()
