"""Base entity identity, device info, availability, and the partial-write guard."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityDescription
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alwaysfull.const import DOMAIN
from custom_components.alwaysfull.entity import (
    FILTER_GROUP,
    FLUSH_GROUP,
    LOG_GROUP,
    MAINTENANCE_GROUP,
    SLEEP_GROUP,
    WATER_GROUP,
    AlwaysFullEntity,
    AlwaysFullWriteEntity,
    ConfigGroup,
)
from custom_components.alwaysfull.exceptions import AlwaysFullRateLimitError

from .conftest import DEVICE_ID, FakeAlwaysFullClient, load_fixture_data

DESCRIPTION = EntityDescription(key="filter_life")


async def _load(hass: HomeAssistant) -> MockConfigEntry:
    """Load one config entry against whatever the fake client is set up to serve."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": "T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _entity(hass: HomeAssistant) -> AlwaysFullEntity:
    """Load an entry and build a bare base entity against its coordinator."""
    entry = await _load(hass)
    return AlwaysFullEntity(entry.runtime_data, DEVICE_ID, DESCRIPTION)


async def _write_entity(hass: HomeAssistant) -> AlwaysFullWriteEntity:
    """Load an entry and build a bare WRITE entity against its coordinator.

    The write bases are tested here rather than through one of the five
    write platforms because the guard under test belongs to the base: a
    platform-level test would prove it for that platform's groups and say
    nothing about the others.
    """
    entry = await _load(hass)
    return AlwaysFullWriteEntity(entry.runtime_data, DEVICE_ID, DESCRIPTION)


async def test_device_info_is_keyed_on_the_vendor_device_id(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Identifiers must survive removing and re-adding the integration.

    Anything derived from the config entry id changes on re-add, which
    orphans every recorder history row for the device.
    """
    entity = await _entity(hass)

    assert entity.unique_id == f"{DEVICE_ID}_filter_life"
    assert entity.has_entity_name is True

    info = entity.device_info
    assert info["identifiers"] == {(DOMAIN, DEVICE_ID)}
    assert info["manufacturer"] == "Always Full"
    assert info["model"] == '9" Bowl'
    assert info["sw_version"] == "3.6.2"
    assert info["name"] == "Test Bowl"


async def test_available_tracks_update_success_and_membership(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Availability needs both a good poll and this device still present."""
    entity = await _entity(hass)
    assert entity.available is True

    mock_api.fail_device_list(AlwaysFullRateLimitError("429"))
    await entity.coordinator.async_refresh()
    assert entity.available is False

    mock_api.device_list_error = None
    await entity.coordinator.async_refresh()
    assert entity.available is True

    unknown = AlwaysFullEntity(entity.coordinator, "ffeeddccbbaa", DESCRIPTION)
    assert unknown.available is False


# One case per config group: the key `/app/device/config` has to omit for
# that group's payload to carry a `None`, and the wire field it becomes.
#
# `waterConfig` is not here because no omitted CONFIG key can make it
# partial -- its own `None` comes from the device row instead, and has its
# own test below.
UNREAD_FIELD_CASES: list[tuple[str, ConfigGroup, str, str]] = [
    ("flush/cleanTime", FLUSH_GROUP, "cleanTime", "cleanTime"),
    ("flush/fillWashState", FLUSH_GROUP, "fillWashState", "fillWashState"),
    ("sleep/sleepState", SLEEP_GROUP, "sleepState", "sleepState"),
    # `capacity` on the way in is `filterCapacity` on the way out -- the
    # rename is the reason this case names both.
    ("filter/filterCapacity", FILTER_GROUP, "capacity", "filterCapacity"),
    ("maintenance/cleanWarnTime", MAINTENANCE_GROUP, "cleanWarnTime", "cleanWarnTime"),
    ("log/logState", LOG_GROUP, "logState", "logState"),
]


@pytest.mark.parametrize(
    ("group", "omitted_config_key", "wire_field"),
    [case[1:] for case in UNREAD_FIELD_CASES],
    ids=[case[0] for case in UNREAD_FIELD_CASES],
)
async def test_write_refuses_a_group_whose_field_was_never_read(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    group: ConfigGroup,
    omitted_config_key: str,
    wire_field: str,
) -> None:
    """A field the config read never returned must refuse the write, not blank it.

    Every one of these endpoints replaces the whole object it is sent, and
    `api.py` strips `None` values out of the body before signing. So a
    payload carrying one `None` does not send a null -- it sends a SHORTER
    object, and the server blanks whatever is missing from it. The user
    changes one setting and silently loses another: their flush duration,
    or, on the filter group, `filterCapacity` alongside a
    `filterCanUseTime: 0` that turns filter tracking off entirely.

    Substituting the field's default here would be the same corruption with
    extra steps -- it would write a value nobody chose and report success.
    """
    config: dict[str, Any] = load_fixture_data("device_config")
    del config[omitted_config_key]
    mock_api.device_config_override = config

    entity = await _write_entity(hass)

    with pytest.raises(HomeAssistantError) as err:
        await entity.async_write_config(group, lambda _config: None)

    # Readable by the person who pressed the control, and specific enough
    # that a bug report says which field was missing.
    assert wire_field in str(err.value)
    assert "has not been read yet" in str(err.value)

    # And nothing reached the vendor. A refusal that still sent the partial
    # object would be worse than no guard at all, because the user would
    # then be told it failed AND lose the setting.
    assert mock_api.writes == []


async def test_water_write_refuses_when_the_device_row_has_no_units(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
) -> None:
    """`waterConfig` gets its `None` from the DEVICE row, not the config object.

    `units` is echoed from `device/detail`/`device/list` rather than stored
    in the config, so the way this group goes partial is a device row with
    a null `units` -- and a `waterConfig` sent without `units` is read
    against the wrong unit, which silently rescales both thresholds.
    """
    rows: list[dict[str, Any]] = load_fixture_data("device_list_multi")["data"]
    for row in rows:
        row["units"] = None
    mock_api.device_rows_override = rows

    entity = await _write_entity(hass)

    with pytest.raises(HomeAssistantError) as err:
        await entity.async_write_config(WATER_GROUP, lambda _config: None)

    assert "units" in str(err.value)
    assert mock_api.writes == []


async def test_write_still_goes_through_when_the_group_is_complete(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
) -> None:
    """The guard must not refuse a whole config, or it has broken every write.

    Without this, deleting the guard's condition and refusing EVERYTHING
    would pass the tests above, and nothing in the integration would be
    able to write at all.
    """
    entity = await _write_entity(hass)

    await entity.async_write_config(FLUSH_GROUP, lambda _config: None)

    assert [method for method, _payload in mock_api.writes] == ["set_flush_config"]
