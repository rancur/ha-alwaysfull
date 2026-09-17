"""Keep `FakeAlwaysFullClient` honest against the real `AlwaysFullClient`.

Every HA-level test in this suite runs against the fake, so if `api.py`
renames a method or changes a parameter, those tests stay green while the
integration is broken in production. These checks are cheap insurance
against exactly that drift: they compare what the fake claims to implement
with what the real client actually offers.

They deliberately compare parameter NAMES, KINDS and DEFAULTS but not
annotations -- the fake types its session as `Any` because it never touches
aiohttp, and failing on that would be noise, not signal.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from custom_components.alwaysfull.api import AlwaysFullClient

from .conftest import DEVICE_ID, FakeAlwaysFullClient, load_fixture_data

# Every endpoint the fake stands in for.
FAKE_METHODS = (
    "login",
    "device_list",
    "device_config",
    "drinking_log",
    "notify_log",
    "notify_config",
    # Writers. These matter more than the readers, not less: the device-id
    # parameter name is NOT uniform across them (`devNo` for the config
    # writers, `deviceId` for `set_units`), and the write platforms reach
    # them only through the fake. A renamed or re-ordered parameter in
    # `api.py` would leave every write test green against a client that no
    # longer exists.
    "save_notify_config",
    "set_flush_config",
    "set_sleep_config",
    "set_filter_config",
    "set_maintenance_config",
    "set_water_config",
    "set_log_config",
    "set_units",
    "set_device_type",
    "reset_filter",
)


def _params(func: Any) -> list[tuple[str, inspect._ParameterKind, Any]]:
    """Return (name, kind, default) per parameter, ignoring annotations."""
    return [
        (p.name, p.kind, p.default) for p in inspect.signature(func).parameters.values()
    ]


def test_fake_covers_every_method_the_coordinator_uses() -> None:
    """The list below must not silently fall behind the fake's own surface."""
    public = {
        name
        for name, value in vars(FakeAlwaysFullClient).items()
        if inspect.iscoroutinefunction(value) and not name.startswith("_")
    }
    assert public == set(FAKE_METHODS)


@pytest.mark.parametrize("name", FAKE_METHODS)
def test_fake_method_matches_the_real_client(name: str) -> None:
    """Catch a rename or a changed parameter list in `api.py`."""
    real = getattr(AlwaysFullClient, name, None)
    assert real is not None, f"AlwaysFullClient has no {name!r} -- the fake is stale"

    fake = getattr(FakeAlwaysFullClient, name)
    assert inspect.iscoroutinefunction(real), f"{name} is no longer a coroutine function"
    assert inspect.iscoroutinefunction(fake)
    assert _params(fake) == _params(real), f"{name} signature drifted from the real client"


def test_fake_constructor_matches_the_real_client() -> None:
    """The fake is instantiated by the same call `__init__.py` makes."""
    real = _params(AlwaysFullClient.__init__)
    fake = _params(FakeAlwaysFullClient.__init__)

    # Names and kinds must match exactly; `session`'s default is allowed to
    # differ (the fake defaults it to None so tests can build one bare).
    assert [(name, kind) for name, kind, _default in fake] == [
        (name, kind) for name, kind, _default in real
    ]
    assert [(name, default) for name, _kind, default in fake if name != "session"] == [
        (name, default) for name, _kind, default in real if name != "session"
    ]


async def test_fake_returns_the_real_clients_response_shapes() -> None:
    """Guard the vendor's shape asymmetries the coordinator depends on.

    `device_list` and `notify_log` return pagination envelopes while
    `drinking_log` returns a BARE LIST. The real client passes the
    envelope's `data` member through untouched, so the committed live
    captures ARE the real client's return shapes -- comparing the fake
    against them is a real equivalence check, not a restatement.
    """
    client = FakeAlwaysFullClient()

    listed = await client.device_list()
    assert isinstance(listed, dict), "device_list returns a pagination envelope"
    assert isinstance(listed["data"], list)
    assert isinstance(load_fixture_data("device_list"), dict)

    log = await client.drinking_log(DEVICE_ID, 1, "2026-09-16", "2026-09-16")
    assert isinstance(log, list), "drinking_log returns a BARE list, not an envelope"
    assert isinstance(load_fixture_data("drinking_log"), list)

    notified = await client.notify_log(DEVICE_ID)
    assert isinstance(notified, dict), "notify_log returns a pagination envelope"
    assert isinstance(notified["data"], list)
    assert isinstance(load_fixture_data("notify_log"), dict)

    config = await client.device_config(DEVICE_ID)
    assert isinstance(config, dict)
    assert set(load_fixture_data("device_config")) <= set(config)
