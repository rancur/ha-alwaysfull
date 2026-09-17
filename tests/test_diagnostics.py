"""The diagnostics download, and everything that must never be in it.

A diagnostics file is the one artefact this integration produces that users
are actively encouraged to paste into a public issue tracker. Everything
here is written from that assumption.

The tests go through the real `/api/diagnostics/config_entry/<id>` HTTP
endpoint rather than calling `async_get_config_entry_diagnostics` directly.
That is deliberate: it is the only way to prove the payload actually
survives JSON serialisation. A dataclass, a `datetime.time` or a `set` left
in the structure returns perfectly from a direct call and answers HTTP 500
to the user, and a direct-call test would never see it.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.alwaysfull.const import DOMAIN

from .conftest import DEVICE_ID, SECOND_DEVICE_ID, load_fixture_data

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

    from .conftest import FakeAlwaysFullClient

# Distinctive stand-ins for the two secrets. The suite's usual "pw"/"T" are
# too short to assert on: a single "T" occurs in half the timestamps in the
# payload, so `"T" not in text` would fail against a perfectly clean file
# and `"pw" not in text` would pass against one that leaked a longer
# password. These cannot occur by accident.
SENTINEL_PASSWORD = "SENTINEL-PASSWORD-DO-NOT-PUBLISH"
SENTINEL_TOKEN = "SENTINEL-TOKEN-DO-NOT-PUBLISH"

ACCOUNT_EMAIL = "user@example.com"

# The bowl's NAME is the user's, not the vendor's: people name bowls after
# their pet, their room, or themselves. It is not an identifier the vendor
# assigned, which is exactly why a redaction set assembled by looking for
# identifier-shaped fields misses it. It arrives twice -- as `deviceName` in
# the vendor's raw device row, and as `device_name` once `BowlState` has
# parsed that row -- so one spelling redacted and the other forgotten
# publishes it anyway.
SENTINEL_BOWL_NAME = "SENTINEL-BOWL-NAME-DO-NOT-PUBLISH"

# A bare twelve-hex run, with the neighbours checked by hand rather than
# with `\b`: `\b` treats a hex/non-hex boundary inside a longer hex string
# as a word boundary only when the neighbour is non-word, so `\b` alone
# would match the first twelve characters of a sixteen-hex id and, worse,
# would NOT match one embedded in a longer identifier. The vendor's device
# id is the bowl's MAC address with the separators stripped, which is
# exactly this shape.
BARE_MAC = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{12}(?![0-9a-fA-F])")

# The punctuated spellings, in case a future field carries one.
PUNCTUATED_MAC = re.compile(r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")


def rows_named(name: str) -> list[dict[str, Any]]:
    """Return the multi-bowl device rows with every bowl renamed to `name`.

    `load_fixture_data` re-reads and re-parses the JSON on every call, so
    renaming the rows here cannot bleed into any other test.
    """
    rows: list[dict[str, Any]] = load_fixture_data("device_list_multi")["data"]
    for row in rows:
        row["deviceName"] = name
    return rows


async def _load_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Load an entry carrying the sentinel secrets, with no platforms.

    Diagnostics reads `entry.runtime_data` and nothing else, so forwarding
    the platforms would add eight platform setups to the test for no extra
    coverage.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "email": ACCOUNT_EMAIL,
            "password": SENTINEL_PASSWORD,
            "token": SENTINEL_TOKEN,
        },
        unique_id=ACCOUNT_EMAIL,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.alwaysfull.PLATFORMS", []):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_diagnostics_leak_no_credentials_account_or_device_identity(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_api: FakeAlwaysFullClient,
) -> None:
    """Nothing identifying survives into the file a user pastes in public.

    Asserted against the SERIALISED text, not against the structure. A
    structural check has to know every place a value could be hiding, and
    the one it forgets is the one that leaks: a device id used as a `dict`
    key is invisible to `async_redact_data`, which matches key names and
    never looks at values.
    """
    mock_api.device_rows_override = rows_named(SENTINEL_BOWL_NAME)
    entry = await _load_entry(hass)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = json.dumps(diagnostics)

    assert SENTINEL_TOKEN not in text
    assert SENTINEL_PASSWORD not in text
    assert ACCOUNT_EMAIL not in text

    # The vendor's device ids, which ARE the bowls' MAC addresses, both
    # as bare values and as the `msg` text they are interpolated into
    # ("Bowl <mac> is filling.").
    assert DEVICE_ID not in text
    assert SECOND_DEVICE_ID not in text

    # The bowl's name, in BOTH spellings: `deviceName` on the vendor's raw
    # row and `device_name` on the parsed state. Asserted against the
    # serialised text, so dropping either key from `TO_REDACT` fails here.
    assert SENTINEL_BOWL_NAME not in text

    # `userId` ties the file to one vendor account.
    assert '"userId"' not in text or '"userId": "**REDACTED**"' in text

    # And nothing MAC-shaped at all, whatever field it arrived in. This is
    # what covers a vendor field nobody has seen yet.
    assert BARE_MAC.search(text) is None, f"a MAC-shaped token survived: {BARE_MAC.search(text)}"
    assert PUNCTUATED_MAC.search(text) is None


async def test_diagnostics_actually_report_the_device(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_api: FakeAlwaysFullClient,
) -> None:
    """The file is useful, so the redaction test above cannot pass vacuously.

    Every assertion in `test_diagnostics_leak_no_credentials_account_or_
    device_identity` is satisfied by returning `{}`. This is the test that
    makes returning `{}` fail, and it is the reason both exist.
    """
    entry = await _load_entry(hass)
    diagnostics: Any = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    devices = diagnostics["devices"]
    # Two real bowls in `device_list_multi.json`; the third row has a null
    # `deviceId` and is dropped by the coordinator.
    assert len(devices) == 2

    bowl = devices[0]
    # Real, non-redacted state that a maintainer reading an issue needs.
    assert bowl["state"]["slave_type"] == 2
    assert bowl["state"]["status"] == 1
    assert bowl["config"]["clean_cycle_seconds"] == 3600
    assert bowl["water_today"] == 903

    # The alert rows are present and their text is redacted RATHER THAN
    # dropped. Dropping the key would also satisfy the leak test while
    # hiding from the maintainer that alerts exist at all.
    alerts = bowl["notifications"]
    assert alerts
    assert alerts[0]["type"]
    assert alerts[0]["msg"] == "**REDACTED**"

    # The account-level notification settings, which are most of the
    # reason anyone opens an issue about this integration.
    assert diagnostics["notify_config"]["notifyItems"]

    # The poll's own health, so "it stopped updating" reports carry the
    # answer with them.
    assert diagnostics["coordinator"]["last_update_success"] is True


async def test_diagnostics_label_each_bowl_stably_without_naming_it(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_api: FakeAlwaysFullClient,
) -> None:
    """Two bowls stay distinguishable in the file without either being named.

    Redacting the device id to a constant `**REDACTED**` would make a
    two-bowl report unreadable -- every row would look like every other
    row, and a bug that only affects the second bowl could not be
    described. The label has to differ per bowl and be stable across
    downloads, while carrying nothing about the hardware.
    """
    entry = await _load_entry(hass)
    first: Any = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    second: Any = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    labels = [device["id"] for device in first["devices"]]
    assert len(set(labels)) == 2
    assert labels == [device["id"] for device in second["devices"]]


# A stand-in for the account-holder record the vendor's `/app/user/loginInfo`
# endpoint returns. This integration does NOT call that endpoint -- see the
# note in `api.py` -- and that is exactly why this test exists.
#
# A redaction test written only against the payloads we fetch TODAY passes
# forever while a new endpoint leaks: the day someone adds a call for
# `subscribe` (the one genuinely useful field on that record), the rest of
# the object rides along, because `diagnostics.py` passes vendor payloads
# through verbatim by design. Feeding the shape through before anybody
# fetches it is what makes the redaction set a property of the file rather
# than a list of the fields that happened to exist when it was written.
#
# Every value here is synthetic. `555-0100` is in the block reserved for
# fiction, `1 Test Street` is not a place, and the rest are sentinels for
# the same reason `SENTINEL_PASSWORD` is one: a realistic short value like
# a two-letter state code occurs by accident in a clean file, so an
# assertion on it would be either vacuous or flaky.
LOGIN_INFO_PII = {
    "firstName": "SENTINEL-FIRST-NAME-DO-NOT-PUBLISH",
    "lastName": "SENTINEL-LAST-NAME-DO-NOT-PUBLISH",
    "phone": "+1-555-0100",
    "countryCode": "SENTINEL-COUNTRY-CODE-DO-NOT-PUBLISH",
    "address1": "1 Test Street",
    "address2": "Apt SENTINEL-DO-NOT-PUBLISH",
    "city": "SENTINEL-CITY-DO-NOT-PUBLISH",
    "st": "SENTINEL-STATE-DO-NOT-PUBLISH",
    "zip": "SENTINEL-POSTCODE-DO-NOT-PUBLISH",
}

# The two fields on that same record that are NOT personal and ARE worth
# reading in a bug report: whether the account has the vendor's
# subscription, and when it lapses. Redacting the whole object indis-
# criminately would throw these away, so they are asserted to survive.
LOGIN_INFO_KEEP = {"subscribe": 1, "subscribeExpires": "2027-01-01 00:00:00"}


async def test_diagnostics_redact_account_holder_fields_we_do_not_yet_fetch(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_api: FakeAlwaysFullClient,
) -> None:
    """A loginInfo-shaped payload leaks nothing, at either place it could land.

    Injected in BOTH positions a future contributor could plausibly put it:
    merged into the vendor's raw device row (nested inside a list) and into
    the account-level notify config (a top-level object). `async_redact_data`
    recurses, so one position passing does not prove the other does -- but
    a field name dropped from `TO_REDACT` fails both.
    """
    rows: list[dict[str, Any]] = load_fixture_data("device_list_multi")["data"]
    for row in rows:
        row.update(LOGIN_INFO_PII)
        row.update(LOGIN_INFO_KEEP)
    mock_api.device_rows_override = rows

    notify_config: dict[str, Any] = load_fixture_data("notify_config")
    notify_config.update(LOGIN_INFO_PII)
    notify_config.update(LOGIN_INFO_KEEP)
    mock_api.saved_notify_config = notify_config

    entry = await _load_entry(hass)
    diagnostics: Any = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = json.dumps(diagnostics)

    for field, value in LOGIN_INFO_PII.items():
        assert value not in text, f"{field} survived into the diagnostics download"

    # Not vacuous: the payload really did reach the file, carrying the two
    # fields that are meant to stay readable.
    assert diagnostics["notify_config"]["subscribe"] == 1
    assert diagnostics["notify_config"]["subscribeExpires"] == "2027-01-01 00:00:00"
    assert diagnostics["devices"][0]["raw"]["subscribe"] == 1
