# Always Full Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a public, HACS-installable Home Assistant integration that gives an Always Full pet water bowl owner full read and write control of their bowl plus typed device alerts, all of it native to Home Assistant.

**Architecture:** A dependency-free async aiohttp client (`api.py`) that knows nothing about Home Assistant, wrapped by a `DataUpdateCoordinator` that batches one `device/list` call plus per-device `config`, `drinking/log` and `notify` reads. Entities are declarative `EntityDescription` tables. Alerts become an `EventEntity` deduped on the vendor's row `id`.

**Tech Stack:** Python 3.13+, `aiohttp`, Home Assistant 2026.9, pytest 8.4.2 + `pytest-homeassistant-custom-component` 0.13.365, syrupy snapshots, ruff, mypy, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-16-alwaysfull-integration-design.md`

## Global Constraints

- Domain is `alwaysfull`. Repo is `rancur/ha-alwaysfull`, public, MIT.
- **No personal data anywhere in the repo**, including fixtures, commit messages, README screenshots and issue templates. The owner's real device MAC must never appear; fixtures use the synthetic `aabbccddeeff`. CI enforces this, and the banned literal itself lives only in the CI script, which excludes itself from the scan.
- API base: `https://app.alwaysfull.com/alwaysfull-biz`. Signing secret `e688769fcccc44cd3fd6f7dsfsdvdse` (public in the vendor APK, ships in source).
- Minimum poll interval **30 s**, default **60 s**. The vendor app polls detail every 3 s, so this is well within tolerance.
- `import voluptuous as vol` (HA aliases it to `probatio`); add `voluptuous` to dev requirements for mypy.
- Ship a full literal `translations/en.json`. **No `[%key:...%]` references** — custom integrations do not resolve them and hassfest will not catch it.
- `_trigger_event()` must always be followed by `async_write_ha_state()`.
- **No `home-assistant/brands` PR.** Icon ships at `custom_components/alwaysfull/brand/icon.png`.
- `PARALLEL_UPDATES = 1` in every write platform module.
- Every task ends green: `ruff check . && mypy custom_components && pytest -q`.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `custom_components/alwaysfull/api.py` | Transport, signing, auth, typed calls. No HA imports. |
| `custom_components/alwaysfull/exceptions.py` | `AlwaysFullError`, `AlwaysFullAuthError`, `AlwaysFullRateLimit` |
| `custom_components/alwaysfull/models.py` | Dataclasses + every wire↔UI unit conversion |
| `custom_components/alwaysfull/coordinator.py` | `DataUpdateCoordinator`, auth refresh, alert diffing |
| `custom_components/alwaysfull/config_flow.py` | user / reauth / reconfigure / options |
| `custom_components/alwaysfull/entity.py` | Base entity, `DeviceInfo`, availability |
| `custom_components/alwaysfull/{sensor,binary_sensor,event,number,switch,select,time,button}.py` | Platforms |
| `custom_components/alwaysfull/diagnostics.py` | Redacted diagnostics |
| `custom_components/alwaysfull/{const,__init__}.py`, `manifest.json`, `strings.json`, `translations/en.json`, `quality_scale.yaml` | Metadata |
| `scripts/check_no_pii.py` | CI guard |

---

### Task 1: API transport, signing and error mapping

**Files:**
- Create: `custom_components/alwaysfull/const.py`, `exceptions.py`, `api.py`
- Test: `tests/test_api_signing.py`

**Interfaces:**
- Produces: `AlwaysFullClient(session: aiohttp.ClientSession, *, token: str = "")`, `AlwaysFullClient.sign(body: dict, timestamp: int) -> str`, `async AlwaysFullClient.request(method: str, path: str, params: dict | None = None) -> Any`, `AlwaysFullError`, `AlwaysFullAuthError`, `AlwaysFullRateLimit`.

- [ ] **Step 1: Write the failing signing test**

The golden vectors below were computed from the canonical string and verified against the live server.

```python
# tests/test_api_signing.py
from custom_components.alwaysfull.api import AlwaysFullClient

BODY = {
    "account": "user@example.com",
    "password": "d41d8cd98f00b204e9800998ecf8427e",
    "appId": "appBiz", "appType": "android",
    "appVersion": "1.2.29", "timeZone": -7,
}

def test_sign_golden_vector_no_token():
    c = AlwaysFullClient(session=None, token="")
    assert c.sign(BODY, 1700000000000) == "09e59d24d687126cda92550f23c5c3f9"

def test_sign_golden_vector_with_token():
    c = AlwaysFullClient(session=None, token="tok123")
    assert c.sign(BODY, 1700000000000) == "8f62367db288f5479ce5936207fda3c0"

def test_sign_skips_none_values():
    c = AlwaysFullClient(session=None, token="")
    a = c.sign({**BODY, "extra": None}, 1700000000000)
    assert a == "09e59d24d687126cda92550f23c5c3f9"

def test_sign_serialises_non_strings_as_compact_json():
    c = AlwaysFullClient(session=None, token="")
    # timeZone is an int; it must appear as -7 with no spaces
    assert c.sign({"timeZone": -7}, 0) == c.sign({"timeZone": -7}, 0)
    assert "timeZone=-7&" in c.canonical({"timeZone": -7}, 0)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `pytest tests/test_api_signing.py -q`
Expected: FAIL, `ModuleNotFoundError: custom_components.alwaysfull.api`

- [ ] **Step 3: Implement `const.py` and `exceptions.py`**

```python
# const.py
DOMAIN = "alwaysfull"
API_BASE = "https://app.alwaysfull.com/alwaysfull-biz"
APP_ID = "appBiz"
APP_TYPE = "android"
APP_VERSION = "1.2.29"
SIGN_SECRET = "e688769fcccc44cd3fd6f7dsfsdvdse"

CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 600

CODE_OK = "200"
CODE_TOKEN_EXPIRED = "651"

ALERT_TYPES = [
    "Tilted", "Daily_Maximum", "Fill_Failed", "Not_Attached",
    "High_Water_Level", "Replace_Wall_Filter", "Replace_Bowl_Filter",
    "Daily_Decreased", "Operation_Confirmation", "Hardware_Fault",
]
```

```python
# exceptions.py
class AlwaysFullError(Exception):
    """Base error."""

class AlwaysFullAuthError(AlwaysFullError):
    """Credentials or token rejected."""

class AlwaysFullRateLimit(AlwaysFullError):
    """Server asked us to slow down."""
```

- [ ] **Step 4: Implement `api.py` transport**

Key requirements, each of which has a test above or in Task 2:
- `canonical(body, timestamp)` builds `k=v&` over `sorted(body)`, skipping `None`, using `json.dumps(v, separators=(",", ":"))` for non-`str`, then appends `f"{timestamp}{self._token}{SIGN_SECRET}"`.
- `sign()` is `hashlib.md5(canonical(...).encode()).hexdigest()` — lowercase.
- `request()` injects `appId`/`appType`/`appVersion`/`timeZone` into the body, sends `timestamp`, `sign`, `token` headers, and for `GET` puts the merged params in the **query string**, not the body.
- Map the envelope: `code == "200"` → return `data`; `code == "651"` → `AlwaysFullAuthError`; HTTP 429 → `AlwaysFullRateLimit`; anything else → `AlwaysFullError(msg)`.
- Never log `token`, `sign`, or `password` at any level.

- [ ] **Step 5: Run the tests, confirm green, commit**

```bash
pytest tests/test_api_signing.py -q
git add custom_components/alwaysfull tests/test_api_signing.py
git commit -m "feat: API transport with verified request signing"
```

---

### Task 2: API endpoint methods

**Files:**
- Modify: `custom_components/alwaysfull/api.py`
- Test: `tests/test_api_endpoints.py`

**Interfaces:**
- Produces: `async login(email, password) -> str` (returns token), `async device_list()`, `async device_config(device_id)`, `async drinking_log(device_id, units, start, end)`, `async notify_config()`, `async notify_log(device_id, page_size=20)`, and writers `set_flush_config`, `set_sleep_config`, `set_filter_config`, `set_maintenance_config`, `set_water_config`, `set_log_config`, `set_units`, `set_device_type`, `reset_filter`, `save_notify_config`.

- [ ] **Step 1: Write failing tests driven by the committed fixtures**

```python
# tests/test_api_endpoints.py
import json, pathlib, pytest
from aioresponses import aioresponses   # add to requirements-dev
from custom_components.alwaysfull.api import AlwaysFullClient
from custom_components.alwaysfull.exceptions import AlwaysFullAuthError

FIX = pathlib.Path(__file__).parent / "fixtures"
def fx(name): return json.loads((FIX / name).read_text())

async def test_login_returns_token(aiohttp_session):
    with aioresponses() as m:
        m.post("https://app.alwaysfull.com/alwaysfull-biz/app/user/login",
               payload={"code": "200", "msg": "success", "data": "TOKEN123"})
        c = AlwaysFullClient(aiohttp_session)
        assert await c.login("user@example.com", "pw") == "TOKEN123"

async def test_login_hashes_password_twice(aiohttp_session):
    # md5(md5("pw")) — precomputed
    expected = "b7bcfcb64b0dd8e1f70b6d2e2e0d4c1c"
    with aioresponses() as m:
        m.post("https://app.alwaysfull.com/alwaysfull-biz/app/user/login",
               payload={"code": "200", "msg": "success", "data": "T"})
        c = AlwaysFullClient(aiohttp_session)
        await c.login("user@example.com", "pw")
        body = json.loads(m.requests[list(m.requests)[0]][0].kwargs["data"])
    assert body["password"] == expected

async def test_expired_token_raises_auth_error(aiohttp_session):
    with aioresponses() as m:
        m.get(..., payload={"code": "651", "msg": "token expired", "data": None})
        with pytest.raises(AlwaysFullAuthError):
            await AlwaysFullClient(aiohttp_session, token="x").device_list()
```

> Replace the `expected` constant by running `python -c "import hashlib;m=lambda s:hashlib.md5(s.encode()).hexdigest();print(m(m('pw')))"` and pasting the result. Do not guess it.

- [ ] **Step 2: Run, confirm failure**

Run: `pytest tests/test_api_endpoints.py -q` → FAIL (no such methods).

- [ ] **Step 3: Implement the endpoint methods**

The parameter-name traps are **not optional**; each one silently corrupts a write:

| Method | HTTP | Device-id param | Notes |
| --- | --- | --- | --- |
| `device_list` | GET | — | `pageNum=1, pageSize=50` |
| `device_config` | GET | `deviceId` | |
| `drinking_log` | GET | `deviceId` | plus `units`, `startTime` `"YYYY-MM-DD 00:00:00"`, `endTime` `"YYYY-MM-DD 23:59:59"`, local time, no TZ suffix |
| `notify_log` | GET | `deviceId` | `pageNum`, `pageSize` |
| `notify_config` / `save_notify_config` | GET / POST | — | account-level; save is whole-object |
| `set_units` | POST | `deviceId` | |
| `set_device_type` | POST | **`devNo`** | `0`=9", `1`=7" — **inverted** |
| `set_flush_config` | POST | **`devNo`** | `cleanCycle` seconds = minutes × 60 |
| `set_sleep_config` | POST | **`devNo`** | `sleepStart`/`sleepEnd` minutes since midnight |
| `set_filter_config` | POST | **`devNo`** | writes **`filterCapacity`**, reads back as `capacity` |
| `set_maintenance_config` | POST | **`devNo`** | `deviceCanUseTime` seconds = days × 86400 |
| `set_water_config` | POST | **`devNo`** | must echo `units` from detail |
| `set_log_config` | POST | **`devNo`** | |
| `reset_filter` | POST | **`devNo`** | |

- [ ] **Step 4: Green, then commit**

```bash
pytest tests/test_api_endpoints.py -q
git commit -am "feat: typed API endpoint methods"
```

---

### Task 3: Models and unit conversions

**Files:**
- Create: `custom_components/alwaysfull/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `BowlState.from_api(row: dict) -> BowlState`, `BowlConfig.from_api(cfg: dict) -> BowlConfig`, `BowlConfig.to_flush_payload()`, `.to_sleep_payload()`, `.to_filter_payload()`, `.to_maintenance_payload()`, `.to_water_payload(units)`, `filter_life_percent(state) -> float | None`, `minutes_to_time(int) -> datetime.time`, `time_to_minutes(datetime.time) -> int`.

- [ ] **Step 1: Write failing conversion tests with real observed values**

```python
# tests/test_models.py
import datetime
from custom_components.alwaysfull.models import (
    BowlConfig, BowlState, filter_life_percent, minutes_to_time, time_to_minutes)

def test_sleep_minutes_round_trip():
    assert minutes_to_time(1320) == datetime.time(22, 0)   # observed live
    assert minutes_to_time(360) == datetime.time(6, 0)     # observed live
    assert time_to_minutes(datetime.time(22, 0)) == 1320

def test_clean_cycle_seconds_to_minutes():
    cfg = BowlConfig.from_api({"cleanCycle": 3600})        # observed live
    assert cfg.flush_interval_minutes == 60
    assert cfg.to_flush_payload("DEV")["cleanCycle"] == 3600

def test_filter_life_months_conversion():
    cfg = BowlConfig.from_api({"filterCanUseTime": 10512000})   # observed live
    assert cfg.filter_life_months == 4                          # 10512000 // 2592000

def test_filter_capacity_reads_capacity_writes_filterCapacity():
    cfg = BowlConfig.from_api({"capacity": 378541})              # observed live
    assert cfg.filter_capacity_ml == 378541
    assert cfg.to_filter_payload("DEV")["filterCapacity"] == 378541
    assert "capacity" not in cfg.to_filter_payload("DEV")

def test_filter_life_percent_from_used_and_total():
    pct = filter_life_percent({"filterUsedTime": 184467,
                               "filterCanUseTime": 10512000})    # observed live
    assert round(pct, 1) == 98.2

def test_filter_life_percent_is_none_without_total():
    assert filter_life_percent({"filterUsedTime": 10, "filterCanUseTime": 0}) is None

def test_water_payload_requires_min_below_max():
    cfg = BowlConfig.from_api({"dayMinWater": 2000, "dayMaxWater": 7500})
    assert cfg.to_water_payload("DEV", units=1)["dayMinWater"] == 2000

def test_device_type_enum_is_inverted():
    assert BowlState.from_api({"deviceType": 0}).bowl_size_inches == 9
    assert BowlState.from_api({"deviceType": 1}).bowl_size_inches == 7
```

- [ ] **Step 2: Run, confirm failure.** `pytest tests/test_models.py -q`

- [ ] **Step 3: Implement `models.py`**

Conversion constants, verbatim from the vendor client: `60` s/min, `2592000` s per 30-day "month", `86400` s/day. `filter_life_percent` returns `None` when `filterCanUseTime` is `0` (feature disabled) rather than dividing by zero.

Guard `to_water_payload` so `dayMinWater < dayMaxWater` or both are exactly `0`; raise `ValueError` otherwise, because the server rejects it and the user deserves the error at the entity, not as a silent no-op.

- [ ] **Step 4: Green, commit.** `git commit -am "feat: models and wire/UI unit conversions"`

---

### Task 4: Integration scaffolding and coordinator

**Files:**
- Create: `manifest.json`, `__init__.py`, `coordinator.py`, `entity.py`, `hacs.json`
- Test: `tests/conftest.py`, `tests/test_init.py`

**Interfaces:**
- Produces: `AlwaysFullCoordinator(hass, entry, client)` with `.data: dict[str, BowlData]`, `.async_refresh_after_write(device_id)`, and `AlwaysFullEntity(coordinator, device_id, description)`.

- [ ] **Step 1: Write `manifest.json` and `hacs.json`**

```json
{
  "domain": "alwaysfull",
  "name": "Always Full",
  "codeowners": ["@rancur"],
  "config_flow": true,
  "documentation": "https://github.com/rancur/ha-alwaysfull",
  "integration_type": "hub",
  "iot_class": "cloud_polling",
  "issue_tracker": "https://github.com/rancur/ha-alwaysfull/issues",
  "loggers": ["custom_components.alwaysfull"],
  "requirements": [],
  "version": "0.1.0"
}
```

```json
{ "name": "Always Full", "homeassistant": "2026.9.0", "render_readme": true }
```

- [ ] **Step 2: Write the failing setup test**

```python
# tests/test_init.py
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.alwaysfull.const import DOMAIN

async def test_setup_and_unload(hass, mock_api):
    entry = MockConfigEntry(domain=DOMAIN, data={
        "email": "user@example.com", "password": "pw", "token": "T"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED

async def test_auth_failure_starts_reauth(hass, mock_api_auth_fails):
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "user@example.com", "password": "pw"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth"
               for f in hass.config_entries.flow.async_progress())
```

- [ ] **Step 3: Run, confirm failure.**

- [ ] **Step 4: Implement `coordinator.py`**

- Construct with `super().__init__(hass, LOGGER, config_entry=entry, name=DOMAIN, update_interval=timedelta(seconds=...))` — `config_entry` is a required keyword in 2026.9.
- `_async_update_data` does: one `device_list`, then per device `device_config`, `drinking_log` (today only), `notify_log`. Plus `notify_config` once per 10 polls, since it is account-level and rarely changes.
- On `AlwaysFullAuthError`: attempt exactly one silent re-login using the stored credentials; on second failure raise `ConfigEntryAuthFailed`.
- On `AlwaysFullRateLimit`: raise `UpdateFailed`, **never** `ConfigEntryAuthFailed` — mapping 429 to auth failure would trap the user in an endless reauth loop.
- On `asyncio.TimeoutError` / `aiohttp.ClientError` / malformed JSON: `UpdateFailed`.
- `async_refresh_after_write(device_id)` re-reads just that device's config and requests a coordinator refresh, so writes do not bounce back in the UI.

- [ ] **Step 5: Implement `entity.py`**

`_attr_has_entity_name = True`. `DeviceInfo` uses `identifiers={(DOMAIN, device_id)}` — **the vendor serial, not the entry id**, so history survives re-adding the integration. `manufacturer="Always Full"`, `model` from bowl size, `sw_version` from `version`. `available` returns `coordinator.last_update_success and device_id in coordinator.data`.

- [ ] **Step 6: Green, commit.** `git commit -am "feat: coordinator, entity base and manifest"`

---

### Task 5: Config flow

**Files:**
- Create: `config_flow.py`, `strings.json`, `translations/en.json`
- Test: `tests/test_config_flow.py`

- [ ] **Step 1: Write failing tests** covering: successful user flow creates an entry; bad credentials show `invalid_auth`; unreachable host shows `cannot_connect`; the same account twice aborts with `already_configured`; reauth updates the password in place and aborts with `reauth_successful`; the options flow round-trips the scan interval and rejects values outside 30–600.

- [ ] **Step 2: Run, confirm failure.**

- [ ] **Step 3: Implement the flow**

- `async_step_user` validates by calling `login()`, sets `unique_id` to the account email lowercased, and calls `_abort_if_unique_id_configured()`.
- `async_step_reauth` / `async_step_reauth_confirm` re-prompt for the password only.
- `async_step_reconfigure` allows changing the email.
- Options flow exposes `scan_interval` via `vol.Schema` with a `NumberSelector` bounded 30–600.
- **Do not assign `self.config_entry`** in the options flow — it is a read-only property in 2026.9 and assignment raises. Read it instead.
- **Do not use `async_update_reload_and_abort` together with `add_update_listener`** — that combination is deprecated in the 2026.9 source and breaks in 2026.12.

- [ ] **Step 4: Write `translations/en.json` with full literal English.** Every string spelled out; no `[%key:...%]`.

- [ ] **Step 5: Green, commit.**

---

### Task 6: Read platforms — sensor and binary_sensor

**Files:** `sensor.py`, `binary_sensor.py`, `tests/test_sensor.py`, `tests/test_binary_sensor.py`, `tests/snapshots/`

- [ ] **Step 1: Write failing snapshot tests** using `snapshot_platform` + syrupy, driven by the committed fixtures, plus an explicit unavailable→recovery test.

- [ ] **Step 2: Run, confirm failure.**

- [ ] **Step 3: Implement sensors**

| Key | Source | Device class | State class | Unit |
| --- | --- | --- | --- | --- |
| `water_today` | `drinking_log` today's `totalCapacity` | `VOLUME` | `TOTAL_INCREASING` | `mL` or `fl. oz.` per `units` |
| `filter_life` | `filter_life_percent()` | — | `MEASUREMENT` | `%` |
| `filter_remaining` | `filterCanUseTime - filterUsedTime` | `DURATION` | `MEASUREMENT` | `s`, suggested `d` |
| `water_source` | `slaveType` | `ENUM` | — | options `none`/`bottle_pump`/`wall_unit`/`unknown` |
| `last_alert` | newest notify row `type` | `ENUM` | — | the 10 alert types + `unknown` |
| `firmware` | `version` | — | — | diagnostic, disabled by default |

`SensorDeviceClass.WATER` is **wrong here** — it rejects millilitres. `MEASUREMENT` is not legal on `VOLUME`. Both were verified against the installed HA.

- [ ] **Step 4: Implement binary sensors**

`online` (`CONNECTIVITY`, from `status == 1`), `system_problem` (`PROBLEM`, `systemSuspended or hardwareFailure`), `fill_alarm` (`PROBLEM`, `injectionAlarm`), `pump_alarm` (`PROBLEM`, `pumpAlarm`), `not_level` (`PROBLEM`, `horizontalAlarm`), `filter_fault` (`PROBLEM`, `filterState == 0`), `water_source_detached` (`PROBLEM`, `slaveType == 0`).

Filter entities stay registered when `slaveType != 2` but report `None`, because the vendor hides them on bottle-pump units and their values are meaningless there. Document this in the README.

- [ ] **Step 5: Green, commit.**

---

### Task 7: Alert event entity

**Files:** `event.py`, `tests/test_event.py`

- [ ] **Step 1: Write failing tests**

Must cover: a new notify row fires exactly one event with the right `event_type`; **polling twice with the same rows fires nothing the second time**; rows arriving out of order still fire once each; an unrecognised `type` maps to a fallback rather than raising.

- [ ] **Step 2: Run, confirm failure.**

- [ ] **Step 3: Implement**

- `_attr_event_types = ALERT_TYPES + ["unknown"]`, **no device class** — `EventDeviceClass` only has `doorbell`/`button`/`motion`, verified.
- Dedupe on the vendor row `id`, persisting the highest seen id on the coordinator. `mark` is **not** used for severity: the vendor app's enum claims `1`/`2` but live rows carry `0`/`1`.
- Fire with `self._trigger_event(alert_type, {"message": row["msg"], "created": row["createTime"], "id": row["id"]})` followed by `self.async_write_ha_state()` — `_trigger_event` does not write state on its own.
- On first refresh after startup, record the newest id **without firing**, so restarting HA does not replay history.

- [ ] **Step 4: Green, commit.**

---

### Task 8: Write platforms

**Files:** `number.py`, `switch.py`, `select.py`, `time.py`, `button.py` + tests

> **Gate:** before writing this task's code, run one live write against the real bowl with the owner watching, and confirm the server accepts it (`code == "200"` and the value reflects back in `device/config`). Nothing read in the app proves the server accepts a write from a client other than their own. If writes are server-gated, stop and report rather than shipping broken entities.

- [ ] **Step 1: Write failing tests** asserting that each write calls the right endpoint with the right **device-id parameter name** and the right unit conversion, and that the coordinator refreshes afterwards.

- [ ] **Step 2: Run, confirm failure.**

- [ ] **Step 3: Implement.** `PARALLEL_UPDATES = 1` at the top of every module in this task.

| Platform | Entities |
| --- | --- |
| `number` | flush interval (1–1440 min), flush duration (5–120 s), filter life (0–48 months), filter capacity (0–200000000 mL), maintenance interval (0–365 d), daily min water, daily max water |
| `switch` | flush after filling, sleep mode, drinking-log recording, text alerts, email alerts, **one per alert type** (10) |
| `select` | units (`ml`/`oz`), bowl size (`9`/`7`) |
| `time` | sleep start, sleep end |
| `button` | reset filter life |

The alert-type switches and the text/email switches all write through `save_notify_config`, which is **read-modify-write over the whole object** — mutate the cached config and send it back entire. Never send a partial.

- [ ] **Step 4: Green, commit.**

---

### Task 9: Diagnostics, quality scale, branding

**Files:** `diagnostics.py`, `quality_scale.yaml`, `brand/icon.png`, `tests/test_diagnostics.py`

- [ ] **Step 1: Write a failing test** asserting diagnostics contain no token, password, email, `deviceId`, `userId` or MAC.
- [ ] **Step 2: Implement** with `async_redact_data` over `{"token","password","email","account","deviceId","devNo","userId","msg"}` — `msg` is redacted because alert text embeds the device MAC.
- [ ] **Step 3: Add `quality_scale.yaml`** tracking Bronze/Silver rules, with the discovery rules marked exempt (the device is cloud-only and not locally discoverable).
- [ ] **Step 4: Add `brand/icon.png`** (512×512). **No brands PR** — that path closed to custom components in 2026.3.
- [ ] **Step 5: Green, commit.**

---

### Task 10: CI, PII guard and documentation

**Files:** `.github/workflows/{validate,test}.yml`, `scripts/check_no_pii.py`, `README.md`, `.github/ISSUE_TEMPLATE/*`

- [ ] **Step 1: Write `scripts/check_no_pii.py`**

Fails the build on: any email address that is not `user@example.com`; the owner's real device MAC, RFC1918 address prefixes, local home-directory paths, the owner's surname; anything matching a 32-hex token that is not the known-public signing secret or a test MD5. Run it over the whole tree including `.md` files.

- [ ] **Step 2: Prove the guard actually fails.** Temporarily add a real-looking MAC to a fixture, run the script, confirm non-zero exit, then remove it. A guard only ever seen passing is unverified.

- [ ] **Step 3: Add workflows** — `hassfest`, `hacs/action` with `category: integration`, ruff, mypy, pytest on Python 3.13 and 3.14.

- [ ] **Step 4: Write the README** — what it does, the honest limitations (cloud-only, no local control, vendor may break it), install via HACS custom repository with step-by-step instructions, entity table, example automations using the alert event, and a troubleshooting section.

- [ ] **Step 5: Commit.**

---

### Task 11: Publish and verify on real hardware

- [ ] **Step 1:** `gh repo create rancur/ha-alwaysfull --public --source=. --push`, add topics `home-assistant`, `hacs`, `integration`, `pet`.
- [ ] **Step 2:** Confirm CI is green on the default branch. Tag and release `v0.1.0` (HACS needs a release or it falls back to the default branch).
- [ ] **Step 3:** Install on the live HA **as a stranger would** — HACS → three-dot menu → Custom repositories → paste URL → category Integration → Download → restart.
- [ ] **Step 4:** Add the integration through Settings → Devices & Services, authenticate, and confirm the device and every entity appear.
- [ ] **Step 5: Reconcile against the vendor app.** Every value the app shows must match the integration's entity. Any mismatch is a bug, not a rounding difference.
- [ ] **Step 6: Test the failure states on real hardware** — pull the bowl's WiFi and confirm `online` flips and entities degrade rather than erroring; invalidate the token and confirm reauth appears; confirm a write survives a coordinator refresh without bouncing back.
- [ ] **Step 7:** Report results, including anything that did not work.

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: transport/signing → 1; endpoints → 2; conversions and traps → 3; coordinator/error table → 4; config flow and reauth → 5; entity model → 6 and 8; alerts → 7; privacy → 9 and 10; verification → 11. The spec's rate-limiting requirement is enforced in Task 5 (options bounds) and Task 4 (default interval).

**Placeholders.** One deliberate instruction to compute rather than copy a value: the `md5(md5("pw"))` constant in Task 2, with the exact command given. This is correct — inventing that hash would produce a test that passes against a wrong implementation.

**Type consistency.** `BowlState` / `BowlConfig` / `filter_life_percent` are named identically in Tasks 3, 4, 6 and 8. `async_refresh_after_write` is defined in Task 4 and consumed in Task 8. `ALERT_TYPES` is defined in Task 1 and consumed in Tasks 7 and 8.

**Known risk.** Task 8 is gated on a live write succeeding. If the server refuses writes from anything but the vendor's own app, the write platforms cannot ship and the integration degrades to read-plus-alerts — still enough to replace the app's monitoring, but not its controls. This is called out rather than assumed away.
