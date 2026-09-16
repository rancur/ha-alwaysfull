# Always Full — Home Assistant Integration Design

**Date:** 2026-09-16
**Status:** Approved (design), pending implementation plan
**Domain:** `alwaysfull`
**Repo:** `rancur/ha-alwaysfull` (public, MIT)

## Goal

An Always Full pet water bowl owner can uninstall the vendor app and lose nothing.
Full read and write control, device alerts surfaced as first-class Home Assistant
events, no vendor subscription required for anything the integration exposes.

Anything less than that is a partial success. A read-only sensor dump would be
easy and would not meet the goal.

## Background

The Always Full bowl is a WiFi water bowl built on a Bouffalo Lab BL602
(RISC-V, WiFi + BLE, 2.4 GHz). A full TCP port scan of the device (all 65,535
ports) found **zero open ports**. It is a pure outbound cloud client: there is
no local control path, and reflashing a BL602 is not a realistic alternative.
Any integration must therefore go through the vendor cloud.

The vendor's Android app (`com.alwaysfull.bowl` 1.2.29) is a uni-app / DCloud
hybrid, so its entire API layer is readable JavaScript. From it we recovered:

- Base URL `https://app.alwaysfull.com/alwaysfull-biz`
- Request signing: merge `appId` / `appType` / `appVersion` / `timeZone` into the
  body; build `k=v&` over keys sorted ascending (JSON for non-strings); append
  `{timestamp_ms}{token}{secret}`; MD5, lowercase hex; send as the `sign` header
  alongside `timestamp` and `token`.
- `password` = `md5(md5(plaintext))`, lowercase hex.
- Envelope `{code, msg, data}`; `"200"` success, `"651"` token expired.

The signing scheme was **verified live**: a signed request to `/app/user/login`
returned a business-logic error (`602 Invalid email address or password.`)
rather than a signature error, which proves the server accepted the signature.

The signing secret is hardcoded in the vendor's publicly distributed APK. It is
the vendor's secret, not a user's, and shipping it in this repo discloses
nothing that was not already public.

## Non-goals

- Local control. It is not possible; see above.
- Firmware modification or OTA interception.
- Reselling, proxying, or caching vendor data beyond what a single user's own
  Home Assistant needs.
- Working around the vendor's paid subscription where the server enforces it.
  If a capability is server-gated we surface the limitation honestly rather
  than attempting to defeat it.

## Architecture

```
custom_components/alwaysfull/
  __init__.py        entry setup/unload, platform forwarding
  api.py             standalone async client (aiohttp). No HA imports.
  models.py          typed dataclasses for API payloads
  coordinator.py     DataUpdateCoordinator, auth refresh, alert diffing
  config_flow.py     user / reauth / reconfigure / options
  entity.py          base entity, DeviceInfo, availability
  sensor.py  binary_sensor.py  button.py  select.py  number.py  switch.py
  event.py           device alerts as an event entity
  diagnostics.py     redacted
  const.py  manifest.json  strings.json  translations/en.json
  quality_scale.yaml
```

`api.py` deliberately has no Home Assistant imports. It can be exercised by
plain pytest without a Home Assistant test harness, which keeps the hard part
(signing, auth, error mapping) cheap to test.

### Data flow

1. Config flow takes email + password, calls `login`, stores both credentials
   and the returned token in the config entry.
2. `AlwaysFullCoordinator` polls on an interval (default 60 s, configurable
   30–600 s via options).
3. Each poll fetches device list/detail, today's drinking log, and the
   notification log in a single batched pass.
4. On `651`, the client transparently re-logs-in once. If that also fails the
   coordinator raises `ConfigEntryAuthFailed`, which triggers Home Assistant's
   native reauth flow.
5. New notification-log entries since the last poll are dispatched to the event
   entity.

### Entity model

| Platform | Entities |
| --- | --- |
| `sensor` | water consumed today, drink count today, last drink, filter life remaining, filter days remaining, firmware version (diagnostic), signal (diagnostic) |
| `binary_sensor` | online (`connectivity`), pump alarm (`problem`), filter due (`problem`), flushing (`running`) |
| `button` | reset filter, flush now |
| `select` / `number` / `switch` | flush interval, sleep schedule, units, notification toggles |
| `event` | device alert — one event entity carrying the vendor's notification types |

The exact field names, units and enum values behind each entity come from the
API contract document and are confirmed against a live response before the
entity ships. Entities are not written speculatively.

### Alerts

Will's stated priority. The vendor gates push notifications behind a
US$29.99/yr subscription. `/app/notify/getNotifyLog` is polled, new entries are
diffed against the previous poll, and each is emitted through an `event` entity
with the alert type as `event_type` and the vendor payload as attributes. That
gives automation users a trigger without the vendor's push service.

If the notification log itself turns out to be server-gated behind the
subscription, we say so plainly in the README rather than shipping an entity
that silently never fires.

## Error handling

| Condition | Behaviour |
| --- | --- |
| Token expired (`651`) | Silent re-login, retry once |
| Re-login fails | `ConfigEntryAuthFailed` → HA reauth flow |
| HTTP 5xx / timeout | `UpdateFailed`, coordinator backs off, entities go unavailable |
| Malformed JSON | `UpdateFailed` with the response logged at debug, never at info |
| Device offline | Entities remain, `available` reflects the device's own online flag |
| Unknown `code` | `UpdateFailed` carrying `msg`; never crash the setup |

Tokens, passwords and signatures are never logged at any level. Diagnostics are
redacted through `async_redact_data`.

## Rate limiting and vendor courtesy

A 60-second default poll against a small vendor's production API is roughly
what their own app generates while open. The floor is 30 s to stop users
hammering the vendor. Writes trigger a single targeted refresh, not a full
re-poll. Requests are serialised per config entry.

## Testing

- `pytest` + `pytest-homeassistant-custom-component`, mocked aiohttp.
- Signing has a golden-vector test: a fixed body and timestamp must produce a
  known MD5, so a refactor cannot silently break auth.
- **Failure states are tested, not just the happy path**: expired token, failed
  re-login, HTTP 500, timeout, malformed JSON, offline device, empty device
  list, and a notification log that grows between polls.
- Snapshot tests over the entity registry so entity IDs and attributes cannot
  drift unnoticed.
- CI: hassfest, HACS validation, ruff, mypy, pytest, plus a PII guard that
  greps the tree for real emails, MAC addresses, device IDs, tokens and local
  filesystem paths.

## Privacy

No real account identifiers appear anywhere in the repo, including test
fixtures, commit messages and issue templates. Fixtures use obviously synthetic
values. The PII guard runs in CI so this stays true.

## Open questions

1. The credential stored in 1Password does not authenticate (`652`). Live
   verification is blocked until that is resolved. Implementation and unit
   tests are not blocked.
2. Whether the notification log and the write endpoints are server-gated behind
   the subscription is unknown until we have a working session.
