# The Always Full cloud API

Everything here was recovered by reading the vendor's Android app
(`com.alwaysfull.bowl` 1.2.29, a uni-app/DCloud hybrid whose API layer is
readable JavaScript) and by probing the live production server with a real
account and a real bowl. None of it is published by the vendor. There is no
official documentation, no developer programme and no stability promise.

This document exists so that the next person does not have to do it again.

## How to read this document

Every claim carries one of two markers.

- **VERIFIED** — observed. Either a request was made against the live server
  and the response was read, or the behaviour was read directly out of the
  decompiled app. Where a number is quoted, that number was measured.
- **INFERRED** — a reasonable reading of the evidence that has *not* been
  confirmed. Treat every one of these as a hypothesis. Several of them are
  cheap to test and simply have not been tested.

Nothing else is asserted. Where something is unknown, this document says so
rather than filling the gap.

Examples use the synthetic device ids `aabbccddeeff` and `001122334455` and
the address `user@example.com`. They are not anybody's.

---

## PRIVACY: what this API will hand out

Read this before you write a single line against this API.

The session token is a key to the **account holder's personal data**, not just
to a water bowl. Three payloads carry it:

| Endpoint | What it discloses | Marker |
| --- | --- | --- |
| `/app/user/loginInfo` | `firstName`, `lastName`, `phone`, `countryCode`, `address1`, `address2`, `city`, `st`, `zip` — the account holder's **full name, phone number and complete postal address** — alongside `subscribe` | VERIFIED |
| `/app/pet/list` | The pets' names | VERIFIED |
| `/app/notify/getNotifyLog` | Each row's `msg` embeds the **device's MAC address** in plain text, e.g. `"Bowl aabbccddeeff is filling."` | VERIFIED |

The device id **is** the bowl's MAC address with the separators stripped
(VERIFIED), so every request you make carries a hardware identifier.

If you build on this API:

- **Do not log any of it.** Debug logs get attached to public issues wholesale.
- **Do not cache it.** There is no reason to hold a postal address to read a
  water bowl.
- **Do not export it.** Diagnostics files, crash reports and telemetry are all
  ways for this to end up somewhere public.
- If you need `subscribe`, extract *only* `subscribe` and `subscribeExpires` at
  the call site and let the rest of the response go out of scope. Do not hand
  the object to anything.

This integration does not call `/app/user/loginInfo` or `/app/pet/list` at all
(see the comment where `loginInfo` would live in
`custom_components/alwaysfull/api.py`), and it redacts the fields anyway, so
that adding the one useful field later cannot quietly publish the rest.

---

## 1. Transport

### Base URL

```
https://app.alwaysfull.com/alwaysfull-biz
```

VERIFIED. Every path below is relative to it.

### There is no local path. At all.

VERIFIED: the bowl is a Bouffalo Lab BL602 (RISC-V, 2.4 GHz WiFi + BLE), and a
full TCP scan across **all 65,535 ports** found **zero open ports**. It is a
pure outbound cloud client: it dials out and never listens.

There is therefore no LAN API, no local HTTP server, no mDNS service and no
local control path of any kind, and no amount of client-side work can create
one. Everything in this document goes through the vendor's cloud.

### Request signing

VERIFIED (read in the app, and confirmed live — a signed request that is
otherwise wrong returns a *business-logic* error rather than a signature
error, which proves the server accepted the signature).

1. Merge four fields into the request parameters:

   | Field | Value | Notes |
   | --- | --- | --- |
   | `appId` | `appBiz` | constant |
   | `appType` | `android` | constant |
   | `appVersion` | `1.2.29` | the app version being impersonated |
   | `timeZone` | integer UTC offset in **whole hours**, e.g. `-7` | **not cosmetic — see §2** |

2. Build the canonical string: iterate the merged parameters' keys **sorted
   ascending**, appending `k=v&` for each — including the last, so the string
   ends with a trailing `&` before step 3. Skip any key whose value is `None`
   **entirely** (it contributes nothing, not even an empty `k=&`). Render
   string values as-is; render everything else as **compact JSON** (no spaces
   after `,` or `:`).

3. Append `f"{timestamp_ms}{token}{secret}"`, where `timestamp_ms` is the
   current time in milliseconds, `token` is the current session token (the
   empty string before login) and `secret` is the vendor's signing constant.

4. Take the **MD5** of that string, lowercase hex. That is the signature.

Send three headers:

```
timestamp: <the same timestamp_ms used in step 3>
sign:      <32 lowercase hex characters>
token:     <the session token, empty before login>
```

On `POST`, the merged parameters are the JSON body. On `GET`, they go in the
**query string** — and are still exactly what is signed, so the query must
carry the merged `appId`/`appType`/`appVersion`/`timeZone` too, with non-string
values rendered the same compact-JSON way they were signed.

The signing secret is a constant compiled into the vendor's publicly
distributed APK — the same value in every installation on every phone. It is
the vendor's, not a user's, it grants nothing on its own, and it is reproduced
in `custom_components/alwaysfull/const.py` as `SIGN_SECRET`.

Two traps worth stating outright:

- **The signed dict and the sent dict must be byte-identical.** If you strip
  `None` values while signing but serialise them as JSON `null` when sending,
  every request fails with a signature error and nothing tells you why.
- **The `timestamp` header and the timestamp inside the digest must match.**
  Computing the digest and then re-reading the clock for the header is a race
  that fails roughly once in a thousand requests.

### Password hashing

VERIFIED: `password` is `md5(md5(plaintext))`, lowercase hex, computed
client-side. The plaintext never goes over the wire. This is obfuscation, not
security — the double digest *is* the credential, so anyone who intercepts it
can authenticate with it.

### Response envelope

Every response is `{"code": ..., "msg": ..., "data": ...}`. `code` is a
**string** in observed responses (`"200"`, not `200`); compare it as one.

| `code` | Meaning | Marker |
| --- | --- | --- |
| `200` | Success. The payload is in `data`. | VERIFIED |
| `651` | Token expired / rejected. Routine — see §1.5. | VERIFIED |
| `652` | **Endpoint-dependent.** From `/app/user/login`: the credentials were rejected (`"Invalid email address or password."`). From a token-bearing call: the SESSION was rejected — see below. | VERIFIED |
| `602` | The same, and the same split applies | VERIFIED (observed once on login, not reproducible on demand) |
| `603` | **Rate limited.** `"Too many requests, please try again later."` | VERIFIED |

**`652` does not distinguish a wrong password from an account that does not
exist.** VERIFIED: a real account with a deliberately wrong password and an
email address with no account behind it both return `652` with the identical
message. No client can tell those two cases apart, and none should try.

`602`'s exact meaning is unknown. What is certain is that it is a refusal to
authenticate; treating it as anything else strands the user on "unexpected
error" instead of "check your password".

**`652` MEANS TWO DIFFERENT THINGS, AND THE ENDPOINT IS WHAT DECIDES WHICH.**
VERIFIED, the hard way, on a live installation.

- From `/app/user/login` — the only call that authenticates with the
  email/password pair — it means that pair was rejected. Retrying cannot help.
  Send the user to reauthentication.
- From **any token-bearing call** it means the **session** was rejected, not the
  password. Against a single-session vendor (§1.5) that is routine, and it heals
  with exactly the one silent re-login `651` gets.

Classifying `652` by code alone, without regard to the endpoint, looks obviously
right and is not. What it does in production: a transient `652` on a poll skips
the re-login that would have healed it, every entity goes `unavailable` and
stays there for as long as nobody restarts anything, and the config entry
carries on reporting itself as loaded. The credentials are correct the whole
time. If the subsequent **login** then answers `652`, that is the real thing and
reauthentication is correct — which is why the relaxation costs nothing.

### 1.4 `603` IS THE RATE LIMIT, and it is the only one

**VERIFIED, and it is easy to get wrong because the signal is in the wrong
place.** The vendor rate-limits in the **envelope code**, under HTTP **200** —
not with HTTP `429`. Eight logins in the space of a few seconds answered:

```json
{"code": "603", "msg": "Too many requests, please try again later.", "data": null}
```

That is the measured trigger, not a documented one: roughly **eight logins in a
few seconds** is enough. Where the threshold actually sits, and whether reads
are counted on the same budget as logins, is unknown. Treat the number as
"a handful of logins in quick succession is too many".

`603` was previously recorded here as a generic *system error*, which is why
`/app/ota/check` is listed in §3.7 as answering it "consistently". Be careful
with that reading: `603` has **also** been observed from `/app/ota/check` and
from `/app/pay/get/product` on an account with no subscription, neither of
which is plausibly a rate limit. So either the vendor overloads one code, or
`603` means something broader like "temporarily unavailable". **The two
readings are not distinguishable from the client**, and they do not need to be:
back off and retry later is the correct response to both.

What must never happen is mapping `603` into the authentication family. The
credentials are fine, so a reauthentication prompt succeeds, the next request
is rate-limited again, and the user is trapped in a loop they cannot escape.

HTTP `429` has also been seen from the front door and arrives without a JSON
envelope. Handle it at the transport layer too — it costs nothing and another
deployment may answer that way — but do not expect it: every rate limit
observed from *this* service arrived as `603`.

### 1.5 THE VENDOR IS SINGLE-SESSION

**VERIFIED, and this is the single most important operational fact in this
document.** The account holds exactly one valid token at a time:

1. Log in. Token A answers `200`.
2. Log in again. Token B is minted.
3. Token A now answers `651 "token expiration"`. Token B answers `200`.

The second login invalidates the first **immediately**. There is no grace
period and no per-device session.

The consequence for anything long-running: **the owner opening the vendor's
phone app signs your client out.** That is not an error condition or a sign of
anything broken — it is an ordinary person using their own bowl. `651` is
therefore a *routine* event, not a once-in-days expiry.

Any client must be able to re-login and retry. Two rules for doing it safely:

- **Exactly one re-login per operation, never a loop.** Two clients that each
  retry on `651` will sign each other out for ever, as fast as the network
  allows, and neither will ever make progress.
- **`651` gets a retry, and so does a `652` that did not come from the login
  call.** Both mean the session is dead, and both heal the same way. A `652`
  from the **login** does not get one: the server has just said the stored
  email/password pair is wrong. Re-sending the same pair cannot succeed; it
  only spends requests against the vendor.

---

## 2. `timeZone` is not cosmetic

**This is the most important undocumented behaviour in the API, and it fails
silently.**

`/app/drinking/log` takes naive `"YYYY-MM-DD HH:MM:SS"` datetime strings with
no zone and no offset. **The server localises them using the `timeZone` value
in the signed body.** The timestamps are not absolute.

VERIFIED, with one identical window and nothing changed but `timeZone`:

| `timeZone` | Reported total |
| --- | --- |
| `-7` | 1028 mL |
| `0` | 1489 mL |
| `+9` | 1086 mL |

A **45% swing** on the same request. Anyone who assumes the timestamps are
absolute — or who lets the value default to whatever the host process's `TZ`
happens to be — gets wrong daily totals, with no error, no warning and nothing
in the response to suggest anything is off.

Day boundaries follow the declared zone too. VERIFIED, with `timeZone=-7`:

- the midnight-local hour reads **0 mL**;
- the 6am-local hour reads **250 mL**;
- the server's own `Daily_Decreased` job fires at **07:00 UTC**, which is local
  midnight in that zone.

INFERRED: the server stores each drinking event with an absolute timestamp and
buckets on read, which is why the same underlying data re-buckets cleanly under
a different `timeZone`. Nothing observed contradicts this, but the storage side
has not been seen.

Practical guidance:

- Send the zone your **totals should be expressed in**, and send it on every
  request.
- Re-read it periodically rather than caching it at startup: a zone that
  observes DST changes offset twice a year, and a client that signed in last
  November will be an hour out all summer.
- Beware the container case. A HAOS or Docker host commonly runs `TZ=UTC` while
  the application is configured for the user's real zone. Reading the
  *process's* offset in that situation is precisely the silent-wrong-answer bug
  above. (This integration reads Home Assistant's configured zone, not the OS
  clock — see `ha_utc_offset_hours()` in `coordinator.py`.)
- Half-hour zones have nowhere to go: the field is whole hours. Truncate
  **toward zero**, so `-03:30` becomes `-3`. Flooring gives `-4` and moves every
  boundary by an hour.

---

## 3. Endpoint reference

All paths are relative to the base URL. "Params" means the parameters merged,
signed and then sent as query string (`GET`) or JSON body (`POST`), as
described in §1.

### 3.1 The device-id parameter name is NOT uniform

**VERIFIED, and the easiest thing in this API to get wrong.** The same bowl is
identified by two different key names depending on the endpoint:

| Key | Endpoints |
| --- | --- |
| `deviceId` | `device/list`, `device/config`, `device/detail`, `drinking/log`, `notify/getNotifyLog`, `device/setUnits` |
| `devNo` | `device/setType`, `device/flushConfig`, `device/sleepConfig`, `device/filterConfig`, `device/maintenanceConfig`, `device/waterConfig`, `device/logConfig`, `device/reset/filter` |

Note that `setUnits` uses `deviceId` while every other writer uses `devNo`.
There is no rule; it has to be memorised or table-driven.

INFERRED: sending the wrong key is likely to be read as "no device specified"
rather than rejected outright. This has not been deliberately tested, because
the plausible failure is a write applied to the wrong place.

### 3.2 Auth

#### `POST /app/user/login`

Params: `account` (the email address), `password` (`md5(md5(plaintext))`).

`data` is the **session token as a bare string**, not an object. VERIFIED.

Send an empty `token` header on this call.

#### `GET /app/user/loginInfo`

Returns the account record. **See the privacy section — this is the most
sensitive payload the vendor exposes.** The only field a device integration has
any use for is `subscribe` (with `subscribeExpires`).

### 3.3 Device state

#### `GET /app/device/list`

Params: `pageNum`, `pageSize`.

Returns the vendor's pagination envelope:

```json
{
  "pageNum": 1, "pageSize": 10, "totalPage": 1, "totalSize": 1,
  "hasNext": false,
  "data": [ { ...device row... } ]
}
```

**VERIFIED: list rows and `device/detail` rows have the identical shape and
carry the full device state.** One `device/list` call therefore covers every
bowl on the account, and the per-device `device/detail` calls the vendor's app
makes buy nothing.

The device row:

| Field | Type | Meaning | Marker |
| --- | --- | --- | --- |
| `id` | int | Server-side row id | VERIFIED |
| `userId` | int | Account id | VERIFIED |
| `deviceId` | string | The bowl's MAC with separators stripped | VERIFIED |
| `deviceName` | string | User-chosen name | VERIFIED |
| `status` | int | `1` = online; anything else is not | VERIFIED |
| `version` | string | Firmware version | VERIFIED |
| `createTime` / `updateTime` | string | `"YYYY-MM-DD HH:MM:SS"`, zone unstated | VERIFIED |
| `onlineTime` / `offlineTime` | string \| null | Last connect / disconnect, `"YYYY-MM-DD HH:MM:SS"`, **zone unstated**. Exactly one of the pair is populated at a time; a bowl that has never connected carries null for both. | VERIFIED |
| `deviceType` | int | Bowl size, **inverted** — see §4 | VERIFIED |
| `slaveType` | int | `0` none, `1` bottle pump, `2` wall unit | VERIFIED |
| `filterUsedTime` | int | Seconds of filter use | VERIFIED |
| `deviceUsedTime` | int | Seconds of total runtime | VERIFIED |
| `filterState` | int | `0` = fault, `1` = healthy | VERIFIED |
| `horizontalAlarm` | int | `1` = tilted | VERIFIED |
| `pumpAlarm` | int | `1` = pump alarm | VERIFIED |
| `injectionAlarm` | int | `1` = fill failed | VERIFIED |
| `systemSuspended` | int | `1` = suspended | VERIFIED |
| `hardwareFailure` | int | `1` = hardware fault | VERIFIED |
| `controlStatus` | int | Never read by the app; meaning unknown | VERIFIED (present) / meaning UNKNOWN |
| `units` | int | `1` = mL, `2` = fl oz | VERIFIED |
| `offline` | bool | Present alongside `status` | VERIFIED |

The app only ever tests the alarm fields `== 1`, never as a bitmask (VERIFIED,
read in the app). INFERRED: they are plain flags.

**The zone of `onlineTime` / `offlineTime` is genuinely unknown.** The app never
reads either field, so there is no rendering code to inspect, and the only other
timestamps in the API (`notify` rows) use a *different*, explicitly-UTC format,
which says nothing about these. Parsing them means guessing a zone, and a wrong
guess silently misplaces every reading by hours. This integration publishes them
as the vendor's own strings for that reason.

`deviceId` can be **null** on a half-provisioned bowl (VERIFIED — a row that
exists in the account but has never completed setup). Skip such rows; there is
nothing to address them with.

#### `GET /app/device/detail`

Params: `deviceId`. Returns one device row, same shape as above. Redundant if
you already call `device/list`.

**`filterDueState` does not exist — see §6.**

#### `GET /app/device/config`

Params: `deviceId`. Returns the writable configuration:

```json
{
  "headLength": 12, "bodyLength": 40, "seq": 2, "msgCode": 32,
  "devNo": "aabbccddeeff",
  "filterCanUseTime": 10512000,
  "capacity": 378541,
  "deviceCanUseTime": 0,
  "cleanWarnTime": 0,
  "cleanCycle": 3600,
  "cleanTime": 25,
  "dayMinWater": 2000,
  "dayMaxWater": 7500,
  "logState": 0,
  "deviceType": 0,
  "fillWashState": 0,
  "sleepState": 0,
  "sleepStart": 1320,
  "sleepEnd": 360
}
```

| Field | Written by | Unit | Marker |
| --- | --- | --- | --- |
| `cleanCycle` | `flushConfig` | seconds (UI: minutes) | VERIFIED |
| `cleanTime` | `flushConfig` | **raw seconds, no conversion** | VERIFIED |
| `fillWashState` | `flushConfig` | int flag | VERIFIED |
| `sleepStart` / `sleepEnd` | `sleepConfig` | minutes since **local** midnight | VERIFIED |
| `sleepState` | `sleepConfig` | int flag | VERIFIED |
| `filterCanUseTime` | `filterConfig` | seconds (UI: "months") | VERIFIED |
| `capacity` | `filterConfig`, **as `filterCapacity`** | mL | VERIFIED |
| `deviceCanUseTime` | `maintenanceConfig` | seconds (UI: days) | VERIFIED |
| `cleanWarnTime` | `maintenanceConfig` | **raw seconds, no conversion** | VERIFIED |
| `dayMinWater` / `dayMaxWater` | `waterConfig` | the bowl's own `units` | VERIFIED |
| `logState` | `logConfig` | int flag | VERIFIED |
| `deviceType` | `setType` (not a config group) | inverted enum — §4 | VERIFIED |
| `headLength`, `bodyLength`, `seq`, `msgCode` | — | **not state, ignore** — §8 | VERIFIED |

**Filter capacity is read as `capacity` and written as `filterCapacity`.**
VERIFIED. Same value, different wire name depending on direction. A
read-modify-write that echoes back `capacity` silently fails to set anything.

### 3.4 Config writers

**Every config write is a WHOLE-OBJECT read-modify-write.** VERIFIED: fields you
omit are **blanked** by the server, not left alone. There is no PATCH semantic
and no error to tell you it happened.

This compounds badly with the signing layer described in §1, which strips `None`
before sending: a config you only partially read produces a payload with holes
in it, the holes vanish on the way out, and the server blanks those fields. The
request succeeds. *Refuse to send a payload containing a `None`* — this
integration does exactly that in `_reject_partial_group()` (`entity.py`) rather
than trusting itself not to build one.

| Endpoint | Method | Full body | Marker |
| --- | --- | --- | --- |
| `/app/device/flushConfig` | POST | `{devNo, cleanCycle, cleanTime, fillWashState}` | VERIFIED |
| `/app/device/sleepConfig` | POST | `{devNo, sleepStart, sleepEnd, sleepState}` | VERIFIED |
| `/app/device/filterConfig` | POST | `{devNo, filterCanUseTime, filterCapacity}` | VERIFIED |
| `/app/device/maintenanceConfig` | POST | `{devNo, deviceCanUseTime, cleanWarnTime}` | VERIFIED |
| `/app/device/waterConfig` | POST | `{devNo, dayMinWater, dayMaxWater, units}` | VERIFIED |
| `/app/device/logConfig` | POST | `{devNo, logState}` | VERIFIED |
| `/app/device/setUnits` | POST | `{deviceId, units}` | VERIFIED |
| `/app/device/setType` | POST | `{devNo, deviceType}` | VERIFIED |
| `/app/device/reset/filter` | POST | `{devNo}` | VERIFIED |

Notice that `cleanWarnTime` belongs to **`maintenanceConfig`**, not
`filterConfig`, even though it reads like a filter setting. Sending it to
`filterConfig` blanks the filter settings and never reaches the field.

Three more rules, all VERIFIED:

- **`waterConfig` must echo `units`** from the device row. The thresholds are
  stored in the bowl's own unit, so a write that omits or guesses `units` is
  interpreted against the wrong scale.
- **`dayMinWater` must be strictly less than `dayMaxWater`**, unless *both* are
  exactly `0`, which is the disabled sentinel. Other combinations are refused.
- **The vendor is eventually consistent on writes.** Observed on real hardware:
  a write succeeded (`fillWashState` went `0 → 1`, siblings untouched) and an
  immediate `device/config` read returned the **pre-write** object. It caught up
  about **twenty seconds** later. A client that re-reads straight after a write
  and displays the result will show the user their change reverting. Trust the
  value you sent until the next scheduled poll.

### 3.5 Drinking log

#### `GET /app/drinking/log`

Params: `deviceId`, `units`, `startTime`, `endTime`.

`startTime`/`endTime` are naive `"YYYY-MM-DD HH:MM:SS"` strings, localised by
the `timeZone` in the signed body — **read §2 before using this endpoint.**

**Unlike every other list-shaped endpoint here, this one is not paginated.**
VERIFIED: it returns a bare array, not a pagination envelope.

```json
[
  {"totalCapacity": 711, "drinkingDate": "2026-09-15"},
  {"totalCapacity": 903, "drinkingDate": "2026-09-16"}
]
```

Its quirks are in §7.

### 3.6 Alerts

#### `GET /app/notify/getConfig`

No device id — this is **account-level**. Returns `isTextNotify`,
`isEmailNotify`, `config`, and **two arrays**, `notifyItems` and `notifyList`.

VERIFIED: in the live capture the two arrays were identical, element for
element. Which one `saveConfig` reads back is **UNKNOWN**, so write both and
keep them consistent.

Each element is `{"type": "Tilted", "desc": "Bowl or Controller Tilted",
"enabled": false}`. `enabled` is a real JSON **boolean** here (VERIFIED), unlike
the int flags elsewhere in the API — `isTextNotify`/`isEmailNotify` and the
device-config flags are ints, and sending `true` where the server stores `1`
changes the signed body as well as the value.

#### `POST /app/notify/saveConfig`

Whole-object read-modify-write, like the device config writers. Round-trip the
entire object you were given with one field changed.

#### `GET /app/notify/getNotifyLog`

Params: `deviceId`, `pageNum`, `pageSize`. Returns a pagination envelope whose
`data` is newest-first:

```json
{
  "id": 5007,
  "userId": 1001,
  "deviceId": "aabbccddeeff",
  "type": "Daily_Decreased",
  "mark": 1,
  "msg": "Bowl aabbccddeeff does not drink enough water today.",
  "createTime": "2026-09-16T07:00:01Z",
  "updateTime": "2026-09-16T07:00:01Z"
}
```

`createTime` here is `%Y-%m-%dT%H:%M:%SZ` — explicitly UTC, and a **different
format** from the device row's timestamps (VERIFIED).

See §5 for the alert catalogue and the three things the vendor's app gets wrong
about this endpoint.

### 3.7 Everything else observed

| Endpoint | Behaviour | Marker |
| --- | --- | --- |
| `GET /app/device/ipconfig` | `{"ip": ..., "port": "9557"}` — where the bowl dials out to. See §8. | VERIFIED |
| `GET /app/pet/list` | Pet names. See the privacy section. | VERIFIED |
| `GET /app/ota/check` | Answers `603` on the live server (see §1.4 — that is the rate-limit code, and this endpoint answers it consistently even when nothing has been rate-limited). No OTA path is usable. | VERIFIED |

---

## 4. Unit conversions

All VERIFIED. Each one is silent when you get it wrong: the server accepts the
value and the bowl behaves differently.

| Field | Wire | UI | Factor |
| --- | --- | --- | --- |
| `cleanCycle` | seconds | minutes | × 60 |
| `cleanTime` | seconds | seconds | **none** |
| `filterCanUseTime` | seconds | "months" | × 2,592,000 (see §5 caveat) |
| `cleanWarnTime` | seconds | seconds | **none** |
| `deviceCanUseTime` | seconds | days | × 86,400 |
| `sleepStart` / `sleepEnd` | minutes since local midnight | `HH:MM` | — |

**The app's "month" is exactly 2,592,000 seconds — 30 days**, not a calendar
month. VERIFIED: the app reads with `Math.floor(filterCanUseTime / 2592e3)` and
writes `30 * months * 24 * 60 * 60`.

`cleanWarnTime` has **no** conversion in either direction. It is called out here
because its absence looks like an oversight and is not: read it raw, write it
raw. Dividing it by 2,592,000 on the way out turns a one-week warning into `0`.

**`deviceType` is INVERTED.** VERIFIED: `0` = **9 inch**, `1` = **7 inch**.
Every reader guesses this the other way round.

`units`: `1` = millilitres, `2` = fluid ounces. VERIFIED.

---

## 5. A bug in the vendor's own app: filter life shortens on a no-op save

VERIFIED, and reproducible on the hardware this was built against.

The bowl was provisioned with `filterCanUseTime: 10512000`. That is exactly
`4 × 2628000` — four months of **365/12 days**, the astronomical month.

The app reads and writes with `2592000` — **30 days**:

- read: `Math.floor(10512000 / 2592000)` → `4`, displayed as "4 months";
- write: `30 * 4 * 24 * 60 * 60` → `10368000`.

So opening the vendor's filter screen and pressing save **without changing
anything** rewrites `10512000` as `10368000`, quietly shortening the filter
lifetime by **1.67 days**. Every time. The device was provisioned with one month
length and the app saves with another, and the difference accumulates for as
long as the user keeps visiting that screen.

The fix is not to change the constant — `2592000` is the vendor's arithmetic,
and being faithful to it is right for a *real* change, since their server and
their app are the ecosystem this lives in. The fix is to make the **no-op** a
genuine no-op: if the requested value floors to the value already displayed,
leave the stored seconds alone and write the device's own raw value back. This
integration does that in `_unchanged_keeps_raw()` (`number.py`), and it applies
to every setting stored in seconds and displayed in a coarser unit —
`filterCanUseTime`, `cleanCycle` and `deviceCanUseTime` — because the display is
a floor in all three cases.

---

## 6. `filterDueState` does not exist

VERIFIED. The vendor's app reads `filterDueState` from the `device/detail`
response and renders its filter-life tile from it. **The server never sends that
field**, in any response observed.

The consequence, visible in the wild: **their filter-life tile is permanently
green**, whatever the actual state of the filter.

Compute it instead:

```
remaining_fraction = (filterCanUseTime - filterUsedTime) / filterCanUseTime
```

Both fields are present on every device row (VERIFIED — observed
`184467 / 10512000`, i.e. 98.2% remaining).

`filterCanUseTime == 0` means the feature is **disabled** on that bowl
(VERIFIED — observed live). Guard the division: returning `None` is right, and
fabricating `0%` or `100%` is a lie either way.

Filter fields are also meaningless unless `slaveType == 2` (wall unit): a bottle
pump has no filter, and `filterState == 0` on one pins a permanent, unclearable
fault. The vendor's app hides the filter UI entirely for those units.

---

## 7. Drinking-log quirks

All VERIFIED.

**Sub-day time windows ARE honoured.** The vendor's app only ever asks for whole
days, but the endpoint accepts any `HH:MM:SS` and buckets accordingly, so an
hourly consumption profile is possible. This is the most interesting unused
capability in the API.

**BUT naive hourly buckets LOSE volume.** Twenty-four queries of
`HH:00:00`–`HH:59:59` summed to **903 mL** for a day whose whole-day query
returned **1028 mL** — 125 mL, 12%, simply gone. Two half-day queries over the
same day reconciled exactly (500 + 528 = 1028). The volume falls into the
one-second gap between `:59:59` and the next `:00:00`.

INFERRED: the server's range filter is inclusive at both ends at one-second
granularity, so an event timestamped inside that gap belongs to no bucket. Use
**half-day windows, or overlapping windows you de-duplicate yourself** — never
naive hourly slices. And whatever windowing you choose, **reconcile the parts
against the whole-day total**; this class of error is invisible otherwise.

**The server ZERO-FILLS days before the device existed.** A 120-day query
returned **122 rows**, going back months before the bowl was ever registered,
every one of them `totalCapacity: 0`. Those zeros are not measurements. A client
that charts them draws a confident flat line through a period when the bowl was
in its box.

**A range of roughly a year or more returns an EMPTY array**, not a truncated
set. The failure mode is "no data", not "less data", so a naive
"fetch everything" call looks like an account with no history at all. The exact
threshold has not been bisected; INFERRED that it is a server-side range cap.

**Values are not quantised into fill units.** Observed: 93, 125, 250 and 278 mL,
with no common divisor. INFERRED: the bowl reports a measured volume rather than
a count of fills, so do not try to reverse a "number of drinks" out of it. The
log carries **daily aggregates only** — there is no per-drink detail anywhere in
this API.

**Missing days are omitted, within the device's lifetime.** A day the server has
no row for is absent from the array rather than returned as `0`. Distinguish
"no row" from `totalCapacity: 0`: the first means "not known yet", the second
means "known to be nothing", and collapsing them into a fabricated zero tells
the user their pet did not drink.

---

## 8. `/app/device/ipconfig` and the binary protocol

VERIFIED:

```json
{"ip": "<vendor ingest host>", "port": "9557"}
```

This is where the **bowl** dials out to — not an address of the bowl, and not
anything a client talks to over HTTP.

**Port 9557 is not HTTP.** The bowl speaks a binary protocol there. That is the
explanation for the four otherwise-inexplicable fields in the `device/config`
response:

| Field | Observed |
| --- | --- |
| `headLength` | 12 |
| `bodyLength` | 40 |
| `seq` | 2 |
| `msgCode` | 32 |

These are **raw device framing surfacing through the REST layer** — a header
length, a body length, a sequence number and a message opcode. INFERRED: the
REST endpoint serialises the last device frame the server holds, framing and
all, rather than a purpose-built view.

**They are not device state. Ignore them.** Do not model them, and above all do
not echo them back in a config write. The safest handling is to never read them
into your config object in the first place, so no write path can carry them.

The binary protocol itself has not been reverse-engineered. Anyone who wants to
try starts here.

---

## 9. Alerts

### The ten types

VERIFIED, from `/app/notify/getConfig` — the catalogue is exactly ten, and each
notify-log row carries one of them in its `type` field:

| `type` | `desc` |
| --- | --- |
| `Tilted` | Bowl or Controller Tilted |
| `Daily_Maximum` | Daily Maximum Water Reached |
| `Fill_Failed` | Failed to Fill |
| `Not_Attached` | Controller not Attached Firmly |
| `High_Water_Level` | High Water Level Detected |
| `Replace_Wall_Filter` | Replace Wall Unit Filter |
| `Replace_Bowl_Filter` | Replace Bowl Filter |
| `Daily_Decreased` | Pet Daily Average Decreased |
| `Operation_Confirmation` | Normal Operation Confirmation |
| `Hardware_Fault` | Hardware Fault |

These are **stable machine-readable codes**, and the vendor's own app ignores
the field entirely — it renders `msg` instead. Match on `type`; treat anything
outside this list as unknown rather than crashing, and keep the vendor's
original string so a type added later is still visible.

### Three things the vendor's app cannot do, or gets wrong

**(a) It can never see past the first page.** VERIFIED, read in the app: the
`pageNum` sent to `getNotifyLog` is hard-coded to `1`. The endpoint is genuinely
paginated — `pageNum`, `pageSize`, `totalPage`, `totalSize` and `hasNext` are
all present and populated — so history beyond the first page exists on the
server and is reachable by any client that increments `pageNum`. Their app never
does. (The page size their app requests has not been confirmed; the field is
accepted from a client either way.)

**(b) `mark` is unreliable. Derive severity from `type`.** VERIFIED: the app's
own enum claims `1` = Ordinary and `2` = Alarm. Live rows carry only `0` and
`1`. Worse, the values do not separate routine from serious in either reading:

| Row | `type` | `mark` |
| --- | --- | --- |
| routine fill | `Operation_Confirmation` | 0 |
| pump returned to normal | `Not_Attached` | 0 |
| bowl knocked over | `Tilted` | 1 |
| pet drank less than usual | `Daily_Decreased` | 1 |

A routine "pump returned to normal" shares a `mark` with a routine fill, while a
genuine `Tilted` fault shares one with an advisory `Daily_Decreased`. Whatever
`mark` means, it is not severity. **Do not read it.** `type` is unambiguous and
is on every row already.

**(c) The log records every alert REGARDLESS of the enable flags.** VERIFIED:
with every type's `enabled` set to `false` in `notify/getConfig`, the notify log
still received rows for all of them. The `enabled` flags gate **the vendor's own
text and email delivery**, not the log.

The practical consequence is the whole point of this integration: a client can
surface **all ten alert types**, whatever the account's notification settings
say. It is an endpoint the app already calls, read with the account's own
credentials.

INFERRED: `Daily_Decreased` is evaluated against the pet's own rolling average
rather than a fixed threshold. It fires from a server-side job at local midnight
(§2), which is consistent with a daily rollup, but the comparison itself has not
been observed.

### Deduplication

Notify rows carry a stable `id` (VERIFIED). Use it.

The live capture shows the ordinary shape — newest first, ids ascending over
time — so a high-water mark *would* work, on an ordering the vendor has never
documented and that has been seen exactly once, from one account, over thirteen
rows. A watermark that is wrong once does not degrade gracefully: it seeds itself
above every future alert and silently suppresses all of them, for ever. A
bounded set of seen ids cannot fail that way, whichever way ids are assigned.

---

## 10. Endpoints with no UI in the vendor's app

VERIFIED:

- **`/app/device/logConfig` (`logState`)** — a working endpoint the app defines
  and **never calls**. It controls whether the bowl records to the drinking log
  at all. In the app's build, the call site is present in the API layer with no
  screen wired to it.
- **`cleanWarnTime`** — supported by the server, and **hard-coded to `0` on
  every write** by the app. There is no control for it anywhere in their UI. Its
  units are raw seconds (§4), and because the app never sets it to anything,
  there is no app behaviour to check a conversion against.

INFERRED for both: these are unfinished or deprecated features rather than
deliberately hidden ones. Nothing in the app suggests which.

---

## 11. Known unknowns

Stated so nobody mistakes silence for absence of doubt:

- The meaning of `controlStatus` on the device row.
- The meaning of `config` (an int) on the notify config object.
- Which of `notifyItems` / `notifyList` `saveConfig` actually reads.
- The zone of `onlineTime` / `offlineTime`.
- The exact drinking-log range threshold at which the response becomes empty.
- What `602` means, beyond "not authenticated".
- The binary protocol on port 9557, in its entirety.
- Where the `603` rate limit's threshold actually sits, and whether reads count
  against the same budget as logins. Eight logins in a few seconds is enough to
  trip it; nothing narrower has been measured.
- Whether `603` is one overloaded code or a broader "temporarily unavailable"
  — `/app/ota/check` and `/app/pay/get/product` (no subscription) both answer it.

---

## 12. Etiquette

This is a small vendor's production API, reached with an ordinary user's
credentials. It has no published limits and no way to ask for more.

- Poll at a sane interval. A 60-second poll is roughly what the vendor's own app
  generates while it is open (their app polls `device/detail` every **three
  seconds**), so it is well within ordinary use.
- Prefer one `device/list` to N × `device/detail`.
- Fetch account-level objects (`notify/getConfig`) far less often than
  per-device ones; they change only when a user edits them.
- Serialise your requests per account. The vendor's limit — whatever it is — is
  per account, so parallel writes from one client count against one budget.
- On a write, trust what you sent rather than firing an immediate re-read
  (§3.4): it costs a request and returns stale data.
- Do not retry `651` in a loop. §1.5 explains what that does to the user's phone
  app, and to you.
- Bound how often you re-login, not just how often you retry one operation. Two
  clients on one account evict each other (§1.5), and "re-login on every
  failure" turns that into a login storm — which is exactly what trips `603`.
