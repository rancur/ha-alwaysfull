"""Config-entry setup, unload and reauth behaviour."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.alwaysfull import PLATFORMS
from custom_components.alwaysfull.binary_sensor import BINARY_SENSORS
from custom_components.alwaysfull.button import BUTTONS
from custom_components.alwaysfull.const import DEFAULT_SCAN_INTERVAL, DOMAIN
from custom_components.alwaysfull.coordinator import AlwaysFullCoordinator
from custom_components.alwaysfull.event import ALERT_EVENTS
from custom_components.alwaysfull.number import NUMBERS
from custom_components.alwaysfull.select import SELECTS
from custom_components.alwaysfull.sensor import SENSORS
from custom_components.alwaysfull.switch import NOTIFY_SWITCHES, SWITCHES
from custom_components.alwaysfull.time import TIMES

from .conftest import (
    DEVICE_ID,
    SECOND_DEVICE_ID,
    FakeAlwaysFullClient,
    setup_platforms,
    zone_unlike_host,
)

# Every per-bowl description table, so "the full entity set" is counted from
# the tables themselves. A hard-coded 27 would go stale the first time a
# sensor is added and would then be asserting yesterday's integration.
PER_BOWL_DESCRIPTIONS = (
    *BINARY_SENSORS,
    *BUTTONS,
    *ALERT_EVENTS,
    *NUMBERS,
    *SELECTS,
    *SENSORS,
    *SWITCHES,
    *TIMES,
)

# The two bowls `device_list_multi.json` carries a usable `deviceId` for.
FIXTURE_DEVICE_IDS = (DEVICE_ID, SECOND_DEVICE_ID)


def _entity_ids_per_bowl(
    entity_registry: er.EntityRegistry, entry: MockConfigEntry
) -> dict[str, list[str]]:
    """Return this entry's registered entity ids, grouped by bowl."""
    grouped: dict[str, list[str]] = {device_id: [] for device_id in FIXTURE_DEVICE_IDS}
    for registered in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        for device_id in FIXTURE_DEVICE_IDS:
            if registered.unique_id.startswith(f"{device_id}_"):
                grouped[device_id].append(registered.entity_id)
    return grouped


async def test_setup_and_unload(hass: HomeAssistant, mock_api: FakeAlwaysFullClient) -> None:
    """A good entry loads, exposes its coordinator, and unloads cleanly."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": "T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, AlwaysFullCoordinator)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_api_auth_fails: FakeAlwaysFullClient
) -> None:
    """A rejected token that cannot be refreshed sends the user to reauth."""
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "user@example.com", "password": "pw"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )


async def test_client_gets_stored_token_and_ha_time_zone(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The client is built with the stored token and HA's configured zone.

    The offset must come from `hass.config.time_zone`, never the OS clock:
    a container running `TZ=UTC` under an HA configured for another zone
    would otherwise mis-bucket every drinking-log day.

    The zone is picked at runtime (see `zone_unlike_host`) as one that is
    DST-free AND whose offset differs from this host's. Hard-coding one
    would make the test host-dependent in both directions: vacuous on a
    runner in that zone, and failing on correct code on a runner that had
    been compared against.
    """
    zone, expected_offset = zone_unlike_host()
    await hass.config.async_set_time_zone(zone)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": "STORED"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert mock_api.token == "STORED"
    # A fixed, known value for the chosen zone -- which by construction is
    # NOT this host's offset, so an OS-clock implementation cannot produce it.
    assert mock_api.constructed_tz_offset_hours == expected_offset


async def test_a_first_setup_creates_every_per_bowl_entity(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """A fresh install must show the whole integration, not a fraction of it.

    Asserted from scratch and with NO reload anywhere, because reloading is
    what hid the original fault: on real hardware the first setup produced
    only the account-level entities, the entry reported `loaded` with
    nothing in the log, and a reload created the rest. A user who does not
    know to reload concludes the integration is broken.
    """
    entry = await setup_platforms(hass, list(PLATFORMS))
    assert entry.state is ConfigEntryState.LOADED

    per_bowl = _entity_ids_per_bowl(entity_registry, entry)
    for device_id in FIXTURE_DEVICE_IDS:
        assert len(per_bowl[device_id]) == len(PER_BOWL_DESCRIPTIONS), device_id

    registered = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert len(registered) == len(FIXTURE_DEVICE_IDS) * len(PER_BOWL_DESCRIPTIONS) + len(
        NOTIFY_SWITCHES
    )


async def test_bowls_the_first_poll_did_not_return_appear_without_a_reload(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """A device list that is empty at setup must not be a permanent verdict.

    This is the shape of the hardware failure. An empty `device/list` is a
    SUCCESSFUL poll -- `last_update_success` is true, the entry loads, and
    nothing is logged -- so a platform that enumerates `coordinator.data`
    once, at forward time, silently creates zero per-bowl entities and
    never looks again. Whatever made that first call come back empty (a
    freshly minted token, a vendor cache, a bowl registered a minute later),
    the integration stayed a fraction of itself until somebody reloaded it
    by hand.

    The second poll here is the ORDINARY scheduled one, fired by moving the
    clock. Nothing reloads the entry.
    """
    mock_api.device_rows_override = []
    entry = await setup_platforms(hass, list(PLATFORMS))
    assert entry.state is ConfigEntryState.LOADED

    # The starting condition, and it is the same under either behaviour:
    # an account with no bowls has no per-bowl entities.
    assert _entity_ids_per_bowl(entity_registry, entry) == {
        device_id: [] for device_id in FIXTURE_DEVICE_IDS
    }

    mock_api.device_rows_override = None
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_SCAN_INTERVAL + 1)
    )
    await hass.async_block_till_done()

    per_bowl = _entity_ids_per_bowl(entity_registry, entry)
    for device_id in FIXTURE_DEVICE_IDS:
        assert len(per_bowl[device_id]) == len(PER_BOWL_DESCRIPTIONS), device_id


async def test_a_bowl_is_added_once_however_many_polls_run(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """Later polls must not re-add a bowl that already has its entities.

    Without this, "add whatever the poll found" passes the test above and
    then raises `Entity id already exists` on the very next poll, which
    aborts the platform's add task and leaves a warning in every log.
    """
    entry = await setup_platforms(hass, list(PLATFORMS))
    before = len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))

    for poll in range(1, 4):
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=(DEFAULT_SCAN_INTERVAL + 1) * poll)
        )
        await hass.async_block_till_done()

    assert len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == before

