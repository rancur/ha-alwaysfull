"""Button platform: resetting the filter's elapsed life.

One button, one endpoint, no payload beyond the device. The things worth
pinning are that it is the RESET endpoint and not a filter-config write
(which would set the filter's total lifetime instead of zeroing the
elapsed time), and that pressing it refreshes the bowl so the filter
sensors stop reporting the old figure.
"""

from __future__ import annotations

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.alwaysfull.exceptions import (
    AlwaysFullError,
    AlwaysFullRateLimitError,
)

from .conftest import (
    DEVICE_ID,
    SECOND_DEVICE_ID,
    FakeAlwaysFullClient,
    only_write,
    setup_platform,
)

# Both bowls' buttons. The object id comes from the entity NAME
# ("Reset filter life"), not from the description key.
WALL = "button.test_bowl_reset_filter_life"
PUMP = "button.second_bowl_reset_filter_life"


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    """Press a button and let the write finish."""
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_all_button_entities(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Entity ids and registry metadata are pinned."""
    entry = await setup_platform(hass, Platform.BUTTON)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_pressing_resets_only_the_bowl_that_was_pressed(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The reset endpoint, for exactly one device, with no other write.

    Asserting the OTHER writers stayed idle is the point: a filter reset
    implemented as `set_filter_config(filterCanUseTime=0)` would look like
    it worked and would instead disable filter tracking outright.
    """
    await setup_platform(hass, Platform.BUTTON)

    await _press(hass, PUMP)

    assert only_write(mock_api, "reset_filter") == {"device_id": SECOND_DEVICE_ID}
    assert [name for name, _payload in mock_api.writes] == ["reset_filter"]


async def test_each_bowl_gets_its_own_button(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Two bowls, two buttons, each naming its own device."""
    await setup_platform(hass, Platform.BUTTON)

    await _press(hass, WALL)
    await _press(hass, PUMP)

    assert [payload["device_id"] for _name, payload in mock_api.writes] == [
        DEVICE_ID,
        SECOND_DEVICE_ID,
    ]


async def test_pressing_refreshes_that_bowl(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A reset changes what the filter sensors should read, so re-read it."""
    await setup_platform(hass, Platform.BUTTON)
    # The debounced full poll that follows a write runs immediately the
    # first time, and it re-reads BOTH bowls' configs. Failing the device
    # list makes it contribute nothing, so the one extra config read below
    # can only have come from the scoped re-read.
    mock_api.fail_device_list(AlwaysFullRateLimitError("429"))
    before = list(mock_api.device_config_calls)

    await _press(hass, WALL)

    assert mock_api.device_config_calls == [*before, DEVICE_ID]


async def test_a_refused_press_raises(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A server refusal is an error the user sees, not a silent no-op."""
    await setup_platform(hass, Platform.BUTTON)
    mock_api.write_error = AlwaysFullError("Device offline")

    with pytest.raises(HomeAssistantError, match="Device offline"):
        await _press(hass, WALL)
