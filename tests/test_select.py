"""Select platform: display units and bowl size.

Both of these write through a dedicated endpoint rather than a config
group, and both carry a trap: `setUnits` is the one writer that names the
device `deviceId` instead of `devNo` (covered at the transport layer in
`test_api_endpoints.py`), and `deviceType` is an INVERTED enum where 0
means the 9-inch bowl.
"""

from __future__ import annotations

import pytest
from homeassistant.components.select import (
    ATTR_OPTION,
    SERVICE_SELECT_OPTION,
)
from homeassistant.components.select import (
    DOMAIN as SELECT_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.alwaysfull.exceptions import (
    AlwaysFullError,
)

from .conftest import (
    DEVICE_ID,
    SECOND_DEVICE_ID,
    FakeAlwaysFullClient,
    device_row,
    only_write,
    setup_platform,
)

WALL = "select.test_bowl_"
PUMP = "select.second_bowl_"


async def _select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    """Call `select.select_option` and let the write finish."""
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: option},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_all_select_entities(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Entity ids, options and current values are all pinned."""
    entry = await setup_platform(hass, Platform.SELECT)
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_units_are_read_per_bowl(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """`units` 1 is millilitres and 2 is fluid ounces, and it is per bowl."""
    await setup_platform(hass, Platform.SELECT)

    assert hass.states.get(f"{WALL}units").state == "ml"
    assert hass.states.get(f"{PUMP}units").state == "fl_oz"


@pytest.mark.parametrize(("option", "wire"), [("ml", 1), ("fl_oz", 2)])
async def test_units_write_the_vendors_enum(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, option: str, wire: int
) -> None:
    """Both directions, because a swapped pair passes either one alone."""
    await setup_platform(hass, Platform.SELECT)

    await _select(hass, f"{WALL}units", option)

    assert only_write(mock_api, "set_units") == {"device_id": DEVICE_ID, "units": wire}


async def test_bowl_size_decodes_the_inverted_enum(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """`deviceType` 0 is the NINE inch bowl and 1 is the SEVEN inch one.

    The fixture's two bowls carry 0 and 1 respectively, so an
    implementation that read the enum the way anyone would guess reports
    both of these backwards.
    """
    await setup_platform(hass, Platform.SELECT)

    assert hass.states.get(f"{WALL}bowl_size").state == "9_inch"
    assert hass.states.get(f"{PUMP}bowl_size").state == "7_inch"


@pytest.mark.parametrize(("option", "wire"), [("9_inch", 0), ("7_inch", 1)])
async def test_bowl_size_writes_the_inverted_enum(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, option: str, wire: int
) -> None:
    """Nine inches must reach the wire as 0, seven inches as 1."""
    await setup_platform(hass, Platform.SELECT)

    await _select(hass, f"{PUMP}bowl_size", option)

    assert only_write(mock_api, "set_device_type") == {
        "device_id": SECOND_DEVICE_ID,
        "device_type": wire,
    }


async def test_a_select_write_requests_a_full_poll(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The write is followed by a full poll of the account.

    Both selects read the DEVICE ROW -- `units` is not in the config object
    at all -- so there is no written config to publish optimistically and
    `device/list` is the only thing that can show the new value.
    """
    await setup_platform(hass, Platform.SELECT)
    before = mock_api.device_list_calls

    await _select(hass, f"{WALL}units", "fl_oz")
    await hass.async_block_till_done()

    assert mock_api.device_list_calls == before + 1


async def test_a_refused_select_write_raises(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A server refusal is an error the user sees."""
    await setup_platform(hass, Platform.SELECT)
    mock_api.write_error = AlwaysFullError("Device offline")

    with pytest.raises(HomeAssistantError, match="Device offline"):
        await _select(hass, f"{WALL}units", "fl_oz")

    assert hass.states.get(f"{WALL}units").state == "ml"


async def test_an_unrecognised_bowl_size_reads_unknown_rather_than_nine_inch(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A `deviceType` outside the enum is not evidence of a 9-inch bowl.

    `models.device_type_to_bowl_size_inches` falls back to 9" so that an
    unrecognised value cannot raise inside a coordinator update, which is
    right for a reading nobody acts on -- the device page's model string.
    It is not right HERE. This entity's own docstring promises `None` for
    a value outside the enum, and the reason is that the user acts on what
    it says: a select claiming "9 inch" for a bowl whose size the vendor
    described in a way we do not understand invites them to "correct" it
    to 7 inch, which WRITES a size to the bowl.

    `unknown` says what is true. The two real options are still there to
    pick from, so nothing is taken away.
    """
    mock_api.device_rows_override = [device_row(deviceType=99)]
    await setup_platform(hass, Platform.SELECT)

    assert hass.states.get(f"{WALL}bowl_size").state == STATE_UNKNOWN
    # The units select on the same bowl is unaffected.
    assert hass.states.get(f"{WALL}units").state == "ml"
