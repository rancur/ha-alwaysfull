# Changelog

Release notes, newest first. Releases before 0.4.0 predate this file; their
changes are in the commit history, one `build: ship as <version>` commit per
release.

## Unreleased

### Three new sensors per bowl

- **Water lifetime** — every completed day since the bowl was registered plus
  today, as a `total_increasing` volume, so Home Assistant's long-term
  statistics keep a lifetime meter. The history is read once at startup in
  windows of at most 180 days (the vendor answers a window of a year or more
  with an empty array), then one request per bowl per day. `unknown` when the
  device row carries no `createTime`.
- **Last drink** — the poll that first saw today's total rise. The vendor's
  log has no per-drink detail, so this is this integration's own observation
  at the scan interval's resolution. Restored across restarts; `unknown` until
  the first rise is seen.
- **Last refill** — the newest routine-fill alert on the vendor's notify page,
  as a timestamp.

Nothing existing changes: no entity ids, unique ids or states are affected.
The diagnostics download gains `water_total` and `last_drink` per bowl.

### One slow request no longer takes every entity unavailable

Measured on a live install: the vendor's `notify/getNotifyLog` hung for 60–70
seconds roughly every other poll until their gateway answered with an HTML
error page, and each time all of the bowl's entities went unavailable for a
minute. Three changes:

- Every request is abandoned after **30 seconds** instead of waiting for the
  vendor's gateway.
- A gateway error (HTTP 5xx) or a non-JSON body is reported as what it is —
  `Server responded HTTP 504 …` — rather than as a complaint about the first
  character of an HTML page. With debug logging on, the start of that body is
  logged so the next bug report can say which page the vendor served.
- Today's drinking log, the drinking history and the alert log are now
  **secondary reads**: if one fails, that reading is `unknown` for the poll
  (the alert entities keep the last page they saw) and the rest of the bowl
  stays live. The device list and config still fail the poll, and a rejected
  session or a rate limit still gets the handling it always had.
- The alert log is read five rows at a time (was twenty), with its own
  55-second timeout, and is rested for two polls after a failed read. Timed
  against a live account: a five-row page took 14–58 seconds, and ten or
  twenty rows timed out at the vendor's gateway every single time.

## 0.4.0

### ⚠️ Breaking: the two connection times are timestamps, not text

`sensor.<bowl>_last_connected` and `sensor.<bowl>_last_disconnected` now carry
Home Assistant's `timestamp` device class and publish a real instant.

| | State |
| --- | --- |
| **0.3.x and earlier** | `2026-09-15 22:08:57` |
| **0.4.0 onwards** | `2026-09-15T22:08:57+00:00` |

**What you have to change.** Anything that reads the *string* form of either
entity — a template that calls `strptime` on it, a condition comparing it to a
literal, a card that prints it raw. Anything comparing instants gets simpler:
`as_datetime` and `now()` work on it directly, with no parsing and no decision
about which zone the pieces were in.

**What you do not have to change.** Entity ids, unique ids, names,
categorisation: all identical. Nothing to re-add, rename or re-enable.
`unknown` still means the vendor sent nothing for that half of the pair —
`last_disconnected` is `unknown` for as long as the bowl stays connected, and
it is still `unknown` rather than a stand-in instant. History recorded before
the upgrade keeps the old string form, because that is what those rows were.

**Why now.** Through 0.3.x these shipped as bare strings on purpose, not by
omission. The vendor sends them with no zone and no offset, a timestamp sensor
has to name one, and naming the wrong one moves every reading by hours while
looking entirely plausible — a history graph in the wrong place, and no error
anywhere.

The zone has now been measured, three ways, and it is **UTC**:

1. The fields are **not** localised per request. The signed `timeZone` field
   genuinely does change how the server interprets `drinking/log` (1028 mL at
   `-7` against 1489 mL at `0`, same window), so this had to be ruled out
   rather than assumed — and `onlineTime`, `offlineTime`, `createTime` and
   `updateTime` come back byte-identical at `-7`, `0` and `+9`.
2. They match the vendor's own explicitly-UTC format to the second: an
   `onlineTime` of `2026-09-15 22:08:57` against the same bowl's first-ever
   `Operation_Confirmation` alert row at `2026-09-15T22:08:57Z`.
3. Corroborated from outside the vendor entirely: the owner's own WiFi
   controller logged that bowl associating at 15:08 local time (UTC-7) that
   day — 22:08 UTC.

The full write-up, including what the previous "the zone is not determinable"
reasoning was and which part of it the measurement disproved, is in
[`docs/VENDOR-API.md`](docs/VENDOR-API.md) §3.3.

### Also in this release

- The parse reports `unknown` rather than raising for a stamp it cannot read,
  including a vendor field that arrives as something other than a string. It
  runs inside a coordinator listener, where an exception would cost every other
  entity on that bowl its update, not just this one.
- A stamp that states its own zone is trusted as sent rather than overridden
  with UTC. The vendor already uses a zoned format for alert rows, so the
  device row acquiring an offset one day is a realistic change.

### Considered and declined

Numeric alert counters (`sensor.<bowl>_alerts_today` and similar, as
`TOTAL_INCREASING`), proposed to get fault history into long-term statistics.
Declined for now — it is speculative feature design rather than an observed
defect, and it adds entities to every installation to answer one user's
question. The reasoning, and the strongest form of the argument *for* it, are
recorded in [`docs/VENDOR-API.md`](docs/VENDOR-API.md) §9.
