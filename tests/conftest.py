"""Shared fixtures for the Always Full test suite.

The HA-level tests here never touch the network. `AlwaysFullClient` is
replaced wholesale by `FakeAlwaysFullClient`, which serves the committed,
sanitised fixtures under `tests/fixtures/` and records what it was asked
for. That is deliberate: Tasks 1-3 already test the real client's signing,
parameter names and envelope handling as pure unit tests, so re-testing the
transport through a mocked HTTP layer here would only duplicate coverage
while coupling these tests to aiohttp's internals.

`aioresponses` is NOT used anywhere in this repo -- it raises
`TypeError: ... missing 'stream_writer'` against the aiohttp that
homeassistant 2026.9.2 pins. Tests that genuinely need HTTP mocking should
use pytest-homeassistant-custom-component's `aioclient_mock` fixture.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from syrupy.assertion import SnapshotAssertion

from custom_components.alwaysfull.const import DOMAIN
from custom_components.alwaysfull.coordinator import BowlData
from custom_components.alwaysfull.exceptions import AlwaysFullAuthError
from custom_components.alwaysfull.models import BowlConfig, BowlState

if TYPE_CHECKING:
    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Synthetic account, never a real one.
ENTRY_DATA = {"email": "user@example.com", "password": "pw", "token": "T"}

# What the config flow sets the entry's unique id to. The account-level
# entities key their unique ids on it, so a test entry without one would
# fall back to the randomly generated entry id and make those unique ids
# -- and therefore the switch snapshot -- different on every run.
ENTRY_UNIQUE_ID = ENTRY_DATA["email"]

# The two bowls in `device_list_multi.json`. Both ids are synthetic.
DEVICE_ID = "aabbccddeeff"
SECOND_DEVICE_ID = "001122334455"

# The day-boundary test freezes time here. Under freezegun the NAIVE clock
# (`datetime.now()` with no tz) reads the frozen UTC wall time whatever the
# host's TZ is -- verified, not assumed -- so both the "reads the OS clock"
# and "reads UTC" mistakes produce FROZEN_UTC_DATE. Asserting a zone whose
# local date differs from that is therefore host-independent on its own:
# 18:00 UTC on the 16th is already 03:00 on the 17th in Tokyo.
FROZEN_INSTANT = "2026-09-16T18:00:00+00:00"
FROZEN_ZONE = "Asia/Tokyo"
FROZEN_LOCAL_DATE = "2026-09-17"
FROZEN_UTC_DATE = "2026-09-16"

# DST-free zones, so each offset is a year-round constant.
_ZONE_CANDIDATES = (("Asia/Tokyo", 9), ("Pacific/Honolulu", -10))


def zone_unlike_host() -> tuple[str, int]:
    """Return a `(zone, utc_offset_hours)` pair whose offset differs from this host's.

    Used by the tests that run on the REAL clock, where the host's zone can
    leak into the result. Hard-coding a zone makes those tests
    host-dependent in both directions: on a runner that happens to sit at
    UTC+9, asserting "the offset is 9" passes for an implementation reading
    the OS clock (a false green), while asserting "the offset is not the
    host's" fails on entirely correct code (worse). Choosing at runtime
    keeps the asserted value a fixed known constant while guaranteeing it
    cannot have come from the host clock. The host can match at most one
    candidate, so this always finds one.
    """
    host_utc_offset = datetime.now().astimezone().utcoffset()
    assert host_utc_offset is not None
    host_offset = int(host_utc_offset.total_seconds() / 3600)

    for zone, offset in _ZONE_CANDIDATES:
        if offset != host_offset:
            return zone, offset

    msg = f"No candidate zone differs from the host offset {host_offset}"
    raise RuntimeError(msg)


def load_fixture_data(name: str) -> Any:
    """Return the `data` member of the committed `name`.json envelope."""
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())["data"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let every test in this suite load `custom_components.alwaysfull`.

    Home Assistant refuses to load custom integrations in tests unless this
    fixture is requested; making it autouse means no test can silently be
    testing a *built-in* integration that happens to share our domain.
    """
    return


class FakeAlwaysFullClient:
    """Drop-in stand-in for `AlwaysFullClient` backed by the JSON fixtures.

    Every method records its arguments so tests can assert on the exact
    calls the coordinator made (including the day-snapped drinking-log
    range and the once-every-N-polls notify-config fetch).
    """

    def __init__(
        self,
        session: Any = None,
        *,
        token: str = "",
        tz_offset_hours: int | None = None,
    ) -> None:
        """Mirror the real client's constructor signature exactly."""
        self.session = session
        self.token = token
        self.tz_offset_hours = tz_offset_hours
        # Kept separately because the coordinator re-syncs `tz_offset_hours`
        # on every poll; without this, a test could not tell whether the
        # value it sees came from the constructor or from that re-sync.
        self.constructed_tz_offset_hours = tz_offset_hours

        # Recorded calls.
        self.login_calls: list[str] = []
        self.device_list_calls = 0
        self.device_config_calls: list[str] = []
        self.drinking_log_calls: list[tuple[str, int, str, str]] = []
        self.notify_log_calls: list[str] = []
        self.notify_config_calls = 0

        # Every write this client was asked to make, in order, as
        # `(method_name, kwargs)`. The kwargs are the EXACT arguments the
        # platform handed the client -- wire field names and converted
        # values both -- which is the only level at which a minutes-for-
        # seconds or `capacity`-for-`filterCapacity` mistake is visible.
        self.writes: list[tuple[str, dict[str, Any]]] = []

        # Concurrency bookkeeping for the serialisation test. `max_writes_
        # in_flight` is the high-water mark of writes inside the client at
        # once; anything above 1 means two vendor requests overlapped.
        self.writes_in_flight = 0
        self.max_writes_in_flight = 0
        # When set, every write blocks here until the test releases it.
        # Without something to block ON, a write runs to completion the
        # instant it is called and concurrent callers could never be
        # OBSERVED to overlap -- the serialisation test would then pass
        # against an implementation holding no lock at all.
        self.write_gate: asyncio.Event | None = None

        # Injectable failures. `_remaining` of None means "raise forever".
        self.device_list_error: Exception | None = None
        self.device_list_error_remaining: int | None = None
        self.login_error: Exception | None = None
        self.write_error: Exception | None = None

        # Set by `save_notify_config`, so a saved object is what the next
        # read returns -- which is what makes a read-modify-write chain
        # testable end to end rather than one call at a time.
        self.saved_notify_config: dict[str, Any] | None = None

        # Injectable response, so a test can prove a re-read really re-read.
        self.device_config_override: dict[str, Any] | None = None

        # Injectable rows, so a platform test can put the bowl into a state
        # the committed captures do not contain (an alarm raised, a filter
        # fault, a day with no drinking row yet) without editing a fixture
        # that other tests assert against.
        self.device_rows_override: list[dict[str, Any]] | None = None
        # Either one list served to EVERY device, or a {device_id: rows}
        # mapping so a test can give each bowl its own alerts -- which is
        # what proves an alert lands on the bowl that raised it.
        self.notify_rows_override: (
            list[dict[str, Any]] | dict[str, list[dict[str, Any]]] | None
        ) = None
        self.drinking_rows_override: list[dict[str, Any]] | None = None

        # Per-device drinking totals, so a multi-device test can prove each
        # bowl got ITS OWN total rather than the first bowl's.
        self.drinking_totals: dict[str, int] = {DEVICE_ID: 903, SECOND_DEVICE_ID: 250}

    def fail_device_list(self, error: Exception, times: int | None = None) -> None:
        """Make `device_list()` raise `error`, for `times` calls (None = forever)."""
        self.device_list_error = error
        self.device_list_error_remaining = times

    async def login(self, email: str, password: str) -> str:
        """Record the login attempt and return a fresh token."""
        # The password is deliberately NOT recorded: no test needs it, and a
        # recorded secret is a secret waiting to end up in a failure dump.
        assert password
        self.login_calls.append(email)
        if self.login_error is not None:
            raise self.login_error
        self.token = "FRESH-TOKEN"
        return self.token

    async def device_list(self) -> Any:
        """Return the paginated device-list envelope from the fixtures.

        Serves `device_list_multi.json`, NOT the single-row live capture:
        with one row, every assertion about the per-device loop passes
        identically for an implementation that only ever handles `rows[0]`,
        so a user's second bowl could silently produce no entities at all
        with the suite still green. The multi fixture keeps the captured row
        verbatim and adds a second synthetic bowl plus a half-provisioned
        row with a null `deviceId`. `device_list.json` stays pristine for
        the transport tests.
        """
        self.device_list_calls += 1
        if self.device_list_error is not None and self.device_list_error_remaining != 0:
            if self.device_list_error_remaining is not None:
                self.device_list_error_remaining -= 1
            raise self.device_list_error
        envelope = copy.deepcopy(load_fixture_data("device_list_multi"))
        if self.device_rows_override is not None:
            envelope["data"] = copy.deepcopy(self.device_rows_override)
        return envelope

    async def device_config(self, device_id: str) -> Any:
        """Return the device-config object for `device_id` from the fixtures.

        `devNo` is echoed back as the requested id so a test can prove each
        bowl was given ITS OWN config, not another bowl's.
        """
        self.device_config_calls.append(device_id)
        config = copy.deepcopy(self.device_config_override or load_fixture_data("device_config"))
        config["devNo"] = device_id
        return config

    async def drinking_log(self, device_id: str, units: int, start: str, end: str) -> Any:
        """Return one row for the requested day, plus one unrelated day.

        Echoing the *requested* date back keeps this deterministic no matter
        what the calendar says when the suite runs, while the extra row
        proves the coordinator picks today's row rather than the first or
        last row the server happened to send.
        """
        self.drinking_log_calls.append((device_id, units, start, end))
        if self.drinking_rows_override is not None:
            return copy.deepcopy(self.drinking_rows_override)
        return [
            {"drinkingDate": "1999-12-31", "totalCapacity": 4242},
            {"drinkingDate": start, "totalCapacity": self.drinking_totals.get(device_id, 0)},
        ]

    async def notify_log(self, device_id: str, page_size: int = 20) -> Any:
        """Return the paginated notification-log envelope from the fixtures.

        The committed capture is one device's log, and it is served for
        WHICHEVER device is asked for -- deliberately, because that is the
        shape a server ignoring its `deviceId` parameter would produce, and
        the coordinator is what has to notice. A test that wants each bowl
        to have its own alerts passes a `{device_id: rows}` mapping.
        """
        self.notify_log_calls.append(device_id)
        envelope = copy.deepcopy(load_fixture_data("notify_log"))
        override = self.notify_rows_override
        if isinstance(override, dict):
            envelope["data"] = copy.deepcopy(override.get(device_id, []))
        elif override is not None:
            envelope["data"] = copy.deepcopy(override)
        return envelope

    async def notify_config(self) -> Any:
        """Return the account-level notification config.

        A previously saved object wins over the committed fixture, so a
        platform's read-modify-write really round-trips: a test can toggle a
        switch and then assert the NEXT read reflects it, which a fake that
        always served the pristine fixture could never show.
        """
        self.notify_config_calls += 1
        if self.saved_notify_config is not None:
            return copy.deepcopy(self.saved_notify_config)
        return copy.deepcopy(load_fixture_data("notify_config"))

    # -- Writers ------------------------------------------------------------
    #
    # Each records the exact call and then raises `write_error` if one is
    # armed, so a test gets to assert BOTH what a failing write attempted
    # and that the failure surfaced.

    async def _record(self, method: str, payload: dict[str, Any]) -> None:
        """Record one write, track overlap, then fail it if a failure is armed."""
        self.writes.append((method, copy.deepcopy(payload)))
        self.writes_in_flight += 1
        self.max_writes_in_flight = max(self.max_writes_in_flight, self.writes_in_flight)
        try:
            if self.write_gate is not None:
                await self.write_gate.wait()
        finally:
            self.writes_in_flight -= 1
        if self.write_error is not None:
            raise self.write_error

    async def save_notify_config(self, config: dict[str, Any]) -> Any:
        """Record the whole notification object and serve it back on the next read."""
        await self._record("save_notify_config", config)
        self.saved_notify_config = copy.deepcopy(config)
        return None

    async def set_flush_config(self, device_id: str, **fields: Any) -> Any:
        """Record a flush-config write."""
        await self._record("set_flush_config", {"device_id": device_id, **fields})
        return None

    async def set_sleep_config(self, device_id: str, **fields: Any) -> Any:
        """Record a sleep-config write."""
        await self._record("set_sleep_config", {"device_id": device_id, **fields})
        return None

    async def set_filter_config(self, device_id: str, **fields: Any) -> Any:
        """Record a filter-config write."""
        await self._record("set_filter_config", {"device_id": device_id, **fields})
        return None

    async def set_maintenance_config(self, device_id: str, **fields: Any) -> Any:
        """Record a maintenance-config write."""
        await self._record("set_maintenance_config", {"device_id": device_id, **fields})
        return None

    async def set_water_config(self, device_id: str, **fields: Any) -> Any:
        """Record a water-config write."""
        await self._record("set_water_config", {"device_id": device_id, **fields})
        return None

    async def set_log_config(self, device_id: str, **fields: Any) -> Any:
        """Record a drinking-log-config write."""
        await self._record("set_log_config", {"device_id": device_id, **fields})
        return None

    async def set_units(self, device_id: str, units: int) -> Any:
        """Record a units write."""
        await self._record("set_units", {"device_id": device_id, "units": units})
        return None

    async def set_device_type(self, device_id: str, device_type: int) -> Any:
        """Record a bowl-size write."""
        await self._record("set_device_type", {"device_id": device_id, "device_type": device_type})
        return None

    async def reset_filter(self, device_id: str) -> Any:
        """Record a filter-life reset."""
        await self._record("reset_filter", {"device_id": device_id})
        return None


@pytest.fixture
def mock_api() -> Generator[FakeAlwaysFullClient]:
    """Patch the integration's client with a fixture-backed fake."""
    client = FakeAlwaysFullClient()
    with patch(
        "custom_components.alwaysfull.AlwaysFullClient",
        autospec=False,
        side_effect=lambda session, **kwargs: _configure(client, session, **kwargs),
    ):
        yield client


@pytest.fixture
def mock_api_auth_fails(mock_api: FakeAlwaysFullClient) -> FakeAlwaysFullClient:
    """Return a client whose token is rejected AND whose re-login fails too."""
    mock_api.fail_device_list(AlwaysFullAuthError("Token expired"))
    mock_api.login_error = AlwaysFullAuthError("Account or password error")
    return mock_api


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Return the snapshot fixture with Home Assistant's serializer applied.

    Requested explicitly rather than relying on the identically named
    fixture pytest-homeassistant-custom-component ships: whichever plugin
    pytest registers LAST wins, and in this environment syrupy's own plain
    fixture was the one that won. The difference is not cosmetic -- without
    Home Assistant's serializer the snapshot records the raw `repr()`,
    including the randomly generated registry ids and the wall-clock
    `created_at`, so every snapshot assertion fails on the very next run.
    """
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
def enable_all_entities() -> Generator[None]:
    """Force-enable entities that ship disabled by default.

    `snapshot_platform` refuses to snapshot a disabled entity, and an
    entity nobody enables is an entity nobody has ever seen the state of.
    A plain `property` is a data descriptor, so it wins over the
    `cached_property` Home Assistant defines AND over anything already
    cached in an instance `__dict__` -- patching with a `MagicMock`
    attribute would not.
    """
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        property(lambda _self: True),
    ):
        yield


def device_row(device_id: str = DEVICE_ID, **overrides: Any) -> dict[str, Any]:
    """Return one committed device-list row with `overrides` applied.

    Used to put a bowl into a state the live capture does not contain
    (alarm raised, filter fault, detached water source) without editing a
    fixture other tests assert against.
    """
    rows = load_fixture_data("device_list_multi")["data"]
    row = next(r for r in rows if r["deviceId"] == device_id)
    return {**copy.deepcopy(row), **overrides}


def bowl_data(**overrides: Any) -> BowlData:
    """Return one `BowlData` built from the committed fixtures.

    Lets a test call a `value_fn` directly. That matters for the enum
    sensors: Home Assistant stores the literal string `"unknown"` in the
    same slot it uses for "no value", so at the state-machine level an
    option of `"unknown"` and a `None` are indistinguishable, and only a
    direct call can tell the two apart.
    """
    row = device_row(**overrides)
    return BowlData(
        device_id=row["deviceId"],
        state=BowlState.from_api(row),
        config=BowlConfig.from_api(load_fixture_data("device_config")),
        water_today=None,
        notifications=load_fixture_data("notify_log")["data"],
        raw=row,
    )


def only_write(client: FakeAlwaysFullClient, method: str) -> dict[str, Any]:
    """Return the payload of the ONE `method` call the client received.

    Asserting there is exactly one is the point: a platform that wrote
    twice -- say a partial followed by a whole object, or the same value
    once per bowl -- would satisfy "the last call looked right".
    """
    payloads = [payload for name, payload in client.writes if name == method]
    assert len(payloads) == 1, f"expected exactly one {method} call, got {len(payloads)}"
    return payloads[0]


def entity_id_for(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    """Return the entity id Home Assistant gave the entity with `unique_id`.

    An entity id is derived from the entity's ENGLISH NAME, so spelling one
    out in a test couples that test to wording which is allowed to change
    -- and worse, a renamed entity makes the test fail by referring to
    nothing at all, which reads like a broken platform. Resolving through
    the registry keys the test to the entity's identity instead. The
    literal ids stay pinned, in the snapshots.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, f"no {domain} entity registered for {unique_id!r}"
    return entity_id


def entity_id_for_key(hass: HomeAssistant, domain: str, key: str) -> str:
    """Return the entity id whose unique id ENDS in `key`.

    For the account-level entities, whose unique id is prefixed with a
    derived account key. A test that rebuilt that prefix itself would be
    asserting the implementation against a copy of the implementation, and
    would keep passing if both were wrong together.
    """
    registry = er.async_get(hass)
    matches = [
        entry.entity_id
        for entry in registry.entities.values()
        if entry.platform == DOMAIN and entry.domain == domain and entry.unique_id.endswith(key)
    ]
    assert len(matches) == 1, f"expected one {domain} entity ending in {key!r}, got {matches}"
    return matches[0]


async def setup_platforms(
    hass: HomeAssistant, platforms: list[Platform], **entry_kwargs: Any
) -> MockConfigEntry:
    """Load a config entry with ONLY `platforms` forwarded."""
    entry_kwargs.setdefault("unique_id", ENTRY_UNIQUE_ID)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, **entry_kwargs)
    entry.add_to_hass(hass)
    with patch("custom_components.alwaysfull.PLATFORMS", platforms):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def setup_platform(
    hass: HomeAssistant, platform: Platform, **entry_kwargs: Any
) -> MockConfigEntry:
    """Load a config entry with ONLY `platform` forwarded.

    `snapshot_platform` asserts a single platform is loaded, and keeping
    each platform's test to its own platform means a snapshot diff names
    the file that caused it.
    """
    return await setup_platforms(hass, [platform], **entry_kwargs)


def _configure(
    client: FakeAlwaysFullClient,
    session: Any,
    *,
    token: str = "",
    tz_offset_hours: int | None = None,
) -> FakeAlwaysFullClient:
    """Record the constructor arguments the integration used on the fake."""
    client.session = session
    client.token = token
    client.tz_offset_hours = tz_offset_hours
    client.constructed_tz_offset_hours = tz_offset_hours
    return client
