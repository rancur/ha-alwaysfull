"""Minimal config flow placeholder.

Task 5 owns the real config flow (user / reauth / reconfigure steps, the
options flow and the translations). This module exists at Task 4 because
`manifest.json` sets `config_flow: true`, and Home Assistant then refuses
to set up ANY config entry for the domain unless the `config_flow`
platform imports -- and, separately, because `ConfigEntryAuthFailed` from
the coordinator starts a reauth flow that needs a handler to land in.

Only the reauth entry point is implemented here, deliberately: it is the
piece the coordinator's error handling depends on. Adding the integration
from the UI arrives with Task 5.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD

from .const import DOMAIN

REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


class AlwaysFullConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an Always Full config flow."""

    VERSION = 1

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after the stored credentials were rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt for a fresh password. Task 5 makes this actually validate."""
        return self.async_show_form(step_id="reauth_confirm", data_schema=REAUTH_SCHEMA)
