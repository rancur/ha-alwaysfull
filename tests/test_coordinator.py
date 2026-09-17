"""Coordinator polling shape, error mapping and post-write refresh."""

from __future__ import annotations

import dataclasses
import logging

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alwaysfull.const import (
    DOMAIN,
    EMPTY_DEVICE_LIST_EVERY_N_POLLS,
    NOTIFY_CONFIG_EVERY_N_POLLS,
)
from custom_components.alwaysfull.coordinator import (
    AlwaysFullCoordinator,
    scan_interval_seconds,
)
from custom_components.alwaysfull.exceptions import (
    AlwaysFullAuthError,
    AlwaysFullCredentialsError,
    AlwaysFullError,
    AlwaysFullRateLimitError,
)

from .conftest import (
    DEVICE_ID,
    FROZEN_INSTANT,
    FROZEN_LOCAL_DATE,
    FROZEN_UTC_DATE,
    FROZEN_ZONE,
    SECOND_DEVICE_ID,
    FakeAlwaysFullClient,
    zone_unlike_host,
)


async def _setup(hass: HomeAssistant, token: str = "T") -> AlwaysFullCoordinator:
    """Load an entry and hand back its coordinator.

    `token` is a parameter so a test that asserts a secret never reaches the
    log can use a value long and distinctive enough for the assertion to
    mean something. The default "T" is one character, and "this string is
    absent" is a claim no one-character needle can support.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": token},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry.runtime_data


# Long and distinctive on purpose -- see `_setup`.
SECRET_TOKEN = "TOKEN-THAT-MUST-NEVER-BE-LOGGED"


def _our_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the warnings THIS integration logged, and nobody else's.

    Filtered by logger name, not just by level: Home Assistant emits its own
    WARNING for every custom integration it loads ("has not been tested by
    Home Assistant..."), so a bare level filter counts a line this code did
    not write and would make a "logged exactly once" assertion pass or fail
    for reasons that have nothing to do with the integration.
    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == "custom_components.alwaysfull"
    ]

async def test_one_device_list_call_plus_per_device_reads(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """One list call carries full state; details are never re-fetched per device."""
    coordinator = await _setup(hass)

    assert mock_api.device_list_calls == 1

    bowl = coordinator.data[DEVICE_ID]
    assert bowl.state.device_name == "Test Bowl"
    assert bowl.state.bowl_size_inches == 9
    assert bowl.config.flush_interval_minutes == 60
    assert bowl.water_today == 903
    assert bowl.firmware_version == "3.6.2"
    assert len(bowl.notifications) == 13


async def test_every_device_in_the_list_is_polled(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A second bowl must not be silently dropped.

    With a single-device fixture this assertion would hold just as well for
    an implementation that only ever processed `rows[0]`, and a two-bowl
    household would get no entities for the second bowl with the suite
    still green.
    """
    coordinator = await _setup(hass)

    assert mock_api.device_config_calls == [DEVICE_ID, SECOND_DEVICE_ID]
    assert mock_api.notify_log_calls == [DEVICE_ID, SECOND_DEVICE_ID]
    assert [call[0] for call in mock_api.drinking_log_calls] == [DEVICE_ID, SECOND_DEVICE_ID]
    assert set(coordinator.data) == {DEVICE_ID, SECOND_DEVICE_ID}

    second = coordinator.data[SECOND_DEVICE_ID]
    assert second.state.device_name == "Second Bowl"
    # Per-device values must come from THAT device's row/reads, never the
    # first bowl's: size, units, firmware and today's water all differ.
    assert second.state.bowl_size_inches == 7
    assert second.state.units == 2
    assert second.firmware_version == "3.6.1"
    assert second.water_today == 250
    assert second.config.device_id == SECOND_DEVICE_ID
    # The drinking log is requested in each bowl's OWN units.
    assert [call[1] for call in mock_api.drinking_log_calls] == [1, 2]


async def test_device_row_without_an_id_is_skipped(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A half-provisioned row must be skipped, not crash the whole poll.

    The fixture's third row has `deviceId: null`. Keying `data` on it would
    collide every such row onto one entry, and passing `None` into
    `device_config()` would fail the update for the healthy bowls too.
    """
    coordinator = await _setup(hass)

    assert coordinator.last_update_success is True
    assert len(coordinator.data) == 2
    assert None not in coordinator.data
    assert None not in mock_api.device_config_calls


async def test_drinking_log_is_snapped_to_today_in_ha_time_zone(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, freezer: FrozenDateTimeFactory
) -> None:
    """Today's log window is a single local day, in HA's zone, not UTC's.

    Time is frozen at an instant whose local date in the Home Assistant
    zone (the 17th in Tokyo) differs from its UTC date (the 16th). Under
    freezegun the naive clock reads the frozen UTC wall time on every host,
    so BOTH the "reads UTC" and "reads the OS clock" implementations produce
    the 16th and fail here -- on any runner, including one in Tokyo.
    """
    freezer.move_to(FROZEN_INSTANT)
    await hass.config.async_set_time_zone(FROZEN_ZONE)
    await _setup(hass)

    assert FROZEN_LOCAL_DATE != FROZEN_UTC_DATE
    device_id, units, start, end = mock_api.drinking_log_calls[0]
    assert (device_id, units) == (DEVICE_ID, 1)
    assert start == FROZEN_LOCAL_DATE
    assert end == FROZEN_LOCAL_DATE


async def test_time_zone_offset_is_resynced_every_poll(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A DST transition must not need a Home Assistant restart to take effect.

    The client is built once at setup, so if the coordinator did not push
    the current offset before each poll, the integration would keep signing
    requests with the offset that was correct the day HA last started.
    """
    zone, expected_offset = zone_unlike_host()
    await hass.config.async_set_time_zone(zone)
    coordinator = await _setup(hass)

    mock_api.tz_offset_hours = 999
    await coordinator.async_refresh()

    assert mock_api.tz_offset_hours == expected_offset


async def test_notify_config_polled_only_once_every_n_polls(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The account-level notify config is rarely fetched, not fetched per poll."""
    coordinator = await _setup(hass)
    assert mock_api.notify_config_calls == 1

    for _ in range(NOTIFY_CONFIG_EVERY_N_POLLS - 1):
        await coordinator.async_refresh()
    assert mock_api.notify_config_calls == 1

    await coordinator.async_refresh()
    assert mock_api.notify_config_calls == 2
    assert coordinator.notify_config is not None


async def test_auth_error_retries_login_exactly_once(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A stale token is refreshed silently; the user sees nothing."""
    coordinator = await _setup(hass)
    mock_api.fail_device_list(AlwaysFullAuthError("Token expired"), times=1)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert mock_api.login_calls == ["user@example.com"]
    assert mock_api.token == "FRESH-TOKEN"


async def test_second_auth_failure_raises_config_entry_auth_failed(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """If the re-login is rejected too, hand off to HA's reauth flow."""
    coordinator = await _setup(hass)
    mock_api.fail_device_list(AlwaysFullAuthError("Token expired"))
    mock_api.login_error = AlwaysFullAuthError("Account or password error")

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert len(mock_api.login_calls) == 1


async def test_rejected_credentials_skip_the_retry_entirely(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Wrong stored credentials go straight to reauth, with NO login attempt.

    The server has already said this email/password pair is wrong, so the
    silent re-login cannot succeed. Attempting it anyway would spend one
    extra vendor request on every poll for as long as the entry is broken,
    which is why `login_calls` being empty is the assertion that matters
    here -- `ConfigEntryAuthFailed` alone is true of the retry path too.
    """
    coordinator = await _setup(hass)
    mock_api.fail_device_list(AlwaysFullCredentialsError("Invalid email address or password."))

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert mock_api.login_calls == []


async def test_expired_token_still_gets_exactly_one_retry(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The base auth error keeps its one silent re-login -- and only one.

    Guards the other side of the split: catching the subclass first must not
    turn the token-expiry path into a no-retry path. The re-login here fails,
    so this counts attempts on the road to reauth rather than on the happy
    path covered by `test_auth_error_retries_login_exactly_once`.
    """
    coordinator = await _setup(hass)
    mock_api.fail_device_list(AlwaysFullAuthError("Token expired"))
    mock_api.login_error = AlwaysFullAuthError("Token expired")

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert mock_api.login_calls == ["user@example.com"]


async def test_relogin_answered_with_rejected_credentials_reaches_reauth(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The realistic production sequence: 651 mid-session, then 652 on re-login.

    The token expires (worth one retry), and the re-login comes back
    "invalid email address or password". That subclass is raised from inside
    the retry handler, so it cannot be caught by that handler's siblings --
    it has to land on the outer clause and become reauth, not leak out as a
    plain update failure.
    """
    coordinator = await _setup(hass)
    mock_api.fail_device_list(AlwaysFullAuthError("Token expired"))
    mock_api.login_error = AlwaysFullCredentialsError("Invalid email address or password.")

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert mock_api.login_calls == ["user@example.com"]


async def test_rate_limit_is_never_mapped_to_auth_failure(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """429 means slow down, not "bad credentials" -- mapping it to reauth traps the user."""
    coordinator = await _setup(hass)
    mock_api.fail_device_list(AlwaysFullRateLimitError("429"))

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert not isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert mock_api.login_calls == []


@pytest.mark.parametrize(
    "error",
    [
        # `asyncio.TimeoutError` IS the builtin on 3.11+, so one entry covers both.
        TimeoutError(),
        aiohttp.ClientError("boom"),
        ValueError("Expecting value: line 1 column 1 (char 0)"),
        AlwaysFullError("Unexpected response code 500"),
    ],
)
async def test_transport_errors_map_to_update_failed(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient, error: Exception
) -> None:
    """Timeouts, client errors and malformed JSON degrade, never reauth."""
    coordinator = await _setup(hass)
    mock_api.fail_device_list(error)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)


@pytest.mark.parametrize(
    ("options", "data_extra", "expected"),
    [
        # Nothing configured at all -> the documented default.
        ({}, {}, 60),
        # Below the floor / above the ceiling -> clamped to the bounds.
        ({"scan_interval": 5}, {}, 30),
        ({"scan_interval": 700}, {}, 600),
        # Exactly on the bounds, and inside them -> used verbatim. Without
        # these, "always return DEFAULT_SCAN_INTERVAL" would pass.
        ({"scan_interval": 30}, {}, 30),
        ({"scan_interval": 600}, {}, 600),
        ({"scan_interval": 45}, {}, 45),
        # Junk that a hand-edited .storage file could contain.
        ({"scan_interval": "not a number"}, {}, 60),
        ({"scan_interval": None}, {}, 60),
        # An interval that only ever reached entry.data (no options flow run).
        ({}, {"scan_interval": 700}, 600),
        # Options win over data when both are present.
        ({"scan_interval": 45}, {"scan_interval": 600}, 45),
    ],
)
def test_scan_interval_is_clamped_into_the_supported_band(
    options: dict[str, object], data_extra: dict[str, object], expected: int
) -> None:
    """Every branch of the clamp, with literal expectations.

    The values asserted are literals, not the constants under test, so
    moving `DEFAULT_SCAN_INTERVAL` or dropping either bound fails here
    rather than quietly redefining what "correct" means.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": "T", **data_extra},
        options=options,
    )

    assert scan_interval_seconds(entry) == expected


async def test_scan_interval_reaches_the_coordinator_poll_timer(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """The clamped value is what the coordinator actually polls on."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw", "token": "T"},
        options={"scan_interval": 5},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.update_interval.total_seconds() == 30


async def test_a_written_config_is_cached_at_once_and_only_for_that_bowl(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """A successful write updates the cache without asking the vendor anything.

    The vendor is EVENTUALLY CONSISTENT: `/app/device/config` served
    immediately after a write returns the PRE-write object for around
    twenty seconds. So the value the user asked for is applied from the
    write itself, and nothing is re-read here -- a re-read is how the
    setting used to visibly snap back.

    Both halves are asserted, because "it stopped re-reading" and "it
    applied the value" are different bugs: the count of config reads must
    not move, and the other bowl must not be touched.
    """
    coordinator = await _setup(hass)
    assert coordinator.data[DEVICE_ID].config.flush_interval_minutes == 60
    before = list(mock_api.device_config_calls)

    written = dataclasses.replace(coordinator.data[DEVICE_ID].config)
    written.flush_interval_minutes = 30
    await coordinator.async_refresh_after_write(DEVICE_ID, written)

    assert coordinator.data[DEVICE_ID].config.flush_interval_minutes == 30
    assert coordinator.data[SECOND_DEVICE_ID].config.flush_interval_minutes == 60
    assert mock_api.device_config_calls == before


async def test_a_later_poll_overrides_a_written_value_the_vendor_did_not_apply(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """Reality wins. A write the vendor accepted but ignored must surface.

    This is the other half of the optimistic update, and the one that
    stops it becoming a lie: the cache shows what was asked for until a
    real poll disagrees, and then the poll's value is what stands -- even
    though it is the OLD one.
    """
    coordinator = await _setup(hass)

    written = dataclasses.replace(coordinator.data[DEVICE_ID].config)
    written.flush_interval_minutes = 30
    await coordinator.async_refresh_after_write(DEVICE_ID, written)
    assert coordinator.data[DEVICE_ID].config.flush_interval_minutes == 30

    # The vendor never applied it and still serves the pre-write object.
    await coordinator.async_refresh()

    assert coordinator.data[DEVICE_ID].config.flush_interval_minutes == 60


async def test_a_write_with_no_known_config_requests_a_full_poll(
    hass: HomeAssistant, mock_api: FakeAlwaysFullClient
) -> None:
    """State a write changed OUTSIDE the config object still gets picked up.

    `set_units`, `set_device_type` and `reset/filter` change the DEVICE
    ROW, not the config object, so there is no written config to apply and
    a real poll is the only thing that can show the result.
    """
    coordinator = await _setup(hass)
    before = mock_api.device_list_calls

    await coordinator.async_refresh_after_write(DEVICE_ID)
    await hass.async_block_till_done()

    assert mock_api.device_list_calls == before + 1


# -- `_notifications_for`, directly ---------------------------------------
#
# Its only other coverage is a full two-bowl integration test, which
# exercises it through four other layers. These call it directly so that a
# failure names the filter rather than the platform that noticed.


def test_notifications_for_keeps_only_this_devices_rows():
    """Rows naming another bowl are dropped; this bowl's are kept in order."""
    rows = [
        {"id": 1, "deviceId": DEVICE_ID, "type": "Tilted"},
        {"id": 2, "deviceId": SECOND_DEVICE_ID, "type": "Fill_Failed"},
        {"id": 3, "deviceId": DEVICE_ID, "type": "Hardware_Fault"},
    ]

    kept = AlwaysFullCoordinator._notifications_for(DEVICE_ID, rows)

    assert [row["id"] for row in kept] == [1, 3]


def test_notifications_for_keeps_a_row_with_no_device_id():
    """An absent `deviceId` is not evidence the row belongs elsewhere.

    We asked this endpoint for this device. Discarding an unlabelled row
    would lose a real alert; keeping it costs nothing on a per-device feed.
    """
    rows = [{"id": 1, "deviceId": None}, {"id": 2}, {"id": 3, "deviceId": SECOND_DEVICE_ID}]

    kept = AlwaysFullCoordinator._notifications_for(DEVICE_ID, rows)

    assert [row["id"] for row in kept] == [1, 2]


def test_notifications_for_keeps_a_malformed_row():
    """A non-dict row cannot be tested for a device id, so it survives.

    Dropping it here would hide it from the entity layer, which is where
    the decision about malformed JSON belongs -- and where it is already
    made.
    """
    rows = ["not a row", {"id": 1, "deviceId": SECOND_DEVICE_ID}]

    kept = AlwaysFullCoordinator._notifications_for(DEVICE_ID, rows)

    assert kept == ["not a row"]


def test_notifications_for_drops_everything_when_none_match():
    """The empty result is a real result, not a fall-through to the input."""
    rows = [{"id": 1, "deviceId": SECOND_DEVICE_ID}, {"id": 2, "deviceId": SECOND_DEVICE_ID}]

    assert AlwaysFullCoordinator._notifications_for(DEVICE_ID, rows) == []


async def test_an_empty_device_list_is_warned_about_without_flooding_the_log(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A poll that finds no bowls must say so, once, in plain words.

    This is the diagnosability half of the empty-device-list fault. An
    empty list is a SUCCESSFUL poll: the entry reports `loaded`,
    `last_update_success` is true and, before this, the log said nothing at
    all -- so from the outside a working integration and one that had
    silently created no bowl entities were indistinguishable. Dynamic
    entity addition fixed the user-visible symptom; it did not, on its own,
    make the next empty list diagnosable.

    WARNING rather than DEBUG because it is always worth reading for an
    account that is supposed to have a bowl, and because `home-assistant.log`
    records WARNING without anyone opting in -- which is where a person
    looks first.

    Deliberately NOT once per poll. At the default sixty-second interval
    that is 1,440 identical lines a day, which is how a real warning gets
    scrolled past. It fires on the TRANSITION to zero and then only
    occasionally while the condition persists.
    """
    mock_api.device_rows_override = []

    with caplog.at_level(logging.WARNING, logger="custom_components.alwaysfull"):
        coordinator = await _setup(hass, token=SECRET_TOKEN)
        assert coordinator.data == {}

        warnings = _our_warnings(caplog)
        assert len(warnings) == 1, warnings

        message = warnings[0]
        # Plain words, and specific about what it means: authentication
        # worked, the vendor returned nothing, so no bowl entities exist.
        assert "no bowls" in message
        # Nothing identifying. There is nothing to identify when the list is
        # empty, so there is no reason for any of it to be in the line.
        for secret in ("user@example.com", "user", SECRET_TOKEN, DEVICE_ID, SECOND_DEVICE_ID):
            assert secret not in message, message

        # A second consecutive empty poll must not repeat it.
        caplog.clear()
        await coordinator.async_refresh()
        assert coordinator.data == {}
        assert _our_warnings(caplog) == []

        # And a poll that DOES find bowls says nothing at all.
        caplog.clear()
        mock_api.device_rows_override = None
        await coordinator.async_refresh()
        assert coordinator.data
        assert _our_warnings(caplog) == []


async def test_an_empty_device_list_is_warned_about_again_once_it_recurs(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A list that fills and empties again is a NEW transition, and must warn again.

    Without this, suppressing the repeat could be implemented as "warn once
    ever", and an account that lost its bowl a week after setup would go
    back to being silent -- the exact condition this warning exists for.
    """
    with caplog.at_level(logging.WARNING, logger="custom_components.alwaysfull"):
        coordinator = await _setup(hass)
        assert _our_warnings(caplog) == []

        mock_api.device_rows_override = []
        await coordinator.async_refresh()
        assert len(_our_warnings(caplog)) == 1

        caplog.clear()
        mock_api.device_rows_override = None
        await coordinator.async_refresh()
        mock_api.device_rows_override = []
        await coordinator.async_refresh()
        assert len(_our_warnings(caplog)) == 1


async def test_a_persistent_empty_device_list_is_repeated_but_rarely(
    hass: HomeAssistant,
    mock_api: FakeAlwaysFullClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The condition must not go quiet forever once it has been mentioned once.

    A warning that fires on the transition and never again is a warning
    somebody misses by restarting Home Assistant at the wrong moment. It
    repeats, just rarely enough that it cannot drown the log.
    """
    mock_api.device_rows_override = []

    with caplog.at_level(logging.WARNING, logger="custom_components.alwaysfull"):
        coordinator = await _setup(hass)
        caplog.clear()

        # One full repeat cycle of further polls, and exactly one more line.
        for _ in range(EMPTY_DEVICE_LIST_EVERY_N_POLLS):
            await coordinator.async_refresh()

        assert len(_our_warnings(caplog)) == 1
