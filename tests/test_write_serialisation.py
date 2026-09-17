"""The coordinator's write lock: one vendor request at a time, per account.

There is a subtlety here that cost a controlled experiment to settle, and
it is the reason this module exists at all rather than living in
`test_number.py`.

`PARALLEL_UPDATES = 1` DOES serialise service calls -- not only entity
updates. `homeassistant/helpers/service.py::entity_service_call` runs every
call through `entity.async_request_call`, which acquires the platform's
`parallel_updates` semaphore. Verified by reading the installed source and
then by experiment: three concurrent `number.set_value` calls reach the
client strictly one at a time with the write lock REMOVED.

So a serialisation test written against one platform is a false green. It
passes whether or not this integration holds any lock, because Home
Assistant is doing the work.

What `PARALLEL_UPDATES` does NOT cover is the gap this module tests. The
semaphore belongs to an `EntityPlatform`, and this integration has five
write platforms -- number, switch, select, time, button -- each with its
own. A number write and a switch write are gated by different semaphores
and overlap freely. Measured with the lock removed: two writes inside the
client at once. The vendor's rate limit is per ACCOUNT, so that overlap is
exactly what it punishes, and closing it needs a lock the integration owns.

Every test below therefore spans two platforms. The gate is what makes the
overlap observable: with writes parked inside the client, an unserialised
implementation has two sitting there and a serialised one has one.
"""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.components.number import (
    ATTR_VALUE,
    SERVICE_SET_VALUE,
)
from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_ON,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.alwaysfull.exceptions import AlwaysFullError

from .conftest import (
    DEVICE_ID,
    FakeAlwaysFullClient,
    entity_id_for,
    entity_id_for_key,
    setup_platforms,
)

WRITE_PLATFORMS = [Platform.NUMBER, Platform.SWITCH]

# More turns of the event loop than the handful of awaits between a service
# call and the client, so "nothing else got through" means the lock held it
# and not that the test looked too early. The proof that it is enough is
# that removing the lock makes these tests fail.
DRAIN_TURNS = 200


async def drain() -> None:
    """Let every ready coroutine run as far as it can get.

    Not `hass.async_block_till_done()`, which would wait forever on the
    tasks deliberately parked on the write gate.
    """
    for _ in range(DRAIN_TURNS):
        await asyncio.sleep(0)


def number_call(hass: HomeAssistant, key: str, value: float) -> asyncio.Task[None]:
    """Start one `number.set_value` against the first bowl."""
    return asyncio.create_task(
        hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id_for(hass, NUMBER_DOMAIN, f"{DEVICE_ID}_{key}"), ATTR_VALUE: value},
            blocking=True,
        )
    )


def switch_call(hass: HomeAssistant, entity_id: str) -> asyncio.Task[None]:
    """Start one `switch.turn_on`."""
    return asyncio.create_task(
        hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )
    )


async def test_writes_on_different_platforms_do_not_overlap(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A number write and a switch write must not be in flight together.

    Home Assistant's own semaphore cannot do this: they are different
    platforms and therefore different semaphores. Without the coordinator's
    lock this reaches the client with two writes open at once, against an
    account whose rate limit counts both.
    """
    await setup_platforms(hass, WRITE_PLATFORMS)
    gate = asyncio.Event()
    mock_api.write_gate = gate

    writes = [
        number_call(hass, "flush_interval", 30),
        switch_call(hass, entity_id_for(hass, SWITCH_DOMAIN, f"{DEVICE_ID}_sleep_mode")),
    ]
    await drain()

    assert len(mock_api.writes) == 1, "a second platform's write got out early"
    assert mock_api.writes_in_flight == 1

    gate.set()
    await asyncio.gather(*writes)
    await hass.async_block_till_done()

    assert [name for name, _payload in mock_api.writes] == [
        "set_flush_config",
        "set_sleep_config",
    ]
    assert mock_api.max_writes_in_flight == 1


async def test_an_account_level_save_takes_the_same_lock(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The notification switches share the account's lock with the bowls.

    They write a different endpoint through a different entity base, so
    this is a second `async with` site and not the one above wearing a
    different hat -- and `notify/saveConfig` spends the same account's
    rate-limit budget as everything else.
    """
    await setup_platforms(hass, WRITE_PLATFORMS)
    gate = asyncio.Event()
    mock_api.write_gate = gate

    writes = [
        switch_call(hass, entity_id_for_key(hass, SWITCH_DOMAIN, "_alert_tilted")),
        number_call(hass, "flush_interval", 30),
    ]
    await drain()

    assert len(mock_api.writes) == 1
    assert mock_api.writes_in_flight == 1

    gate.set()
    await asyncio.gather(*writes)
    await hass.async_block_till_done()

    assert [name for name, _payload in mock_api.writes] == [
        "save_notify_config",
        "set_flush_config",
    ]
    assert mock_api.max_writes_in_flight == 1


async def test_a_refused_write_releases_the_lock(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """One server refusal must not wedge every later write for ever.

    `async with` releases on the exception path, so this passes today. It
    is here because the failure it guards is unbounded and silent: acquire
    without a matching release and the integration never writes again until
    Home Assistant restarts, with nothing in the log to say why.
    """
    entry = await setup_platforms(hass, WRITE_PLATFORMS)
    mock_api.write_error = AlwaysFullError("Device offline")

    with pytest.raises(HomeAssistantError, match="Device offline"):
        await number_call(hass, "flush_interval", 30)

    assert not entry.runtime_data.write_lock.locked()

    mock_api.write_error = None
    await number_call(hass, "flush_interval", 45)
    await hass.async_block_till_done()

    assert [name for name, _payload in mock_api.writes] == [
        "set_flush_config",
        "set_flush_config",
    ]


async def test_a_refused_account_save_releases_the_lock(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The account path releases on failure too, for the same reason."""
    entry = await setup_platforms(hass, WRITE_PLATFORMS)
    mock_api.write_error = AlwaysFullError("Account suspended")

    with pytest.raises(HomeAssistantError, match="Account suspended"):
        await switch_call(hass, entity_id_for_key(hass, SWITCH_DOMAIN, "_alert_tilted"))

    assert not entry.runtime_data.write_lock.locked()

    mock_api.write_error = None
    await number_call(hass, "flush_interval", 45)
    await hass.async_block_till_done()

    assert [name for name, _payload in mock_api.writes] == [
        "save_notify_config",
        "set_flush_config",
    ]
