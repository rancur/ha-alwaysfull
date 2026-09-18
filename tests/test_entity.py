"""Base entity identity, device info, availability, and the partial-write guard."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityDescription
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.alwaysfull import PLATFORMS
from custom_components.alwaysfull.const import DOMAIN, RELOGIN_MAX_ATTEMPTS, SIGN_SECRET
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
from custom_components.alwaysfull.models import device_label
from custom_components.alwaysfull.switch import NOTIFY_SWITCHES

from .conftest import (
    DEVICE_ID,
    SECOND_DEVICE_ID,
    FakeAlwaysFullClient,
    load_fixture_data,
    setup_platforms,
    vendor_error,
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

    # A session refused twice is not one of the cases measurement has
    # settled, so the message carries the vendor's words and claims
    # nothing about what the bowl did with the write.
    assert "token expiration" in str(err.value)
    assert "not known" in str(err.value)
    assert len(mock_api.login_calls) == 1
    assert len(mock_api.writes) == 2


async def test_a_write_answered_652_recovers_instead_of_reauthing(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A `652` on a WRITE endpoint is a dead session, and heals like one.

    The write path shares the poll path's recovery, so it shares this fix
    too -- but it shares it through a different method on a different
    base, which is exactly where a fix applied in one place stops. The
    error is built by the real client from a real `652` envelope, so this
    is asserting the classification and the recovery together.
    """
    entity = await _write_entity(hass)
    mock_api.fail_writes(await vendor_error("652"), times=1)

    await entity.async_write_config(FLUSH_GROUP, lambda _config: None)

    assert mock_api.login_calls == [ACCOUNT_EMAIL]
    assert [method for method, _payload in mock_api.writes] == [
        "set_flush_config",
        "set_flush_config",
    ]
    await hass.async_block_till_done()
    assert _reauth_flows(hass) == []


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
    # Refused at the gate, so this is the one class where the user is told
    # outright that their setting is untouched.
    assert "Nothing was changed" in str(err.value)
    assert mock_api.login_calls == []
    assert len(mock_api.writes) == 1

    await hass.async_block_till_done()
    assert _reauth_flows(hass) == []


async def test_a_write_shares_the_polls_relogin_budget(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The budget is per ACCOUNT, so a write cannot spend past what polls used.

    The vendor counts requests per account, and this integration has one
    account and two request paths. A budget the write path did not respect
    would be no budget at all: the login storm would simply come out of
    the other door.

    Past the budget the write gives the user a readable error -- NOT a
    reauth prompt, because nothing here says the credentials are wrong,
    and not a wait either.
    """
    entity = await _write_entity(hass)
    for _ in range(RELOGIN_MAX_ATTEMPTS):
        await entity.coordinator.async_relogin()
    assert len(mock_api.login_calls) == RELOGIN_MAX_ATTEMPTS

    mock_api.fail_writes(AlwaysFullAuthError("token expiration"), times=1)

    with pytest.raises(HomeAssistantError) as err:
        await entity.async_write_config(FLUSH_GROUP, lambda _config: None)

    assert not isinstance(err.value, ConfigEntryAuthFailed)
    assert "not signing in again just yet" in str(err.value)
    assert len(mock_api.login_calls) == RELOGIN_MAX_ATTEMPTS
    # Attempted once and not retried: the retry is what the re-login was
    # for, and the re-login did not happen.
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


# -- What a failed write TELLS the user ------------------------------------
#
# A live install turned a switch off, the write failed, and the user was
# shown "Always Full could not apply the change: The setup failed." --
# the vendor's own `msg`, passed through with nothing added. It says
# nothing about what state their bowl is in, what to do next, or what to
# put in a bug report, and the vendor's envelope CODE never reached them
# at all.
#
# The correction is not "say it failed more clearly". It is that we do not
# know that it failed. Measured on the live bowl on 2026-09-17 and written
# up in `docs/VENDOR-API.md`: a `flushConfig` write answered "The setup
# failed." and the vendor's stored `fillWashState` moved to the requested
# value seven seconds later and held. THE ERROR CAME BACK AND THE CHANGE
# LANDED ANYWAY.
#
# So these tests pin the distinction that matters, and pin it in both
# directions, because a message that hedges everything is as useless as one
# that claims everything:
#
# | Failure                       | What the user is told                |
# | ----------------------------- | ------------------------------------ |
# | rate limit (603 / HTTP 429)   | nothing was changed -- DEFINITE      |
# | timeout, connection error     | outcome not known                    |
# | any other vendor code + msg   | outcome not known                    |
#
# The rate limit is the only one that is definite, and it is definite
# because the request was refused before it reached the bowl. Everything
# else either never got an answer or got an answer that has been PROVED not
# to mean what it says.


async def _failed_write_message(
    entity: AlwaysFullWriteEntity,
    mock_api: FakeAlwaysFullClient,
    error: Exception,
) -> str:
    """Fail every write with `error` and return what the user is shown."""
    mock_api.fail_writes(error)
    with pytest.raises(HomeAssistantError) as err:
        await entity.async_write_config(FLUSH_GROUP, lambda _config: None)
    return str(err.value)


async def test_a_rate_limited_write_says_plainly_that_nothing_changed(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """603 is refused at the gate, so this is the one case we can be definite about.

    The vendor's rate limit is answered before the request is processed --
    it is the documented meaning of the code, and it is the only failure
    class here where "your setting is untouched" is something we actually
    know. Hedging it would be as wrong as claiming it elsewhere.
    """
    entity = await _write_entity(hass)

    message = await _failed_write_message(
        entity, mock_api, await vendor_error("603", msg="Too many requests, please try again later.")
    )

    assert "Nothing was changed" in message
    # The code and the vendor's own words, so a bug report carries both.
    assert "603" in message
    assert "Too many requests, please try again later." in message
    assert "try again" in message
    # NOT hedged: this is the case we can be definite about.
    assert "not known" not in message


async def test_a_connection_failure_does_not_claim_the_change_failed(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """We never saw an answer, so the request may well have been delivered."""
    entity = await _write_entity(hass)

    message = await _failed_write_message(
        entity, mock_api, aiohttp.ClientConnectionError("Cannot connect to host")
    )

    assert "not known" in message
    assert "Nothing was changed" not in message
    assert "Cannot connect to host" in message
    assert "safe" in message


async def test_a_timed_out_write_does_not_claim_the_change_failed(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A timeout is the strongest case of all for "unknown".

    The request was sent and no answer came back; that is precisely the
    shape of a write the vendor processed and did not get to report on.
    `TimeoutError()` carries no message at all, so the detail has to be
    supplied rather than interpolated from an empty string.
    """
    entity = await _write_entity(hass)

    message = await _failed_write_message(entity, mock_api, TimeoutError())

    assert "not known" in message
    assert "timed out" in message
    assert "Nothing was changed" not in message


async def test_an_arbitrary_vendor_error_carries_its_code_and_its_own_words(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """THE REPORTED CASE, verbatim: "The setup failed." under an unknown code.

    The error is built by the REAL client from a real envelope, so this
    asserts the whole chain: the code survives `api._handle_response`, it
    reaches the exception, and it reaches the person reading the toast.
    A message built from `str(err)` alone cannot pass this.
    """
    entity = await _write_entity(hass)

    message = await _failed_write_message(
        entity, mock_api, await vendor_error("500", msg="The setup failed.")
    )

    assert "The setup failed." in message
    assert "500" in message
    # Neither claim is available to us, and the second one is the one the
    # live bowl disproved.
    assert "not known" in message
    assert "Nothing was changed" not in message
    # Whole-object writes are idempotent, so a repeat cannot compound this.
    assert "safe" in message


async def test_no_write_failure_message_carries_a_secret(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Every class of failure, checked for the four things that must never leak.

    The device id is the bowl's MAC address. It is checked here because the
    obvious way to write these messages -- naming the bowl -- is also the
    way it gets published, and `device_label` exists precisely so a message
    CAN name the bowl. So the label is asserted present as well: a message
    that named nothing would pass the negative half of this test while
    being useless with two bowls on the account.
    """
    entity = await _write_entity(hass)
    errors: list[Exception] = [
        await vendor_error("603", msg="Too many requests, please try again later."),
        await vendor_error("500", msg="The setup failed."),
        aiohttp.ClientConnectionError("Cannot connect to host"),
        TimeoutError(),
    ]

    for error in errors:
        message = await _failed_write_message(entity, mock_api, error)

        assert DEVICE_ID not in message
        assert SIGN_SECRET not in message
        assert "FRESH-TOKEN" not in message
        assert "password" not in message.lower()
        assert device_label(DEVICE_ID) in message


async def test_a_failed_write_re_reads_the_bowl_instead_of_trusting_the_cache(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, freezer: FrozenDateTimeFactory
) -> None:
    """A write that LOOKS failed may have applied, so the cache stops being trusted.

    This is the behavioural half of the same finding. Home Assistant is
    showing a value it can no longer vouch for -- the device may have taken
    the change the error appeared to refuse -- so the cached value must be
    reconciled against the device rather than left standing until the next
    scheduled poll, up to a whole interval away.

    Eleven seconds, and the poll interval is sixty: long enough for the
    request-refresh debouncer to fire, far short of the scheduled poll. So
    the extra read can only have come from the failure.
    """
    entity = await _write_entity(hass)
    mock_api.fail_writes(await vendor_error("500", msg="The setup failed."))
    before = mock_api.device_list_calls

    with pytest.raises(HomeAssistantError):
        await entity.async_write_config(FLUSH_GROUP, lambda _config: None)

    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert mock_api.device_list_calls == before + 1


async def test_a_failed_write_still_does_not_publish_what_the_user_asked_for(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Re-reading is NOT the same as applying it optimistically.

    "The outcome is unknown" cuts both ways: we may not claim the change
    was refused, and we equally may not claim it was taken. The cache keeps
    the last value the vendor actually reported until a poll replaces it.
    """
    entity = await _write_entity(hass)
    coordinator = entity.coordinator
    before = coordinator.data[DEVICE_ID].config.flush_interval_minutes
    mock_api.fail_writes(await vendor_error("500", msg="The setup failed."))

    def _mutate(config: Any) -> None:
        config.flush_interval_minutes = before + 5

    with pytest.raises(HomeAssistantError):
        await entity.async_write_config(FLUSH_GROUP, _mutate)

    assert coordinator.data[DEVICE_ID].config.flush_interval_minutes == before


async def test_the_account_write_path_reports_a_failure_the_same_way(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """`notify/saveConfig` is a write too, and it fails the same way.

    A separate method on a separate base: a message fixed only on
    `AlwaysFullWriteEntity` would leave the notification switches saying
    "The setup failed." and nothing else, which is where this started.
    """
    entry = await _load(hass)
    entity = AlwaysFullAccountEntity(entry.runtime_data, DESCRIPTION)
    mock_api.fail_writes(await vendor_error("500", msg="The setup failed."))
    before = mock_api.notify_config_calls

    with pytest.raises(HomeAssistantError) as err:
        await entity.async_save_notify_config(lambda _config: None)

    # Reconciled against the NOTIFICATION object, which is the one a failed
    # save may have changed. A device poll would re-read every bowl and
    # leave this switch on its pre-write value for up to ten poll
    # intervals, because `notify/getConfig` is fetched once every ten.
    assert mock_api.notify_config_calls == before + 1

    message = str(err.value)
    assert "The setup failed." in message
    assert "500" in message
    assert "not known" in message
    assert "Nothing was changed" not in message
    # Named for what it is: there is no bowl to name on this path.
    assert "notification settings" in message
