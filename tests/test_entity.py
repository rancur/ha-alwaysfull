"""Base entity identity, device info, availability, and the partial-write guard."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityDescription
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alwaysfull import PLATFORMS
from custom_components.alwaysfull.const import DOMAIN
from custom_components.alwaysfull.entity import (
    FILTER_GROUP,
    FLUSH_GROUP,
    LOG_GROUP,
    MAINTENANCE_GROUP,
    SLEEP_GROUP,
    WATER_GROUP,
    AlwaysFullAccountEntity,
    AlwaysFullEntity,
    AlwaysFullWriteEntity,
    ConfigGroup,
)
from custom_components.alwaysfull.exceptions import (
    AlwaysFullAuthError,
    AlwaysFullCredentialsError,
    AlwaysFullRateLimitError,
)
from custom_components.alwaysfull.switch import NOTIFY_SWITCHES

from .conftest import (
    DEVICE_ID,
    SECOND_DEVICE_ID,
    FakeAlwaysFullClient,
    load_fixture_data,
    setup_platforms,
)

DESCRIPTION = EntityDescription(key="filter_life")

# The synthetic account every test here runs as. Its local part and its
# domain are asserted against separately, because a fallback name built
# from the entry title would carry them in whatever spelling Home
# Assistant's slugify produced.
ACCOUNT_EMAIL = "user@example.com"
ACCOUNT_PARTS = ("user", "example", "@")


async def _load(hass: HomeAssistant) -> MockConfigEntry:
    """Load one config entry against whatever the fake client is set up to serve.

    Titled with the address, because that is what the config flow does
    (`async_create_entry(title=email)`) and because the entry title is
    precisely what Home Assistant falls back to when a `DeviceInfo` omits
    its name. A test entry left on `MockConfigEntry`'s "Mock Title" cannot
    see that fallback for what it is.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=ACCOUNT_EMAIL,
        unique_id=ACCOUNT_EMAIL,
        data={"email": ACCOUNT_EMAIL, "password": "pw", "token": "T"},
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


def _unnamed_rows() -> list[dict[str, Any]]:
    """Return the committed device rows with the vendor's name removed.

    The DEFAULT state of a bowl nobody renamed in the vendor app, not an
    edge case: `deviceName` is null until somebody types one in.
    """
    rows: list[dict[str, Any]] = load_fixture_data("device_list_multi")["data"]
    for row in rows:
        row["deviceName"] = None
    return rows


async def test_device_info_always_carries_a_name_when_the_vendor_has_none(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A bowl with no vendor name must still name itself, and not after the account.

    Omitting `name` from `DeviceInfo` is not neutral: Home Assistant falls
    back to the config entry title, and this integration titles the entry
    with the account's EMAIL ADDRESS. So the common case -- a bowl nobody
    renamed in the vendor app -- put the owner's address into the device
    name and therefore into every per-bowl entity id, where it shows in the
    UI, in automations and in any screenshot.
    """
    mock_api.device_rows_override = _unnamed_rows()
    entity = await _entity(hass)

    name = entity.device_info["name"]
    assert name

    for part in ACCOUNT_PARTS:
        assert part not in name.lower(), name
    # And not the entry title by another route: the title IS the address.
    assert name != entity.coordinator.config_entry.title


async def test_two_unnamed_bowls_get_distinct_names(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """An owner with two unnamed bowls must be able to tell them apart.

    A single constant fallback would satisfy "there is a name" and leave
    both bowls, and both sets of entity ids, indistinguishable -- with the
    second one's ids silently suffixed `_2` in registration order.
    """
    mock_api.device_rows_override = _unnamed_rows()
    entry = await _load(hass)

    first = AlwaysFullEntity(entry.runtime_data, DEVICE_ID, DESCRIPTION)
    second = AlwaysFullEntity(entry.runtime_data, SECOND_DEVICE_ID, DESCRIPTION)

    assert first.device_info["name"] != second.device_info["name"]


async def test_device_info_names_a_bowl_the_poll_no_longer_knows(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The name must not depend on the bowl still being in the last poll.

    `device_info` returns early when the bowl has gone, and that early
    return is on the same fallback path: it must not be the one place that
    hands Home Assistant a nameless device and gets the account title back.
    """
    entity = await _entity(hass)
    gone = AlwaysFullEntity(entity.coordinator, "ffeeddccbbaa", DESCRIPTION)

    name = gone.device_info["name"]
    assert name
    for part in ACCOUNT_PARTS:
        assert part not in name.lower(), name


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


async def test_no_entity_id_carries_the_account_address(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """Not one entity id this integration creates may carry the account.

    The registry-level companion to the unique-id and device-identifier
    guard in `test_switch.py`, and it covers the thing that one could not:
    an entity id is derived from the DEVICE NAME, so a `DeviceInfo` that
    omits its name publishes the config entry title -- which is the
    address -- into every per-bowl entity id, slugified past any search
    for an `@`.

    Run across every platform, with the vendor's `deviceName` removed:
    that is the state the fault needs, and it is the state no committed
    fixture bowl is in.
    """
    mock_api.device_rows_override = _unnamed_rows()

    entry = await setup_platforms(hass, list(PLATFORMS))
    registered = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    # Asserted to exist, so this cannot pass by finding nothing.
    assert len(registered) > len(NOTIFY_SWITCHES)

    local_part, _, domain = ACCOUNT_EMAIL.partition("@")
    for entity in registered:
        for part in ("@", ACCOUNT_EMAIL, local_part, domain, domain.replace(".", "_")):
            assert part not in entity.entity_id, entity.entity_id


# -- Single-session recovery on the WRITE path -----------------------------
#
# The vendor allows ONE active session per account: logging in again
# invalidates the previous token immediately (verified against the live
# server -- token A answers 200, a second login mints token B, and token A
# then answers 651 "token expiration" while token B answers 200). So a
# rejected token mid-session is not a rare days-later expiry; it is what
# happens every time the owner opens the phone app while Home Assistant is
# running, which is an ordinary thing to do with your own bowl.
#
# The poll path has always handled it: one silent re-login, then reauth
# only if that fails too. The write path did not, so polls self-healed and
# writes reported "Always Full could not apply the change: token
# expiration" with nothing the user could do about it. These tests pin the
# two paths to the same behaviour.


def _reauth_flows(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return the reauth flows this integration currently has in progress."""
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"].get("source") == SOURCE_REAUTH
    ]


async def test_write_recovers_from_an_expired_token_with_one_re_login(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A token invalidated elsewhere costs one silent re-login, not an error.

    `times=1` is the live shape: the first attempt carries the token the
    phone app just invalidated, the re-login mints a new one, and the
    retry goes through. The user sees their setting change.
    """
    entity = await _write_entity(hass)
    mock_api.fail_writes(AlwaysFullAuthError("token expiration"), times=1)

    await entity.async_write_config(FLUSH_GROUP, lambda _config: None)

    # Exactly one login, asserted as a COUNT: "it succeeded" is also true
    # of an unbounded retry loop that happened to stop after two.
    assert mock_api.login_calls == [ACCOUNT_EMAIL]
    assert mock_api.token == "FRESH-TOKEN"
    # Attempted, refused, retried -- and the retry really was a write.
    assert [method for method, _payload in mock_api.writes] == [
        "set_flush_config",
        "set_flush_config",
    ]
    assert _reauth_flows(hass) == []


async def test_write_that_fails_auth_twice_surfaces_a_readable_error(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """One re-login and no more, even when the retry is refused as well.

    An unbounded retry against a single-session vendor is how two Home
    Assistant instances -- or Home Assistant and the phone app -- log each
    other out in a loop, so the login count is the assertion that matters
    here, not the error.
    """
    entity = await _write_entity(hass)
    mock_api.fail_writes(AlwaysFullAuthError("token expiration"))

    with pytest.raises(HomeAssistantError) as err:
        await entity.async_write_config(FLUSH_GROUP, lambda _config: None)

    assert "could not apply the change" in str(err.value)
    assert len(mock_api.login_calls) == 1
    assert len(mock_api.writes) == 2


async def test_write_rejected_credentials_never_retries_and_reauths(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A wrong stored password goes straight to reauth, with NO login attempt.

    Re-sending a pair the server has just rejected cannot succeed, so the
    retry would only spend a vendor request to fail the same way. Routing
    to Home Assistant's reauth prompt is what gives the user somewhere to
    go; a bare error would be a dead end.

    `login_calls == []` is the assertion that kills a swapped except
    ordering: with the base class catching first, this error would take
    the re-login branch, and the reauth would still happen on the retry.
    """
    entity = await _write_entity(hass)
    mock_api.fail_writes(AlwaysFullCredentialsError("Invalid email address or password."))

    with pytest.raises(ConfigEntryAuthFailed):
        await entity.async_write_config(FLUSH_GROUP, lambda _config: None)

    assert mock_api.login_calls == []
    assert len(mock_api.writes) == 1

    await hass.async_block_till_done()
    assert len(_reauth_flows(hass)) == 1


async def test_write_rate_limit_is_never_an_auth_failure(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """429 means slow down. Re-logging in makes it worse; reauth traps the user.

    The credentials are fine, so a reauth prompt would succeed and the next
    write would be rate-limited again, with no way out of the loop.
    """
    entity = await _write_entity(hass)
    mock_api.fail_writes(AlwaysFullRateLimitError("Server responded 429 Too Many Requests"))

    with pytest.raises(HomeAssistantError) as err:
        await entity.async_write_config(FLUSH_GROUP, lambda _config: None)

    assert not isinstance(err.value, ConfigEntryAuthFailed)
    assert "could not apply the change" in str(err.value)
    assert mock_api.login_calls == []
    assert len(mock_api.writes) == 1

    await hass.async_block_till_done()
    assert _reauth_flows(hass) == []


async def test_the_account_write_path_recovers_the_same_way(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """`notify/saveConfig` is a write too, and takes the same recovery.

    It is a separate method on a separate base -- the account settings are
    not per-bowl -- so a fix applied only to `AlwaysFullWriteEntity` would
    leave the notification switches failing on exactly the same token.
    """
    entry = await _load(hass)
    entity = AlwaysFullAccountEntity(entry.runtime_data, DESCRIPTION)
    mock_api.fail_writes(AlwaysFullAuthError("token expiration"), times=1)

    await entity.async_save_notify_config(lambda _config: None)

    assert mock_api.login_calls == [ACCOUNT_EMAIL]
    assert [method for method, _payload in mock_api.writes] == [
        "save_notify_config",
        "save_notify_config",
    ]
