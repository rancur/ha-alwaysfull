"""Config and options flows for Always Full.

Four entry points:

- `user` -- email + password, validated by an actual `login()` call so a
  typo is caught while the user is still looking at the form rather than
  three minutes later as a failed setup.
- `reauth_confirm` -- the coordinator raises `ConfigEntryAuthFailed` when a
  re-login with the stored credentials fails, and Home Assistant lands the
  user here. The email is already known, so only the password is asked for.
- `reconfigure` -- moves an entry to a different account.
- options -- the poll interval, bounded 30-600 s.

Platform notes, verified against the installed Home Assistant 2026.9.2
rather than taken from tutorials (most of which are wrong on both counts):

- `OptionsFlow.config_entry` is a read-only property resolved from
  `self._config_entry_id`; assigning it in `__init__` raises. It is read
  here, never written, and this options flow has no `__init__` at all.
- `async_update_reload_and_abort` must not be combined with a config entry
  update listener: `config_entries.py` reports that pairing as breaking in
  2026.12. `__init__.py` deliberately registers no update listener, which
  is also what lets the options flow inherit `OptionsFlowWithReload` --
  that class raises outright if an update listener is present.

Secrets: the password and the token are never logged, never put in an
exception message and never used as a form default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import AlwaysFullClient
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import ha_utc_offset_hours
from .exceptions import AlwaysFullAuthError, AlwaysFullRateLimitError

if TYPE_CHECKING:
    from collections.abc import Mapping

EMAIL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="email")
)
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR})


class AlwaysFullConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an Always Full config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AlwaysFullOptionsFlow:
        """Return the options flow.

        The entry is intentionally NOT stored on the flow: `config_entry`
        is a read-only property on `OptionsFlow` in 2026.9 and assigning it
        raises `AttributeError`.
        """
        return AlwaysFullOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add an account from the UI."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]

            # Checked BEFORE the login call: a duplicate needs no round trip
            # against a rate-limited vendor API to be recognised.
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            errors, token = await self._async_validate(email, password)
            if not errors:
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                        CONF_TOKEN: token,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=CREDENTIALS_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after the stored credentials were rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh password for the account already configured."""
        entry = self._get_reauth_entry()
        email = entry.data[CONF_EMAIL]
        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            errors, token = await self._async_validate(email, password)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_PASSWORD: password, CONF_TOKEN: token},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={CONF_EMAIL: email},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Point an existing entry at a different account."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            unique_id = email.lower()

            # Only when the address actually changes: re-asserting the entry's
            # OWN unique id would match itself and abort every unchanged
            # reconfigure as a duplicate.
            if unique_id != entry.unique_id:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

            errors, token = await self._async_validate(email, password)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=unique_id,
                    data_updates={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                        CONF_TOKEN: token,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, {CONF_EMAIL: entry.data[CONF_EMAIL]}
            ),
            errors=errors,
        )

    async def _async_validate(self, email: str, password: str) -> tuple[dict[str, str], str]:
        """Log in once and return `(errors, token)`.

        An empty error dict means the token is good. Nothing here -- return
        value, log line or exception -- carries the password or the token.
        """
        client = AlwaysFullClient(
            async_get_clientsession(self.hass),
            tz_offset_hours=ha_utc_offset_hours(),
        )
        try:
            token = await client.login(email, password)
        except AlwaysFullAuthError:
            return {"base": "invalid_auth"}, ""
        except (AlwaysFullRateLimitError, TimeoutError, aiohttp.ClientError):
            # A rate limit is not an authentication problem: sending the user
            # to "check your password" for a 429 is how people end up
            # changing credentials that were never wrong.
            return {"base": "cannot_connect"}, ""
        except Exception:  # noqa: BLE001 -- see below
            # Deliberately blind. Anything escaping this method aborts the
            # flow with Home Assistant's generic "unknown error" page and no
            # way back; a form carrying `unknown` keeps the user in the flow,
            # and the traceback still reaches the log for diagnosis.
            LOGGER.exception("Unexpected error while validating Always Full credentials")
            return {"base": "unknown"}, ""

        return {}, str(token or "")


class AlwaysFullOptionsFlow(OptionsFlowWithReload):
    """Expose the poll interval.

    `OptionsFlowWithReload` reloads the entry when the options change, which
    is what makes a new interval take effect without a Home Assistant
    restart. It is only legal because `__init__.py` registers no config
    entry update listener -- with one, Home Assistant raises here.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the poll interval."""
        errors: dict[str, str] = {}

        if user_input is not None:
            seconds = _as_bounded_seconds(user_input.get(CONF_SCAN_INTERVAL))
            if seconds is not None:
                return self.async_create_entry(data={CONF_SCAN_INTERVAL: seconds})
            # Defence in depth. The selector below already rejects these
            # bounds, and the flow manager applies that schema before this
            # step ever runs, so in the normal UI/API path an out-of-range
            # value is refused before it gets here. This branch is what
            # answers the question "and if something reached the step
            # anyway?" -- the answer is a form with an error, not a
            # coordinator hammering the vendor every second.
            errors[CONF_SCAN_INTERVAL] = "invalid_scan_interval"

        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
            errors=errors,
        )


def _as_bounded_seconds(value: Any) -> int | None:
    """Return `value` as whole seconds inside the supported band, else `None`.

    The selector hands back a float; the coordinator and the entry options
    both want an int, so the rounding happens once, here.
    """
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if MIN_SCAN_INTERVAL <= seconds <= MAX_SCAN_INTERVAL:
        return seconds
    return None
