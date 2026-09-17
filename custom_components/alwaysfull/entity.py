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
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import AlwaysFullCoordinator
from .exceptions import AlwaysFullError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.helpers.entity import EntityDescription

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
        """
        bowl = self.bowl
        info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
        )
        if bowl is None:
            return info
        if bowl.state.device_name:
            info["name"] = bowl.state.device_name
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
        await self.async_write(lambda: group.send(self.coordinator.client, payload))

    async def async_write(self, action: Callable[[], Awaitable[Any]]) -> None:
        """Run one write, surface any failure, then re-read this bowl.

        Everything happens under the coordinator's write lock: the send AND
        the re-read that follows it, because the two are one operation and
        letting the next write start while this one's read-back is still in
        flight is exactly the overlap the lock exists to prevent.

        `action` is called INSIDE the try so that a payload built lazily by
        the caller is covered by the same error mapping.
        """
        async with self.coordinator.write_lock:
            try:
                await action()
            except WRITE_FAILURES as err:
                msg = f"Always Full could not apply the change: {err}"
                raise HomeAssistantError(msg) from err
            await self.coordinator.async_refresh_after_write(self._device_id)

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
        entry = coordinator.config_entry
        # The config flow sets the entry's unique id to the account, which
        # survives a remove-and-re-add; the entry id does not, and keying
        # on it would orphan the user's history every time.
        account = entry.unique_id or entry.entry_id
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
        async with self.coordinator.write_lock:
            try:
                await self.coordinator.client.save_notify_config(updated)
            except WRITE_FAILURES as err:
                msg = f"Always Full could not apply the change: {err}"
                raise HomeAssistantError(msg) from err
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
