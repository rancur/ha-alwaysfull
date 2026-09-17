"""Switch platform for Always Full: three per-bowl flags and thirteen account ones.

Two tables, because the settings genuinely live in two places:

- `SWITCHES` are per bowl and go through a whole-object config group, the
  same as the numbers and times.
- `NOTIFY_SWITCHES` are per ACCOUNT. `notify/getConfig` takes no device id
  and `notify/saveConfig` changes the setting for every bowl at once, so
  these are created once per config entry rather than once per bowl --
  two switches in front of the user for one server-side flag would each be
  able to make the other wrong.

The ten alert-type switches are generated from `const.ALERT_TYPE_OPTIONS`,
the same mapping the `last_alert` sensor and the alert event entity report.
A second copy of that table here is the whole failure Task 7 removed: an
alert spelt differently in one place is an automation that silently never
fires, and a list that lost an entry is an alert the user cannot reach at
all.

The vendor's wire is not consistent about how a flag is spelled, and each
`set_fn` matches the field it writes rather than a rule:

- The per-bowl DEVICE-CONFIG flags (`fillWashState`, `sleepState`,
  `logState`) are INTS. `True` would serialise as JSON `true`, which is
  not what the server stores and which changes the signed body, so those
  setters write 1/0.
- The account-level notification flags `isTextNotify` / `isEmailNotify`
  are ints too, for the same reason.
- But `enabled`, inside `notifyItems` / `notifyList`, is a real JSON
  BOOLEAN in the capture (`"enabled": false`), so `_set_alert_enabled`
  writes a Python bool. That is not an exception to a rule being broken
  here; it is the field's actual type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription

from .const import ALERT_TYPE_OPTIONS
from .entity import (
    FLUSH_GROUP,
    LOG_GROUP,
    SLEEP_GROUP,
    AlwaysFullAccountEntity,
    AlwaysFullWriteEntity,
    ConfigGroup,
    async_add_bowl_entities,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import AlwaysFullConfigEntry
    from .models import BowlConfig

# One request at a time -- see `number.py`.
PARALLEL_UPDATES = 1

# The two arrays `notify/getConfig` returns. They were byte-identical in
# the live capture and the vendor has never said which one `saveConfig`
# reads back, so BOTH are updated on every save and both are sent.
NOTIFY_ARRAYS = ("notifyItems", "notifyList")

ON = 1
OFF = 0


@dataclass(frozen=True, kw_only=True)
class AlwaysFullSwitchEntityDescription(SwitchEntityDescription):
    """Describes one per-bowl switch."""

    value_fn: Callable[[BowlConfig], bool | None]
    set_fn: Callable[[BowlConfig, bool], None]
    group: ConfigGroup


@dataclass(frozen=True, kw_only=True)
class AlwaysFullNotifySwitchEntityDescription(SwitchEntityDescription):
    """Describes one account-level notification switch."""

    value_fn: Callable[[dict[str, Any]], bool | None]
    set_fn: Callable[[dict[str, Any], bool], None]


def _set_fill_wash_state(config: BowlConfig, on: bool) -> None:
    config.fill_wash_state = ON if on else OFF


def _set_sleep_state(config: BowlConfig, on: bool) -> None:
    config.sleep_state = ON if on else OFF


def _set_log_state(config: BowlConfig, on: bool) -> None:
    config.log_state = ON if on else OFF


SWITCHES: tuple[AlwaysFullSwitchEntityDescription, ...] = (
    AlwaysFullSwitchEntityDescription(
        key="flush_after_filling",
        translation_key="flush_after_filling",
        value_fn=lambda config: config.fill_wash_state == ON,
        set_fn=_set_fill_wash_state,
        group=FLUSH_GROUP,
    ),
    AlwaysFullSwitchEntityDescription(
        key="sleep_mode",
        translation_key="sleep_mode",
        value_fn=lambda config: config.sleep_state == ON,
        set_fn=_set_sleep_state,
        group=SLEEP_GROUP,
    ),
    AlwaysFullSwitchEntityDescription(
        key="drinking_log",
        translation_key="drinking_log",
        value_fn=lambda config: config.log_state == ON,
        set_fn=_set_log_state,
        group=LOG_GROUP,
    ),
)


def _flag(field: str) -> Callable[[dict[str, Any]], bool | None]:
    """Return a reader for one account-level int flag."""
    return lambda config: config.get(field) == ON


def _set_flag(field: str) -> Callable[[dict[str, Any], bool], None]:
    """Return a writer for one account-level int flag."""

    def _set(config: dict[str, Any], on: bool) -> None:
        config[field] = ON if on else OFF

    return _set


def _alert_enabled(vendor_type: str) -> Callable[[dict[str, Any]], bool | None]:
    """Return a reader for one alert type's `enabled` flag.

    Reads `notifyItems` and falls back to `notifyList`: the two agreed in
    the live capture, but if they ever disagree this reports the first one
    rather than silently preferring whichever happened to be iterated last.
    """

    def _value(config: dict[str, Any]) -> bool | None:
        for array in NOTIFY_ARRAYS:
            for row in config.get(array) or []:
                if isinstance(row, dict) and row.get("type") == vendor_type:
                    return bool(row.get("enabled"))
        return None

    return _value


def _set_alert_enabled(vendor_type: str) -> Callable[[dict[str, Any], bool], None]:
    """Return a writer that flips one alert type in BOTH arrays.

    Updating only the array this entity happened to read leaves the other
    stale, and the server reads back whichever it likes -- so the switch
    would appear to work and then revert on the next fetch.

    Raises `ValueError` if the type is in neither array. That means the
    vendor has renamed it, and writing the object back unchanged would be
    a switch that reports success and does nothing at all.
    """

    def _set(config: dict[str, Any], on: bool) -> None:
        found = False
        for array in NOTIFY_ARRAYS:
            for row in config.get(array) or []:
                if isinstance(row, dict) and row.get("type") == vendor_type:
                    row["enabled"] = on
                    found = True
        if not found:
            msg = f"Always Full no longer offers the {vendor_type!r} alert"
            raise ValueError(msg)

    return _set


NOTIFY_SWITCHES: tuple[AlwaysFullNotifySwitchEntityDescription, ...] = (
    AlwaysFullNotifySwitchEntityDescription(
        key="text_alerts",
        translation_key="text_alerts",
        value_fn=_flag("isTextNotify"),
        set_fn=_set_flag("isTextNotify"),
    ),
    AlwaysFullNotifySwitchEntityDescription(
        key="email_alerts",
        translation_key="email_alerts",
        value_fn=_flag("isEmailNotify"),
        set_fn=_set_flag("isEmailNotify"),
    ),
    *(
        AlwaysFullNotifySwitchEntityDescription(
            key=f"alert_{option}",
            translation_key=f"alert_{option}",
            value_fn=_alert_enabled(vendor_type),
            set_fn=_set_alert_enabled(vendor_type),
        )
        for vendor_type, option in ALERT_TYPE_OPTIONS.items()
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlwaysFullConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up per-bowl switches for every bowl, plus one set of account switches.

    The account switches are added once, here: they belong to the account
    rather than to any bowl, so they must not wait on a device list and
    must not be re-added when one arrives.
    """
    coordinator = entry.runtime_data
    async_add_entities(
        AlwaysFullNotifySwitch(coordinator, description) for description in NOTIFY_SWITCHES
    )
    async_add_bowl_entities(
        coordinator,
        async_add_entities,
        lambda device_id: (
            AlwaysFullSwitch(coordinator, device_id, description) for description in SWITCHES
        ),
    )


class AlwaysFullSwitch(AlwaysFullWriteEntity, SwitchEntity):
    """One on/off setting on one bowl."""

    entity_description: AlwaysFullSwitchEntityDescription

    @property
    def is_on(self) -> bool | None:
        """Return whether the setting is on, from the last poll."""
        bowl = self.bowl
        if bowl is None:
            return None
        return self.entity_description.value_fn(bowl.config)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        await self._async_set(on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        await self._async_set(on=False)

    async def _async_set(self, *, on: bool) -> None:
        await self.async_write_config(
            self.entity_description.group,
            lambda config: self.entity_description.set_fn(config, on),
        )


class AlwaysFullNotifySwitch(AlwaysFullAccountEntity, SwitchEntity):
    """One account-level notification setting."""

    entity_description: AlwaysFullNotifySwitchEntityDescription

    @property
    def is_on(self) -> bool | None:
        """Return whether the setting is on, from the last notification fetch."""
        config = self.coordinator.notify_config
        if config is None:
            return None
        return self.entity_description.value_fn(config)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        await self._async_set(on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        await self._async_set(on=False)

    async def _async_set(self, *, on: bool) -> None:
        await self.async_save_notify_config(
            lambda config: self.entity_description.set_fn(config, on),
        )
