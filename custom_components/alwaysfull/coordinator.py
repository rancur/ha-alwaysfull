"""Polling coordinator for the Always Full cloud API.

Polling shape, per cycle:

- exactly ONE `device/list` call. Live captures confirm list rows carry the
  full device state, byte-identical in shape to `device/detail`, so the
  N extra detail calls the vendor app makes buy nothing here.
- per device: `device/config`, today's `drinking/log`, and `notify/log`.
- `notify/getConfig` only once every `NOTIFY_CONFIG_EVERY_N_POLLS` cycles.
  It is account-level, not per-device, and changes only when the user edits
  it.

Error mapping is deliberately asymmetric and must stay that way:

| Failure                          | Raised                  |
| -------------------------------- | ----------------------- |
| token rejected, re-login works   | (nothing -- silent)     |
| token rejected, re-login fails   | `ConfigEntryAuthFailed` |
| credentials rejected (652/602)   | `ConfigEntryAuthFailed` |
| HTTP 429                         | `UpdateFailed`          |
| timeout / client error / bad JSON| `UpdateFailed`          |

Credentials rejected is NOT the same row as a rejected token, and is not a
retry: the server has said the stored email/password pair is wrong, so
re-sending that same pair cannot succeed. Retrying it would buy nothing and
cost the vendor one extra request per poll for as long as the entry stays
broken. It goes straight to reauth.

A rate limit must NEVER become `ConfigEntryAuthFailed`: the credentials are
fine, so the reauth flow the user is pushed into would succeed, the next
poll would be rate-limited again, and they would be trapped in a reauth
loop with no way to fix it.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
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
    LOGGER,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    NOTIFY_CONFIG_EVERY_N_POLLS,
)
from .exceptions import (
    AlwaysFullAuthError,
    AlwaysFullCredentialsError,
    AlwaysFullError,
    AlwaysFullRateLimitError,
)
from .models import BowlConfig, BowlState

CREDENTIALS_REJECTED_MESSAGE = "Always Full rejected the stored credentials"

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import AlwaysFullClient

    type AlwaysFullConfigEntry = ConfigEntry[AlwaysFullCoordinator]


@dataclasses.dataclass(slots=True)
class BowlData:
    """Everything one poll learned about a single bowl."""

    device_id: str
    state: BowlState
    config: BowlConfig
    water_today: int | None
    notifications: list[dict[str, Any]]
    raw: dict[str, Any]

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
        self._poll_count = 0

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
                await self._async_relogin()
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

    async def _async_relogin(self) -> None:
        """Re-login with the stored credentials, refreshing the client token.

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
        await self.client.login(email, password)

    async def _async_fetch_all(self) -> dict[str, BowlData]:
        """Do the actual reads for one poll cycle."""
        # Re-sync the offset every poll so a DST transition is picked up
        # without waiting for a Home Assistant restart.
        self.client.tz_offset_hours = ha_utc_offset_hours()

        envelope = await self.client.device_list() or {}
        rows: list[dict[str, Any]] = envelope.get("data") or []
        today = dt_util.now().strftime("%Y-%m-%d")

        data: dict[str, BowlData] = {}
        for row in rows:
            state = BowlState.from_api(row)
            device_id = state.device_id
            if not device_id:
                LOGGER.debug("Skipping a device row with no deviceId")
                continue
            data[device_id] = BowlData(
                device_id=device_id,
                state=state,
                config=BowlConfig.from_api(await self.client.device_config(device_id) or {}),
                water_today=await self._async_water_today(device_id, state.units, today),
                notifications=self._notifications_for(
                    device_id, (await self.client.notify_log(device_id) or {}).get("data") or []
                ),
                raw=row,
            )

        if self._poll_count % NOTIFY_CONFIG_EVERY_N_POLLS == 0:
            self.notify_config = await self.client.notify_config()
        self._poll_count += 1

        return data

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

    async def async_refresh_after_write(self, device_id: str) -> None:
        """Re-read one device's config right after writing to it.

        The vendor API is eventually consistent enough that a plain
        debounced refresh can return the PRE-write config, making a setting
        visibly snap back in the UI. Re-reading just this device's config
        and pushing it to listeners immediately avoids that, while the
        requested refresh still picks up any state the write changed.
        """
        if self.data and device_id in self.data:
            try:
                raw = await self.client.device_config(device_id) or {}
            except (
                AlwaysFullError,
                TimeoutError,
                aiohttp.ClientError,
                ValueError,
            ) as err:
                # The write itself already succeeded; failing here only
                # means the UI may lag until the next poll, so log it and
                # let the scheduled refresh below sort it out.
                LOGGER.warning("Could not re-read config for %s after a write: %s", device_id, err)
            else:
                self.data[device_id].config = BowlConfig.from_api(raw)
                self.async_set_updated_data(self.data)

        await self.async_request_refresh()

    async def async_refresh_notify_config(self) -> None:
        """Re-read the ACCOUNT's notification config right after writing it.

        The per-device analogue of `async_refresh_after_write`, and it
        exists for the same reason: `notify/getConfig` is polled only once
        every `NOTIFY_CONFIG_EVERY_N_POLLS` cycles, so without this a
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
