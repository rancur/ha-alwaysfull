"""Time platform for Always Full: the sleep window.

`sleepStart` and `sleepEnd` are MINUTES SINCE LOCAL MIDNIGHT on the wire.
The conversion both ways lives in `models.py` (`minutes_to_time` /
`time_to_minutes`) and is reached here only through the `sleep_start` /
`sleep_end` properties, so nothing in this module does arithmetic on a
clock.

Both entities write the `sleepConfig` group entire. The window and the
sleep-mode switch share that endpoint, so a payload carrying only the time
that changed would switch sleep mode off as a side effect of moving the
window by five minutes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.time import TimeEntity, TimeEntityDescription

from .entity import SLEEP_GROUP, AlwaysFullWriteEntity

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import time

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import AlwaysFullConfigEntry
    from .models import BowlConfig

# One request at a time -- see `number.py`.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class AlwaysFullTimeEntityDescription(TimeEntityDescription):
    """Describes one writable Always Full time of day."""

    value_fn: Callable[[BowlConfig], time]
    set_fn: Callable[[BowlConfig, time], None]


def _set_sleep_start(config: BowlConfig, value: time) -> None:
    config.sleep_start = value


def _set_sleep_end(config: BowlConfig, value: time) -> None:
    config.sleep_end = value


TIMES: tuple[AlwaysFullTimeEntityDescription, ...] = (
    AlwaysFullTimeEntityDescription(
        key="sleep_start",
        translation_key="sleep_start",
        value_fn=lambda config: config.sleep_start,
        set_fn=_set_sleep_start,
    ),
    AlwaysFullTimeEntityDescription(
        key="sleep_end",
        translation_key="sleep_end",
        value_fn=lambda config: config.sleep_end,
        set_fn=_set_sleep_end,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlwaysFullConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one sleep window per bowl found by the first poll."""
    coordinator = entry.runtime_data
    async_add_entities(
        AlwaysFullTime(coordinator, device_id, description)
        for device_id in coordinator.data
        for description in TIMES
    )


class AlwaysFullTime(AlwaysFullWriteEntity, TimeEntity):
    """One end of one bowl's sleep window."""

    entity_description: AlwaysFullTimeEntityDescription

    @property
    def native_value(self) -> time | None:
        """Return this end of the window, from the last poll."""
        bowl = self.bowl
        if bowl is None:
            return None
        return self.entity_description.value_fn(bowl.config)

    async def async_set_value(self, value: time) -> None:
        """Move this end of the window, sending the sleep group entire."""
        await self.async_write_config(
            SLEEP_GROUP,
            lambda config: self.entity_description.set_fn(config, value),
        )
