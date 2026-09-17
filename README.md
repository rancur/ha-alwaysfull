<p align="center">
  <img src="custom_components/alwaysfull/brand/icon.png" alt="" width="128" height="128">
</p>

<h1 align="center">Always Full for Home Assistant</h1>

<p align="center">
  <a href="https://github.com/rancur/ha-alwaysfull/actions/workflows/validate.yml"><img src="https://github.com/rancur/ha-alwaysfull/actions/workflows/validate.yml/badge.svg" alt="Validate"></a>
  <a href="https://github.com/rancur/ha-alwaysfull/actions/workflows/test.yml"><img src="https://github.com/rancur/ha-alwaysfull/actions/workflows/test.yml/badge.svg" alt="Test"></a>
  <a href="https://hacs.xyz/"><img src="https://img.shields.io/badge/HACS-custom%20repository-41BDF5.svg" alt="HACS: custom repository"></a>
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT licence">
  <img src="https://img.shields.io/badge/iot__class-cloud__polling-orange.svg" alt="cloud polling">
</p>

Brings the [Always Full](https://alwaysfull.com/) automatic pet water bowl into
Home Assistant: how much your pet drank today, whether the bowl is online,
level, filling and healthy, how much filter life is left, and every setting the
vendor's app can change. All ten of the bowl's alert types arrive as Home
Assistant events, ready to use as automation triggers.

> **Before you install:** this is **cloud-polled and has no local control path
> whatsoever** — a full scan of all 65,535 ports on the device found zero open
> ports. If the vendor's servers are down, so is this. Please read
> **[Known limitations](#known-limitations)** first; it is the section that
> decides whether this integration is right for you, and it is a long way down
> the page.

---

## The headline: every alert, as an automation trigger

The bowl has something to say when it tips over, when it fails to fill, and
when your cat drinks less than usual. This integration polls the vendor's
notification log and re-emits each new entry as a Home Assistant `event`
entity, with the alert type as the event type — so any of those can start an
automation, light up a dashboard, or page your phone through whichever notifier
you already use.

The vendor's servers write every alert into that log regardless of the account's
notification settings — **including when every alert type is switched off in
their app** — because those switches gate the vendor's own text and email
delivery, not the log. Nothing has to be turned on anywhere for the `event`
entity to fire.

All ten types come through:

| Event type | What the bowl means |
| --- | --- |
| `tilted` | Bowl or controller tilted |
| `daily_maximum` | Daily maximum water reached |
| `fill_failed` | Failed to fill |
| `not_attached` | Controller not attached firmly |
| `high_water_level` | High water level detected |
| `replace_wall_filter` | Replace the wall unit's filter |
| `replace_bowl_filter` | Replace the bowl's filter |
| `daily_decreased` | Pet drank less than their average |
| `operation_confirmation` | Normal operation (routine fill) |
| `hardware_fault` | Hardware fault |

An eleventh value, `unknown`, is reported if the vendor ever sends a type that
is not in this list. The vendor's own spelling is always available in the
`raw_type` attribute, so a new type is still matchable the day it appears.

This is the same endpoint the vendor's own app calls, read with your own
account's credentials — their API, driven from your own home automation
system.

---

## Beyond what the app exposes

Reverse-engineering the protocol turned up several things the Always Full app
either does not do or does wrong. Each of these is a real difference you get by
using this integration, and each one is documented in detail — with what was
measured and what is only inferred — in **[Known vendor
quirks](docs/VENDOR-API.md)**.

**All ten alert types, as automation triggers.** The vendor's per-alert switches
gate their own text messages and emails. The notification log they read from is
written either way, so every alert type reaches Home Assistant even with all ten
of those switches off.

**Alert history the vendor's app cannot reach.** Their app asks for page one of
the notification log and nothing else — the page number is a constant in their
code — so its history is capped at a single page for ever, however long the
account has existed. The endpoint itself is paginated. Home Assistant also keeps
its own record: every alert this integration sees becomes a logbook entry and
stays in your history, long after it has scrolled off the vendor's page.

**Two settings their app never shows you.** Drinking-log recording (`logState`)
is a working endpoint their app defines and never calls, and the maintenance
warning lead time is supported by the server but written as `0` on every save by
their app, with no control for it anywhere in their UI. Both are ordinary
entities here.

**A no-op save does not shorten your filter life.** Theirs does. The bowl this
was built against was provisioned with a filter lifetime measured in one length
of "month", and their app reads and writes it in another, so opening their
filter screen and pressing save without changing anything quietly takes 1.67
days off the filter. This
integration writes the device's original raw value back when the displayed value
has not changed, so a no-op stays a no-op.

**Filter life that actually moves.** Their app reads a field the server does not
send, which is why their filter tile is permanently green. This one computes the
percentage from the two fields the server does send.

---

## Installation

The integration is **not in the HACS default index**, so it has to be added as
a custom repository. This takes about a minute.

1. Open **HACS** in Home Assistant.
2. Click the **⋮** menu in the top right and choose **Custom repositories**.
3. Paste `https://github.com/rancur/ha-alwaysfull` into **Repository**.
4. Choose **Integration** as the **Type**, then click **Add**.
5. Close the dialog. Search HACS for **Always Full** and open it.
6. Click **Download**, then **Download** again to confirm.
7. **Restart Home Assistant.** HACS will offer to do it for you.
8. Go to **Settings → Devices & Services → + Add Integration** and search for
   **Always Full**.
9. Enter the email address and password you use in the Always Full app.

That is the same account the app uses; there is no separate API key and nothing
to register. Your password is sent once, hashed the way the vendor's app hashes
it, and only the session token it returns is kept afterwards.

### No HACS?

Copy `custom_components/alwaysfull/` from this repository into your Home
Assistant `config/custom_components/` directory and restart. You will have to
repeat that for every update, which is what HACS is for.

### Removing it

**Settings → Devices & Services → Always Full → ⋮ → Delete**. That removes the
config entry, its devices and all of its entities, and stops all polling. Then
remove the repository from HACS (**HACS → Always Full → ⋮ → Remove**) and
restart. Nothing is left behind outside Home Assistant's own config directory,
and nothing is changed on your Always Full account.

---

## Configuration

Everything except the poll interval is an entity, changeable from the device
page. The poll interval lives in the options flow:

**Settings → Devices & Services → Always Full → Configure**

| Option | Default | Range | Notes |
| --- | --- | --- | --- |
| Scan interval | 60 s | 30–600 s | How often every bowl is polled. Values outside the range are clamped rather than rejected, including ones written directly into storage. |

One poll costs three requests per bowl plus one shared request, and a fourth
shared request every tenth poll. At the default 60 seconds a single bowl is
about 5,900 requests a day — well inside what the vendor's own app does, which
polls device detail every three seconds while it is open.

To re-enter your password without removing the integration, use **⋮ →
Reconfigure**. If the vendor rejects your credentials, Home Assistant starts a
repair flow on its own.

---

## Entities

Home Assistant creates **33 entities per bowl**, plus **12 account-level
entities** shared by every bowl on the account.

### Per bowl

#### Sensors

| Entity | Notes |
| --- | --- |
| Water today | Total consumed today. `unknown` until the server has a row for today — never a fabricated `0`. |
| Filter life | Percentage remaining. Wall unit only. |
| Filter time remaining | Shown in days. Wall unit only. |
| Water source | `wall_unit`, `bottle_pump`, `none` or `unknown`. Diagnostic. |
| Last alert | The newest alert's type, as one of the eleven values above. |
| Total runtime | How long the bowl has been running in total, shown in days. Diagnostic. |
| Last connected | When the bowl last connected, as the vendor's own `YYYY-MM-DD HH:MM:SS` string. Diagnostic. See the note below. |
| Last disconnected | When it last dropped off. `unknown` while it is connected — the vendor sends one of this pair at a time. Diagnostic. |
| Firmware version | Diagnostic, **disabled by default** — the device page already shows it. |

**Why the two connection times are text and not timestamps.** The vendor sends
them with no time zone and no offset, and nothing available says which zone it
means: its own app never reads either field, and the only other timestamps in
the API use a different format that states its zone explicitly. Publishing them
as real timestamps would mean guessing, and a wrong guess moves every reading by
hours while looking completely normal. So they are published exactly as sent.

#### Binary sensors

| Entity | Device class | Notes |
| --- | --- | --- |
| Online | connectivity | |
| System problem | problem | Suspended, or a hardware failure. |
| Water fill alarm | problem | The bowl could not fill. |
| Pump alarm | problem | |
| Not level | problem | The bowl or controller is tilted. |
| Filter fault | problem | Wall unit only; `unknown` on anything else. A bottle pump has no filter, and `filterState == 0` on one would pin a permanent, unclearable fault. |
| Water source detached | problem | On when the bowl reports no wall unit and no bottle pump. Not the same thing as the `not_attached` alert. |

#### Event

| Entity | Notes |
| --- | --- |
| Alert | Fires once per new alert. Attributes: `event_type`, `raw_type`, `message`, `created`, `id`. |

#### Numbers, switches, selects, times and a button

All of these write to the bowl and are in the **Configuration** section of the
device page.

| Entity | Platform | Range / options |
| --- | --- | --- |
| Flush interval | number | 1–1440 minutes |
| Flush duration | number | 5–120 seconds |
| Filter lifetime | number | 0–48 months (a vendor "month" is exactly 30 days, not a calendar month) |
| Filter capacity | number | 0–200,000,000 mL |
| Maintenance interval | number | 0–365 days |
| Maintenance warning lead time | number | 0–31,536,000 **seconds**. How long before the maintenance interval expires you are warned. Seconds because that is exactly what the device stores; the vendor's app writes `0` here on every save and gives you no control over it at all. |
| Daily minimum water | number | 0–200,000, in the bowl's own unit |
| Daily maximum water | number | 0–200,000, in the bowl's own unit |
| Flush only after filling | switch | |
| Sleep mode | switch | |
| Drinking log recording | switch | Turning this off stops **Water today** updating. |
| Units | select | `ml` / `fl_oz` |
| Bowl size | select | `9_inch` / `7_inch` |
| Sleep start | time | |
| Sleep end | time | |
| Reset filter life | button | Press after fitting a new filter. |

Daily minimum must be below daily maximum, or both must be exactly `0` to
disable the thresholds. Anything else is refused with a readable error rather
than silently dropped.

### Account level

These belong to the account, not to any one bowl, and appear under a service
device called **Always Full account**. They exist because the vendor's
notification settings are account-wide: one switch per setting, not one per
bowl, so two bowls cannot disagree about a single server-side flag.

| Entity | Platform |
| --- | --- |
| Text alerts | switch |
| Email alerts | switch |
| Ten per-alert-type switches (Tilted alerts, Fill failed alerts, …) | switch |

**You do not need any of these switched on for the `event` entity to fire.**
They control the vendor's own text messages and emails; the notification log the
`event` entity reads is written either way.

---

## Automation examples

Both examples use a plain **state** trigger with a **template condition**
rather than a trigger on the `event_type` attribute. That is deliberate and
worth copying: an attribute trigger only fires when the attribute *changes*, so
a second `daily_decreased` alert in a row would not fire it. The entity's state
is the timestamp of the last event, which changes every single time.

Replace `water_bowl` with your own bowl's entity id.

### 1. Your pet is drinking less than usual

`daily_decreased` is the alert the bowl raises when consumption drops below the
pet's own rolling average — quiet, easy to miss in person, and usually the
first sign that something is wrong.

```yaml
alias: "Water bowl: pet drank less than usual"
description: >
  Notifies once when the bowl reports a drop in daily consumption, with
  today's total for context.
triggers:
  - trigger: state
    entity_id: event.water_bowl_alert
    not_from:
      - unknown
      - unavailable
conditions:
  - condition: template
    value_template: >
      {{ trigger.to_state.attributes.event_type == 'daily_decreased' }}
actions:
  - action: notify.persistent_notification
    data:
      title: "Water bowl: drinking less than usual"
      message: >
        {{ states('sensor.water_bowl_water_today') }}
        {{ state_attr('sensor.water_bowl_water_today', 'unit_of_measurement') }}
        today, which is below the usual average.
mode: single
```

### 2. Hardware fault, or the bowl cannot fill

These are the ones that need a person. They are grouped into one automation
because the response is the same: go and look at it.

```yaml
alias: "Water bowl: needs attention"
description: >
  Fires on a hardware fault, a failed fill, or the bowl being knocked over.
  Repeats every 30 minutes until the bowl stops reporting a problem.
triggers:
  - trigger: state
    entity_id: event.water_bowl_alert
    not_from:
      - unknown
      - unavailable
conditions:
  - condition: template
    value_template: >
      {{ trigger.to_state.attributes.event_type in
         ['hardware_fault', 'fill_failed', 'tilted'] }}
actions:
  - repeat:
      sequence:
        - action: notify.mobile_app_your_phone
          data:
            title: "Water bowl needs attention"
            message: >
              {{ trigger.to_state.attributes.event_type | replace('_', ' ') }}
              — reported at {{ trigger.to_state.state | as_timestamp
              | timestamp_custom('%H:%M') }}.
            data:
              priority: high
        - delay: "00:30:00"
      until:
        - condition: state
          entity_id: binary_sensor.water_bowl_system_problem
          state: "off"
        - condition: state
          entity_id: binary_sensor.water_bowl_water_fill_alarm
          state: "off"
        - condition: state
          entity_id: binary_sensor.water_bowl_not_level
          state: "off"
mode: single
```

### Bonus: everything the bowl says, in the logbook

```yaml
alias: "Water bowl: log every alert"
triggers:
  - trigger: state
    entity_id: event.water_bowl_alert
    not_from:
      - unknown
      - unavailable
actions:
  - action: logbook.log
    data:
      name: Water bowl
      message: >
        {{ trigger.to_state.attributes.event_type }}
        (vendor type {{ trigger.to_state.attributes.raw_type }})
mode: queued
```

---

## Known limitations

Read this section before filing an issue. Most of it is the hardware, not the
integration, and none of it is going to change.

**It is cloud-polled, and it dies when the vendor's servers do.** Every reading
comes from `app.alwaysfull.com`. If that is down, slow or rate-limiting, your
entities go unavailable. There is no offline mode and no cached fallback,
because a cached water total is a lie about your pet.

**There is no local control path. At all.** A full TCP scan of the device
across all 65,535 ports found **zero open ports**. It is a Bouffalo Lab BL602
running a pure outbound cloud client — it connects out and never listens. No
amount of work on this integration can produce a local API, and reflashing a
BL602 is not a realistic alternative for a bowl full of water.

**The vendor can break this without warning.** The protocol was
reverse-engineered from the Android app and verified against the live server.
Nobody at Always Full has agreed to keep any of it stable. A server-side change
can break this integration on any given morning.

**Volume readings are converted to your unit system.** Home Assistant renders
convertible units in the viewer's own system, so a bowl set to fluid ounces
reads in millilitres on a metric Home Assistant and vice versa. The number will
often *not* match what the vendor's app shows. The underlying value is the
same; only the presentation differs. (The two *threshold* numbers are not
converted — they are sent to the server in the bowl's own unit.)

**Filter entities are meaningless on a bottle pump.** Filter life, filter time
remaining and filter fault apply to the wall unit. On a bottle-pump or
standalone bowl they report `unknown`, which is honest: the vendor's app hides
them entirely for those units.

**A daily-threshold write during the post-write window can echo a stale unit.**
The daily minimum and maximum are sent together with the bowl's `units` value,
read from the last poll. If you change **Units** and then immediately change a
threshold, before the debounced refresh has landed, the threshold write can
carry the previous unit. Wait for the Units select to settle — a few seconds —
before changing a threshold, or set the threshold again afterwards.

**Alerts raised while Home Assistant was down are never fired.** On startup the
entity records everything already on the vendor's page without firing it. The
alternative is worse: a restart would replay up to twenty alerts at once, at
whatever hour you restarted. Those alerts are still visible on the **Last
alert** sensor and in the vendor's app.

**An alert with no id is skipped.** Deduplication is keyed on the row id the
vendor assigns. A row without one cannot be deduplicated, and firing it would
re-fire it on every poll for as long as it stayed on the page.

**Not everything in the app is exposed.** There are no OTA entities —
`/app/ota/check` answers `603` on the live server, the same code the vendor
uses to say "too many requests", so there is no usable OTA path either way —
and no entity for the vendor's `filterDueState`, which their API never actually
returns (which is why their own filter-life tile is permanently green).

---

## Known vendor quirks

The Always Full protocol is undocumented, and a fair amount of it is
surprising: daily totals that depend on the time zone the client declares, a
filter lifetime their own app shortens on a no-op save, an alert severity field
that does not indicate severity, a device-id parameter with two different names
depending on the endpoint, and a drinking log that zero-fills months before the
bowl existed.

If you are debugging something odd, writing another client, or reverse-
engineering this device yourself, all of it is written up in
**[docs/VENDOR-API.md](docs/VENDOR-API.md)** — every claim marked as either
verified against the live API or inferred, with the measurements that back it.

---

## Troubleshooting

**Everything is unavailable.** Check the bowl in the vendor's app first. If it
works there, look for `custom_components.alwaysfull` lines in your Home
Assistant log; a rate limit or a timeout is reported as an update failure and
recovers on its own, while a credentials problem starts a repair flow.

**It loaded, but there are no bowl entities.** The account signed in fine and
the vendor returned an empty device list, so there was nothing to create
entities for; look for the `custom_components.alwaysfull` warning saying the
account returned no bowls. It usually clears itself — as soon as a poll lists
the bowl, its entities appear on their own, with no reload. If the bowl is
visible in the Always Full app and this persists, open an issue.

**Using the phone app signs Home Assistant out (and that is fine).** Opening
the Always Full app on your phone signs Home Assistant out of your account, and
Home Assistant signs itself back in on its next poll or its next setting change.
You do not have to stop using the app, and there is nothing to do about it.

In detail: the vendor allows **one active session per account**. Signing in
anywhere else — the phone app is the everyday case — invalidates the token Home
Assistant is using, and the next request it makes comes back `token expiration`.
This is normal, it is not a sign anything is broken.

The integration recovers on its own. A poll or a setting change that meets a
rejected token signs in again, once, and carries on; a change you made goes
through on that second attempt, so you should see nothing at all. The reverse
is true too: Home Assistant signing back in is what logs the *app* out, so an
app that asks you to sign in again after a while is the same behaviour seen
from the other side.

What is *not* normal is this failing repeatedly. The integration will not
re-authenticate more than once per operation, and not more than **three times
in five minutes** overall — against a one-session-per-account server, signing
in on every failure is how two clients sign each other out for ever, and a
burst of sign-ins is what the vendor answers with "too many requests".

So a failure that keeps repeating does one of two things, and the difference
matters:

- **Something else is using the account** (a second Home Assistant, or an app
  left open somewhere). Past that budget the integration stops signing in for a
  few minutes: your entities go unavailable and come back on their own. It does
  **not** ask you to re-authenticate, because your password is not the problem.
- **The sign-in itself was refused**, i.e. the account's password has changed.
  Home Assistant asks you to re-authenticate, which is the case below.

**It keeps asking me to re-authenticate.** The vendor returns the same error
for a wrong password and for an address with no account, so those two cases
cannot be told apart. Sign in to the Always Full app with the same credentials
to find out which it is.

**A setting snaps back to its old value.** That is a refused write. The
integration re-reads the bowl's config immediately after every write precisely
so you see the truth rather than what you asked for. Check the log for the
reason.

**A switch reads `unknown`.** The vendor's answer did not carry that setting.
It is reported as unknown rather than off on purpose: "off" would invite you to
switch on something that may already be on. It clears on the next poll that
carries the field.

**Water today is `unknown`.** Either the server has no row for today yet
(common early in the morning) or **Drinking log recording** is switched off on
that bowl.

**Water today looks wrong, and check your time zone first.** The vendor's
server does not store daily totals against a fixed clock. It works out which day
each drink belongs to from the time zone the *client* declares, and Home
Assistant declares the one it is configured with
(**Settings → System → General → Time zone**). So if Home Assistant's zone is
wrong, your daily total is wrong: the day is being cut at the wrong hour, and
drinks are landing on the day before or the day after.

This was measured, not guessed. The same query over the same window returned
1,028 mL, 1,489 mL and 1,086 mL under three different declared zones — a 45%
spread. **Nothing reports an error when this happens.** The total looks
perfectly plausible; it is just not your pet's day. If Home Assistant's zone
does not match the zone the bowl is standing in, fix it there and the totals
correct themselves on the next poll.

**The numbers do not match the app.** See *Volume readings* above.

**Filing an issue.** Attach the diagnostics download
(**Settings → Devices & Services → Always Full → ⋮ → Download diagnostics**).

Every field known to be sensitive is redacted before the file is written: your
email address, password, session token, account id, every bowl's device id and
the name you gave each bowl are replaced with `**REDACTED**`, and so is the text
of each alert, because the vendor writes your bowl's MAC address into it. Each
bowl appears as a derived label like `bowl-1a2b3c4d` instead of its id or name.

Your **name, phone number and postal address** are redacted too, even though
nothing this integration fetches contains them. The vendor's API will hand all
of that out to anyone holding your session token, from an endpoint this
integration deliberately does not call; the redaction is in place first, so that
adding the one useful field on that record cannot quietly publish the rest.

The file **also contains the vendor's raw device record verbatim**, which is
deliberate — it is what makes a report about an unsupported bowl useful, and it
carries fields this integration does not model. Redaction works by field name,
so a field the vendor adds after this was written is passed through unredacted.
Nothing like that is in the payload today, but "today" is the honest scope of
that claim. **Skim the file before you post it.** If you find something in
there that should have been redacted, that is a bug worth its own issue and it
will be treated as urgent.

---

## About the signing secret in the source

`custom_components/alwaysfull/const.py` contains a constant called
`SIGN_SECRET`. To be completely clear about what that is:

- It is the **vendor's** request-signing constant, the same value for every
  installation of their app on every phone in the world.
- It is **already public**: it ships in plain text in the Android APK that
  Always Full distributes, which is where it was read from.
- It is **not a credential, and not yours**. It is not derived from your
  account, it grants no access on its own, and knowing it lets nobody do
  anything they could not do by downloading the vendor's app.

Your actual secrets — your password and your session token — are never logged,
never written to diagnostics, and never included in any error message. A test
in this repository asserts that the diagnostics download contains none of them,
and it was verified by being made to fail first.

---

## Contributing

Issues and pull requests are welcome. Start with
**[docs/VENDOR-API.md](docs/VENDOR-API.md)**, which is the protocol reference
this integration is built on. Two more things to know before you open one:

- **No personal data, ever.** `scripts/check_no_pii.py` runs in CI over the
  **whole repository** — every file a commit would pick up, tracked or not,
  plus the full git history *and* every commit message — and fails the build on
  an email address outside the documentation domains, an RFC1918 address, a
  home-directory path, a 32-hex token, a bare twelve-hex device id, a street
  address or a US state beside a ZIP code. Use `user@example.com` and the
  synthetic fixture ids.

  **Staging is not the boundary.** A brand-new file you have not `git add`ed
  is scanned exactly like a tracked one, because that is where fresh personal
  data actually arrives. Ignored paths (`.gitignore`, so `.venv` and the
  caches) are the only thing left out. Run it before you commit — it is
  standard library only and takes a couple of seconds:

  ```bash
  python scripts/check_no_pii.py
  ```

  A finding in the working tree is a file to fix. A finding marked `history:`
  cannot be fixed by a commit: the value is in the object database and needs a
  history rewrite and a force-push, so it is worth catching before it gets
  there.
- **Tests come first, and they have to be able to fail.** Every guard in this
  repository has been run against a deliberately broken implementation and
  observed to fail before it was believed.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && mypy && pytest -q
python scripts/check_no_pii.py
```

---

## Licence and disclaimer

MIT. See [LICENSE](LICENSE).

**This project is not affiliated with, endorsed by, or supported by Always
Full or its makers.** "Always Full" is used only to say which product this
works with. It is an unofficial, community-built integration that talks to an
undocumented API, and it comes with no warranty of any kind. If it stops
working because the vendor changed something, that is a thing that can happen
and is nobody's fault but entropy's.
