"""Diagnostics for Always Full.

This is the one artefact the integration produces that users are actively
told to attach to a public issue, so the default assumption here is that
everything in it will be read by strangers.

Two things about `async_redact_data` decide the shape of this module, and
both are easy to get wrong:

- It matches KEY NAMES and never looks at values. A device id used as a
  `dict` key is therefore invisible to it and would be published intact.
  That is why `devices` is a LIST and every bowl carries a derived label
  instead of being keyed on its id.
- It cannot know that a free-text field contains an identifier. The
  vendor's alert text is `"Bowl <mac> is filling."`, so `msg` embeds the
  bowl's MAC address in prose. Redacting `deviceId` and leaving `msg`
  would publish the same value one line further down.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .coordinator import scan_interval_seconds
from .models import device_label

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import AlwaysFullConfigEntry, BowlData

# Redacted everywhere they appear, at any depth.
#
# The device-id spellings are all three that occur, because the vendor is
# not consistent about it and this dataclass layer adds a fourth: the wire
# uses `deviceId` on device rows and `devNo` on config objects, and
# `dataclasses.asdict` produces `device_id`. Listing only the one you
# happen to have looked at publishes the other two.
TO_REDACT = {
    # Credentials. Never logged, never raised, and not published here.
    "token",
    "password",
    # Account identity. The config entry's unique id IS the account's
    # email address, so anything carrying it is redacted rather than
    # trusted to be absent.
    "email",
    "account",
    "userId",
    # Device identity. The vendor's device id is the bowl's MAC address
    # with the separators stripped -- a hardware identifier, not an opaque
    # handle.
    "deviceId",
    "devNo",
    "device_id",
    # The bowl's NAME, which the user chose. Not an identifier the vendor
    # issued, which is why a set assembled by looking for identifier-shaped
    # fields misses it -- but people name bowls after their pets, their
    # rooms and sometimes themselves. Both spellings, for the same reason
    # the device id needs three: the wire says `deviceName` and
    # `dataclasses.asdict(BowlState)` says `device_name`, and each one
    # appears in this file on its own.
    "deviceName",
    "device_name",
    # Free text that embeds the MAC: "Bowl <mac> is filling." Not obvious,
    # and the reason a redaction set built only from field NAMES that look
    # like identifiers is not enough.
    "msg",
    # THE ACCOUNT HOLDER'S NAME, PHONE NUMBER AND POSTAL ADDRESS.
    #
    # Nothing this integration fetches produces any of these today. They
    # are listed anyway, because the endpoint that returns them --
    # `/app/user/loginInfo`, described in `api.py` -- is one call away from
    # being added: it carries `subscribe`, which is a genuinely useful
    # thing to want, and everything above rides along with it in the same
    # flat object.
    #
    # Redaction here is by KEY NAME, so listing a key that never appears
    # costs exactly nothing. The reverse costs a user their home address,
    # published to a public issue tracker by a contributor who added one
    # API call and had no reason to think about this file. Which of those
    # two mistakes to risk is not a close question.
    #
    # `subscribe` and `subscribeExpires` are deliberately NOT here: they
    # say whether the account has the vendor's subscription and when it
    # lapses, which is worth reading in a bug report and is nobody's
    # personal data.
    "firstName",
    "lastName",
    "phone",
    "countryCode",
    "address1",
    "address2",
    "city",
    "st",
    "zip",
}


def _bowl_diagnostics(bowl: BowlData) -> dict[str, Any]:
    """Return everything worth knowing about one bowl, before redaction."""
    return {
        "id": device_label(bowl.device_id),
        "state": dataclasses.asdict(bowl.state),
        "config": dataclasses.asdict(bowl.config),
        "water_today": bowl.water_today,
        # The vendor's untouched device row. Its redacted fields are
        # already covered; what is left is the firmware version, the
        # timestamps and any field the vendor has added since this
        # integration was written -- which is exactly what a bug report
        # about an unsupported bowl needs to carry.
        "raw": bowl.raw,
        "notifications": bowl.notifications,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: AlwaysFullConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for one config entry."""
    coordinator = entry.runtime_data

    data: dict[str, Any] = {
        "entry": {
            "data": dict(entry.data),
            "options": dict(entry.options),
            # NOT `entry.unique_id`, which is the account's email address.
            # Whether one is set is the part that ever matters in a bug
            # report -- an entry without one predates the config flow
            # setting it and would key the account entities differently.
            "unique_id_set": entry.unique_id is not None,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "configured_scan_interval_seconds": scan_interval_seconds(entry),
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval is not None
                else None
            ),
            "bowl_count": len(coordinator.data or {}),
            # `notify/getConfig` is fetched once every ten polls, so "not
            # read yet" is a real state that makes every account-level
            # switch unavailable. Worth stating rather than leaving the
            # reader to infer it from a null below.
            "notify_config_read": coordinator.notify_config is not None,
        },
        # A LIST, not a mapping. Keying this on the device id would put the
        # bowl's MAC address in a position `async_redact_data` does not
        # look at -- it matches key names against values it never reads.
        "devices": [_bowl_diagnostics(bowl) for bowl in (coordinator.data or {}).values()],
        "notify_config": coordinator.notify_config,
    }

    return async_redact_data(data, TO_REDACT)
