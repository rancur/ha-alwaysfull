"""Base entities shared by every Always Full platform.

Three bases live here:

- `AlwaysFullEntity`, for anything that reads one bowl.
- `AlwaysFullWriteEntity`, for anything that also writes to one bowl.
- `AlwaysFullAccountEntity`, for the notification settings, which are
  account-level rather than per-bowl and would otherwise be duplicated
  once per bowl with every copy driving the same server-side setting.

The write bases exist so that the five write platforms cannot each invent
their own answer to the same three questions: how a whole-object
read-modify-write is assembled, what a failed write looks like to the
user, and what happens afterwards so the UI does not bounce back to the
old value.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER, MANUFACTURER
from .coordinator import CREDENTIALS_REJECTED_MESSAGE, AlwaysFullCoordinator
from .exceptions import AlwaysFullAuthError, AlwaysFullCredentialsError, AlwaysFullError
from .models import device_label

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity import Entity, EntityDescription
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import AlwaysFullClient
    from .coordinator import BowlData
    from .models import BowlConfig

# The name the account's settings appear under. It is a service device, not
# a second bowl: Home Assistant renders those differently and does not
# offer them a firmware version or an area.
ACCOUNT_DEVICE_NAME = "Always Full account"

# Everything a write can fail with, mapped to one readable error. A cloud
# API that is down, slow, rate-limiting or refusing the value are all the
# same thing from the user's chair: the change did not happen.
WRITE_FAILURES = (AlwaysFullError, TimeoutError, aiohttp.ClientError)


@dataclasses.dataclass(frozen=True, kw_only=True)
class ConfigGroup:
    """One vendor config endpoint, as a whole-object read-modify-write.

    `sleepConfig`, `waterConfig`, `logConfig` and the rest all replace the
    entire object they are sent. Sending only the field that changed is
    accepted by the server and blanks the others, so every write goes
    through a `to_*_payload()` builder that reconstructs the whole group
    from the cached config.

    A builder can only reconstruct what the config read gave it, so a
    payload carrying a `None` is a partial group wearing a full group's
    shape -- `api.py` strips the `None` out before signing and the vendor
    blanks the field. `_reject_partial_group` refuses those rather than
    sending them; see its docstring.

    `build` takes the bowl as well as the config because `waterConfig` has
    to echo `units`, which lives on the device row and not in the config
    object at all.
    """

    build: Callable[[BowlConfig, str, BowlData], dict[str, Any]]
    send: Callable[[AlwaysFullClient, dict[str, Any]], Awaitable[Any]]


# The payload builders are Task 3's, and the device-id parameter name is
# Task 2's (`devNo` for these, `deviceId` for `set_units`). Nothing here
# constructs a wire body of its own.
FLUSH_GROUP = ConfigGroup(
    build=lambda config, device_id, _bowl: config.to_flush_payload(device_id),
    send=lambda client, payload: client.set_flush_config(**payload),
)
SLEEP_GROUP = ConfigGroup(
    build=lambda config, device_id, _bowl: config.to_sleep_payload(device_id),
    send=lambda client, payload: client.set_sleep_config(**payload),
)
FILTER_GROUP = ConfigGroup(
    build=lambda config, device_id, _bowl: config.to_filter_payload(device_id),
    send=lambda client, payload: client.set_filter_config(**payload),
)
MAINTENANCE_GROUP = ConfigGroup(
    build=lambda config, device_id, _bowl: config.to_maintenance_payload(device_id),
    send=lambda client, payload: client.set_maintenance_config(**payload),
)
WATER_GROUP = ConfigGroup(
    build=lambda config, device_id, bowl: config.to_water_payload(device_id, bowl.state.units),
    send=lambda client, payload: client.set_water_config(**payload),
)
LOG_GROUP = ConfigGroup(
    build=lambda config, device_id, _bowl: config.to_log_payload(device_id),
    send=lambda client, payload: client.set_log_config(**payload),
)


async def async_send_write(
    coordinator: AlwaysFullCoordinator,
    action: Callable[[], Awaitable[Any]],
) -> None:
    """Run one vendor write, recovering ONCE from a token rejected mid-session.

    The write path gets exactly the recovery the poll path has had all
    along, and for the same reason: the vendor is SINGLE-SESSION. Logging
    in a second time invalidates the first token immediately (verified
    against the live server: token A answers 200, a second login mints
    token B, token A then answers 651 "token expiration", token B answers
    200). So the owner opening the Always Full phone app signs Home
    Assistant out -- an entirely ordinary thing to do with your own bowl.

    Without this, the asymmetry was the bug. A poll healed itself silently
    while a write raised "Always Full could not apply the change: token
    expiration" at the person who had just moved a slider, with nothing
    they could do but wait for a poll to fix the session behind their back.

    ONE re-login and ONE retry, never a loop. Against a single-session
    vendor an unbounded retry is how two Home Assistant instances -- or
    Home Assistant and the phone app -- log each other out for ever, each
    re-login invalidating the token the other just minted.

    The three except clauses are a decision table, and the ORDER of the
    inner two is load-bearing in exactly the way the coordinator's is:

    | Failure during a write            | Outcome                        |
    | --------------------------------- | ------------------------------ |
    | token rejected, re-login works    | the retry succeeds, silently   |
    | token rejected, re-login/retry no | readable `HomeAssistantError`  |
    | credentials rejected (652/602)    | reauth, with NO login attempt  |
    | rate limit (429)                  | readable error, NO login       |

    `AlwaysFullCredentialsError` is a SUBCLASS of `AlwaysFullAuthError`, so
    it must be re-raised before the base clause or the re-login branch
    swallows it -- and re-sending a pair the server has just rejected
    cannot succeed, it only spends a request. It goes to Home Assistant's
    reauth prompt instead, which is where a human can actually fix it and
    is what the coordinator does for the same case.

    A rate limit is NOT an auth failure: it is an `AlwaysFullError` and
    nothing else, so it falls to the last clause untouched. Mapping it to
    reauth would trap the user in a prompt that succeeds and changes
    nothing, and re-logging in would spend another request on an account
    that has just been told to slow down.

    Nothing here logs or raises a token value, a password or a `sign`
    header. The vendor's own message ("token expiration") is a description,
    not a secret.
    """
    try:
        try:
            await action()
        except AlwaysFullCredentialsError:
            # ORDER IS LOAD-BEARING -- see the docstring. Re-raised to the
            # outer handler so there is one place that maps this to reauth.
            raise
        except AlwaysFullAuthError:
            LOGGER.debug("A write was rejected for its token; attempting one silent re-login")
            await coordinator.async_relogin()
            await action()
    except AlwaysFullCredentialsError as err:
        # Also the landing place for a re-login answered "invalid email
        # address or password": raised from inside the handler above, so it
        # cannot be caught by that handler's siblings.
        #
        # `ConfigEntryAuthFailed` is what the coordinator raises, but only
        # the coordinator's own machinery turns that into a reauth flow --
        # nothing does so for an exception out of a service call, so the
        # flow is started explicitly. `_if_available` because starting a
        # flow the integration does not implement would be a no-op error.
        coordinator.config_entry.async_start_reauth_if_available(coordinator.hass)
        raise ConfigEntryAuthFailed(CREDENTIALS_REJECTED_MESSAGE) from err
    except WRITE_FAILURES as err:
        msg = f"Always Full could not apply the change: {err}"
        raise HomeAssistantError(msg) from err


def _reject_partial_group(payload: dict[str, Any]) -> None:
    """Refuse a config-group write that would blank the fields it omits.

    A `None` in a built payload does NOT reach the vendor as a null.
    `AlwaysFullClient.request` strips `None` values out of the body before
    signing it, so the group arrives SHORTER than it should be -- and these
    endpoints replace the whole object they are sent, blanking whatever is
    missing. A `None` here is therefore a silent write of a value nobody
    chose.

    It happens when the config read this cache was filled from came back
    empty or short: `/app/device/config` answering `data: null` is what the
    coordinator's `or {}` already anticipates, and `BowlConfig.from_api({})`
    leaves `cleanTime`, `fillWashState`, `sleepState`, `filterCapacity`,
    `cleanWarnTime` and `logState` all `None`. The next setting the user
    changes inside that poll window would then wipe their flush duration,
    or send `filterCanUseTime: 0` and disable filter tracking entirely.

    Substituting each missing field's default would be the same corruption
    with extra steps: it writes a value the user never chose and reports
    success. Refusing is recoverable -- the next poll refills the cache --
    and it is the only outcome that never loses a setting, so the message
    is written for the person who pressed the control and names the fields
    so a bug report can carry them.
    """
    unread = sorted(key for key, value in payload.items() if value is None)
    if not unread:
        return
    msg = (
        "Always Full could not send this setting because the bowl's "
        f"configuration has not been read yet ({', '.join(unread)} unknown). "
        "Nothing was changed. Try again in a few moments."
    )
    raise HomeAssistantError(msg)


def account_key(entry: ConfigEntry) -> str:
    """Return a short, stable, NON-IDENTIFYING key for this entry's account.

    The account-level entities have to be keyed on something, and the
    entry's unique id is the right thing to derive it from: the config
    flow sets it to the account, so it survives a remove-and-re-add, where
    `entry_id` does not and would orphan the user's history every time.

    It must not BE that value, though, because the entry's unique id is the
    account's email address. Unique ids and device identifiers are copied
    verbatim into a diagnostics download, and diagnostics downloads get
    pasted into public issue trackers by users who have no idea there is an
    address in them. A truncated SHA-256 keeps every property that matters
    -- stable across restarts and re-adds, distinct per account, derived
    from nothing else -- and carries no address. Twelve hex characters is
    48 bits, which is not a collision risk across the handful of accounts
    one Home Assistant will ever hold.

    Not a security control: an email address is guessable, so this is not
    claimed to be irreversible. It is here so that the address is not
    sitting in plain text in a file people share.
    """
    account = entry.unique_id or entry.entry_id
    return hashlib.sha256(account.encode()).hexdigest()[:12]


@callback
def async_add_bowl_entities(
    coordinator: AlwaysFullCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[str], Iterable[Entity]],
) -> None:
    """Add `build(device_id)` for every bowl, now AND as bowls appear later.

    Every per-bowl platform goes through this instead of enumerating
    `coordinator.data` once at forward time, and the reason is a fault
    found on a real install: after the config flow finished, Home Assistant
    had the account-level entities and NOT ONE of the per-bowl ones. The
    entry said `loaded`, nothing was logged, and reloading it by hand
    created all of them.

    The mechanism is that an empty device list is a SUCCESSFUL poll.
    `_async_fetch_all` returns `{}`, `last_update_success` stays true, and
    `async_config_entry_first_refresh` -- which IS awaited before the
    platforms are forwarded -- raises nothing, because "this account has no
    bowls" is a legitimate answer that the integration cannot tell apart
    from "the vendor did not list them this time". A one-shot enumeration
    turns that single call into a permanent verdict: the platforms set up
    with an empty device list and never look again, for as long as the
    entry stays loaded.

    Ordering alone therefore cannot fix this, and moving the first refresh
    would not have: whatever leaves that first list empty -- a token minted
    seconds earlier, a vendor-side cache, a bowl registered a minute after
    the account -- the next poll is the thing that knows better, so the
    next poll is what has to be able to add the entities.

    It also buys the case nobody had covered either way: a SECOND bowl
    added to the account months later now appears on the following poll
    instead of waiting for a restart.

    `added` is keyed on the vendor device id and never emptied. A bowl that
    drops out of one poll and comes back must not be added twice -- its
    entities were never removed, they went unavailable (see
    `AlwaysFullEntity.available`), and re-adding them would collide on
    their entity ids.
    """
    added: set[str] = set()

    @callback
    def _add_new_bowls() -> None:
        new = [device_id for device_id in coordinator.data or {} if device_id not in added]
        if not new:
            return
        added.update(new)
        async_add_entities(entity for device_id in new for entity in build(device_id))

    coordinator.config_entry.async_on_unload(coordinator.async_add_listener(_add_new_bowls))
    _add_new_bowls()


class AlwaysFullEntity(CoordinatorEntity[AlwaysFullCoordinator]):
    """Common identity, device info and availability for one bowl."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AlwaysFullCoordinator,
        device_id: str,
        description: EntityDescription,
    ) -> None:
        """Bind this entity to one bowl and one entity description."""
        super().__init__(coordinator)
        self.entity_description = description
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def bowl(self) -> BowlData | None:
        """Return this entity's bowl from the last poll, or `None` if it vanished."""
        return (self.coordinator.data or {}).get(self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Describe the physical bowl this entity belongs to.

        `identifiers` is keyed on the VENDOR's device id, never on anything
        derived from the config entry id. A config entry gets a fresh id
        every time the integration is removed and re-added; keying the
        device on it would create a brand-new device and orphan every
        recorder history row the user had built up.

        `name` is ALWAYS set, and that is not a tidiness point. Omitting it
        is not "no name": Home Assistant falls back to the config entry
        title, and this integration titles the entry with the account's
        EMAIL ADDRESS. The vendor's `deviceName` is null until somebody
        renames the bowl in their app, which is the default state and not
        an edge case, so the common install put the owner's address into
        the device name and from there into every per-bowl entity id --
        visible in the UI, written into automations, and in every
        screenshot and issue report.

        The fallback is `device_label`, the same derivation the diagnostics
        download and the warning log already use. It keeps the two
        properties that matter -- identical every time, distinct per bowl,
        so an owner with two of them can tell which is which -- and it is
        derived from the device id, so nothing about the account can reach
        it. One definition, so the name a user sees in the UI is the name
        their log lines and their diagnostics file use.
        """
        bowl = self.bowl
        info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
            name=device_label(self._device_id),
        )
        if bowl is None:
            return info
        # `.strip()`, because a name of spaces is a name the user cannot
        # see and would hand Home Assistant the entry title right back.
        vendor_name = (bowl.state.device_name or "").strip()
        if vendor_name:
            info["name"] = vendor_name
        info["model"] = f'{bowl.state.bowl_size_inches}" Bowl'
        if bowl.firmware_version:
            info["sw_version"] = bowl.firmware_version
        return info

    @property
    def available(self) -> bool:
        """Available only while the last poll worked AND this bowl still exists.

        Deliberately not `super().available`: a bowl removed from the
        account keeps its entities registered but must stop reporting
        stale values as if they were live.
        """
        return self.coordinator.last_update_success and self._device_id in (
            self.coordinator.data or {}
        )


class AlwaysFullWriteEntity(AlwaysFullEntity):
    """One bowl's entity that can also change a setting on it."""

    _attr_entity_category = EntityCategory.CONFIG

    async def async_write_config(
        self,
        group: ConfigGroup,
        mutate: Callable[[BowlConfig], None],
    ) -> None:
        """Apply `mutate` to this bowl's cached config and send `group` entire.

        The mutation lands on a COPY. If the write is refused, the
        coordinator's cache must still describe the device as it actually
        is -- showing the value the user asked for after the device
        rejected it is worse than showing the old one.
        """
        bowl = self._require_bowl()
        config = dataclasses.replace(bowl.config)
        mutate(config)
        try:
            payload = group.build(config, self._device_id, bowl)
        except ValueError as err:
            # The payload builders' own rules, e.g. `to_water_payload`'s
            # "min must be below max unless both are zero". Their messages
            # are written for a human to read, so they are surfaced as-is
            # rather than replaced with a generic failure.
            raise HomeAssistantError(str(err)) from err
        _reject_partial_group(payload)
        await self.async_write(lambda: group.send(self.coordinator.client, payload), config)

    async def async_write(
        self,
        action: Callable[[], Awaitable[Any]],
        config: BowlConfig | None = None,
    ) -> None:
        """Run one write, surface any failure, then publish what it changed.

        `config` is the mutated copy the payload was built from, and it is
        passed on ONLY after `async_send_write` has returned without
        raising -- the point at which the vendor has accepted the write.
        The coordinator caches it so the entity reads the new value at
        once, because the vendor's own read-back is stale for around twenty
        seconds afterwards and would otherwise revert the control the user
        just operated. See `AlwaysFullCoordinator.async_refresh_after_write`.

        A caller with no config to hand (`select`, `button` -- they write
        the device row, not the config object) omits it and gets a real
        poll instead.

        Everything happens under the coordinator's write lock: the send AND
        the publish that follows it, because the two are one operation and
        letting the next write start while this one is still finishing is
        exactly the overlap the lock exists to prevent.

        `action` is called by `async_send_write`, which maps the failures
        and does the one-shot re-login recovery -- so a payload built
        lazily by the caller is covered by the same error mapping.

        The re-login and the retry happen while the lock is still HELD, and
        that is deliberate: dropping it to re-authenticate would let the
        next queued write start against the very token that was just
        rejected, and the two would then race to replace each other's
        session on a vendor that allows only one.
        """
        async with self.coordinator.write_lock:
            await async_send_write(self.coordinator, action)
            await self.coordinator.async_refresh_after_write(self._device_id, config)

    def _require_bowl(self) -> BowlData:
        """Return this bowl, or refuse the write if it is no longer known."""
        bowl = self.bowl
        if bowl is None:
            msg = "This bowl is no longer on the Always Full account"
            raise HomeAssistantError(msg)
        return bowl


class AlwaysFullAccountEntity(CoordinatorEntity[AlwaysFullCoordinator]):
    """A setting that belongs to the ACCOUNT rather than to any one bowl.

    The notification settings are the only ones like this: `notify/getConfig`
    takes no device id and `notify/saveConfig` changes the setting for every
    bowl at once. Creating these once per bowl would put two switches in
    front of the user for one server-side flag, each of them able to make
    the other wrong.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AlwaysFullCoordinator,
        description: EntityDescription,
    ) -> None:
        """Bind this entity to the account and one entity description."""
        super().__init__(coordinator)
        self.entity_description = description
        account = account_key(coordinator.config_entry)
        self._attr_unique_id = f"{account}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"account_{account}")},
            manufacturer=MANUFACTURER,
            name=ACCOUNT_DEVICE_NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Available only once the notification config has actually been read.

        It is fetched once every ten polls, so "not read yet" is a real
        state and not a corner case. Reporting it as "everything is off"
        would invite the user to switch on an alert that was already on.
        """
        return self.coordinator.last_update_success and self.coordinator.notify_config is not None

    async def async_save_notify_config(
        self,
        mutate: Callable[[dict[str, Any]], None],
    ) -> None:
        """Apply `mutate` to a copy of the whole notification object and save it.

        `notify/saveConfig` replaces everything it is sent, and the live
        object carries TWO parallel arrays (`notifyItems` and `notifyList`)
        whose relationship the vendor has never documented. So the object
        that goes back is the object that came out, with one field changed
        -- never a reconstruction from the fields this integration happens
        to know about.
        """
        config = self.coordinator.notify_config
        if config is None:
            msg = "The Always Full notification settings have not been read yet"
            raise HomeAssistantError(msg)

        updated = _deep_copy_config(config)
        try:
            mutate(updated)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        # The same account-wide lock the per-bowl writes take: these are
        # requests against the same rate-limited account.
        #
        # Through `async_send_write` like every per-bowl write, so the
        # account settings get the same one-shot re-login: the token the
        # phone app invalidated is the same token for both, and a fix that
        # reached only `AlwaysFullWriteEntity` would leave the notification
        # switches failing on exactly the error the bowl entities recover
        # from.
        async with self.coordinator.write_lock:
            await async_send_write(
                self.coordinator,
                lambda: self.coordinator.client.save_notify_config(updated),
            )
            await self.coordinator.async_refresh_notify_config()


def _deep_copy_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy deep enough that mutating it cannot touch the cache.

    The nested rows inside `notifyItems`/`notifyList` are what get edited,
    so a shallow copy would edit the coordinator's own object in place --
    and a refused save would leave the cache claiming a change the server
    never accepted.
    """
    return {
        key: [dict(row) if isinstance(row, dict) else row for row in value]
        if isinstance(value, list)
        else value
        for key, value in config.items()
    }
