"""Config flow: user, reauth, reconfigure, options and the shipped translations.

Every test here asserts the thing that would actually be wrong if the
behaviour regressed -- the error key, the stored credential, the unique id --
rather than only that a form came back. `FlowResultType.FORM` on its own is
true of both a correctly-rejected login and a silently-swallowed one.

The client is replaced at `config_flow`'s own import site (not the package's),
because `tests/conftest.py`'s `mock_api` patches
`custom_components.alwaysfull.AlwaysFullClient`, which is a different name
binding and would leave the config flow talking to the real vendor API.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alwaysfull.config_flow import AlwaysFullConfigFlow
from custom_components.alwaysfull.const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.alwaysfull.coordinator import scan_interval_seconds
from custom_components.alwaysfull.exceptions import (
    AlwaysFullAuthError,
    AlwaysFullError,
    AlwaysFullRateLimit,
)

from .conftest import FakeAlwaysFullClient

INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / "alwaysfull"

EMAIL = "user@example.com"
MIXED_CASE_EMAIL = "User@Example.COM"
OTHER_EMAIL = "other@example.com"
OLD_PASSWORD = "old-placeholder-value"
NEW_PASSWORD = "new-placeholder-value"
OLD_TOKEN = "OLD-TOKEN"
FRESH_TOKEN = "FRESH-TOKEN"


@pytest.fixture
def flow_api() -> Generator[FakeAlwaysFullClient]:
    """Patch the config flow's client and stub entry setup.

    Entry setup is stubbed because a created entry is set up immediately;
    without the stub that setup would build a REAL client and poll the
    vendor API. This fixture keeps each test about the flow itself.
    """
    client = FakeAlwaysFullClient()
    with (
        patch(
            "custom_components.alwaysfull.config_flow.AlwaysFullClient",
            side_effect=lambda session, **kwargs: _bind(client, session, **kwargs),
        ),
        patch(
            "custom_components.alwaysfull.async_setup_entry",
            AsyncMock(return_value=True),
        ),
    ):
        yield client


def _bind(client: FakeAlwaysFullClient, session: Any, **kwargs: Any) -> FakeAlwaysFullClient:
    """Record the constructor arguments the config flow used."""
    client.session = session
    client.tz_offset_hours = kwargs.get("tz_offset_hours")
    return client


def _add_entry(
    hass: HomeAssistant,
    *,
    email: str = EMAIL,
    password: str = OLD_PASSWORD,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Add an already-configured entry for `email`."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=email,
        unique_id=email.lower(),
        data={CONF_EMAIL: email, CONF_PASSWORD: password, CONF_TOKEN: OLD_TOKEN},
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def _start_user_flow(hass: HomeAssistant) -> str:
    """Start the user flow and return its flow id."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]
    return result["flow_id"]


# -- User step -------------------------------------------------------------


async def test_user_flow_creates_entry(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient
) -> None:
    """A good login creates an entry with the credentials and fresh token."""
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: MIXED_CASE_EMAIL, CONF_PASSWORD: NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == MIXED_CASE_EMAIL
    assert result["data"] == {
        CONF_EMAIL: MIXED_CASE_EMAIL,
        CONF_PASSWORD: NEW_PASSWORD,
        CONF_TOKEN: FRESH_TOKEN,
    }
    # Lowercased, so the same account typed with different capitalisation is
    # recognised as already configured.
    assert result["result"].unique_id == EMAIL
    # The credentials really were validated, with the address as typed.
    assert flow_api.login_calls == [MIXED_CASE_EMAIL]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AlwaysFullAuthError("Account or password error"), "invalid_auth"),
        (aiohttp.ClientError("boom"), "cannot_connect"),
        (TimeoutError(), "cannot_connect"),
        (AlwaysFullRateLimit("429"), "cannot_connect"),
        (AlwaysFullError("Unexpected response code 500"), "unknown"),
        (RuntimeError("something else entirely"), "unknown"),
    ],
)
async def test_user_flow_login_failures(
    hass: HomeAssistant,
    flow_api: FakeAlwaysFullClient,
    error: Exception,
    expected: str,
) -> None:
    """Each failure mode maps onto its own error key and creates nothing."""
    flow_api.login_error = error
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: EMAIL, CONF_PASSWORD: NEW_PASSWORD}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_user_flow_recovers_after_bad_credentials(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient
) -> None:
    """A rejected login leaves the flow usable, not wedged on the error."""
    flow_api.login_error = AlwaysFullAuthError("Account or password error")
    flow_id = await _start_user_flow(hass)

    rejected = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: EMAIL, CONF_PASSWORD: OLD_PASSWORD}
    )
    assert rejected["errors"] == {"base": "invalid_auth"}

    flow_api.login_error = None
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: EMAIL, CONF_PASSWORD: NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PASSWORD] == NEW_PASSWORD


async def test_unexpected_error_never_logs_the_secrets(
    hass: HomeAssistant,
    flow_api: FakeAlwaysFullClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The `unknown` path logs a traceback but never the password or token."""
    flow_api.login_error = AlwaysFullError("Unexpected response code 500")
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: EMAIL, CONF_PASSWORD: NEW_PASSWORD}
    )

    assert result["errors"] == {"base": "unknown"}
    # Something was logged -- otherwise this test would pass trivially on an
    # implementation that logs nothing at all.
    assert "Unexpected error" in caplog.text
    assert NEW_PASSWORD not in caplog.text
    assert FRESH_TOKEN not in caplog.text


async def test_same_account_twice_aborts(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient
) -> None:
    """A second entry for the same account aborts before touching the API."""
    _add_entry(hass)
    flow_id = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: MIXED_CASE_EMAIL, CONF_PASSWORD: NEW_PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    # No pointless login against a rate-limited vendor API for a duplicate.
    assert flow_api.login_calls == []


# -- Reauth ----------------------------------------------------------------


async def test_reauth_updates_password_and_token(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient
) -> None:
    """Reauth completes, rewrites the stored secrets and keeps the email."""
    entry = _add_entry(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == NEW_PASSWORD
    assert entry.data[CONF_TOKEN] == FRESH_TOKEN
    assert entry.data[CONF_EMAIL] == EMAIL
    # Validated against the stored email -- reauth asks for the password only.
    assert flow_api.login_calls == [EMAIL]


async def test_reauth_rejects_a_still_wrong_password(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient
) -> None:
    """A rejected reauth shows `invalid_auth` and leaves the entry untouched."""
    entry = _add_entry(hass)
    flow_api.login_error = AlwaysFullAuthError("Account or password error")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_PASSWORD] == OLD_PASSWORD
    assert entry.data[CONF_TOKEN] == OLD_TOKEN


# -- Reconfigure -----------------------------------------------------------


async def test_reconfigure_changes_the_email(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient
) -> None:
    """Reconfigure moves the entry to another account, unique id included."""
    entry = _add_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: OTHER_EMAIL, CONF_PASSWORD: NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_EMAIL] == OTHER_EMAIL
    assert entry.data[CONF_PASSWORD] == NEW_PASSWORD
    assert entry.data[CONF_TOKEN] == FRESH_TOKEN
    assert entry.unique_id == OTHER_EMAIL
    assert flow_api.login_calls == [OTHER_EMAIL]


async def test_reconfigure_rejects_an_email_already_configured(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient
) -> None:
    """Reconfiguring onto another entry's account aborts and changes nothing."""
    entry = _add_entry(hass)
    _add_entry(hass, email=OTHER_EMAIL)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: OTHER_EMAIL, CONF_PASSWORD: NEW_PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_EMAIL] == EMAIL
    assert entry.unique_id == EMAIL


async def test_reconfigure_keeping_the_same_email_still_succeeds(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient
) -> None:
    """Re-submitting the same address is a password update, not a duplicate."""
    entry = _add_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PASSWORD] == NEW_PASSWORD


# -- Options ---------------------------------------------------------------


async def test_options_flow_round_trips_the_interval(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient
) -> None:
    """A valid interval is stored as an int the coordinator will use as-is."""
    entry = _add_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    # The form offers the default when nothing has been chosen yet.
    assert result["data_schema"]({})[CONF_SCAN_INTERVAL] == DEFAULT_SCAN_INTERVAL

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCAN_INTERVAL: 120}
    assert isinstance(entry.options[CONF_SCAN_INTERVAL], int)
    # The option name matches what the coordinator actually reads.
    assert scan_interval_seconds(entry) == 120


async def test_options_flow_offers_the_current_value(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient
) -> None:
    """Reopening the options shows what is configured, not the default."""
    entry = _add_entry(hass, options={CONF_SCAN_INTERVAL: 300})

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["data_schema"]({})[CONF_SCAN_INTERVAL] == 300


@pytest.mark.parametrize("bad", [10, 700])
async def test_options_flow_rejects_out_of_band_intervals(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient, bad: int
) -> None:
    """The schema itself refuses out-of-range values, so an API POST fails."""
    entry = _add_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData) as err:
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: bad}
        )

    assert CONF_SCAN_INTERVAL in err.value.schema_errors
    assert entry.options == {}


@pytest.mark.parametrize("bad", [10, 700, "banana"])
async def test_options_step_validates_even_without_the_schema(
    hass: HomeAssistant, flow_api: FakeAlwaysFullClient, bad: Any
) -> None:
    """The step re-checks the bounds itself, not only in the form schema.

    Driven directly, bypassing the flow manager's schema validation, which
    is the one layer a caller reaching the step by any other route would
    not go through.
    """
    entry = _add_entry(hass)
    flow = AlwaysFullConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id

    result = await flow.async_step_init({CONF_SCAN_INTERVAL: bad})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SCAN_INTERVAL: "invalid_scan_interval"}
    assert entry.options == {}


# -- Translations ----------------------------------------------------------


def _load(name: str) -> dict[str, Any]:
    """Load one of the shipped translation files."""
    return json.loads((INTEGRATION_DIR / name).read_text())


def test_translations_are_literal_and_complete() -> None:
    """`translations/en.json` must carry real English for every flow string.

    `strings.json` is not read at runtime for a custom integration, and a
    `[%key:...%]` reference copied from a core integration is never resolved
    -- the user sees the raw key text on screen. hassfest does not catch it.
    """
    strings = _load("strings.json")
    english = _load("translations/en.json")

    assert strings == english
    assert "[%key:" not in (INTEGRATION_DIR / "translations/en.json").read_text()

    config = english["config"]
    assert set(config["step"]) == {"user", "reauth_confirm", "reconfigure"}
    assert set(config["step"]["user"]["data"]) == {CONF_EMAIL, CONF_PASSWORD}
    assert set(config["step"]["reauth_confirm"]["data"]) == {CONF_PASSWORD}
    assert set(config["step"]["reconfigure"]["data"]) == {CONF_EMAIL, CONF_PASSWORD}
    assert {"cannot_connect", "invalid_auth", "unknown"} <= set(config["error"])
    assert {
        "already_configured",
        "reauth_successful",
        "reconfigure_successful",
    } <= set(config["abort"])

    options = english["options"]
    assert set(options["step"]["init"]["data"]) == {CONF_SCAN_INTERVAL}
    assert "invalid_scan_interval" in options["error"]
