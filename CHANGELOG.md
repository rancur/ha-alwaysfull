# Changelog

Release notes, newest first. Releases before 0.4.0 predate this file; their
changes are in the commit history, one `build: ship as <version>` commit per
release.

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
