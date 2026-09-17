"""Constants for the Always Full integration."""

import logging

LOGGER = logging.getLogger(__package__)

DOMAIN = "alwaysfull"
MANUFACTURER = "Always Full"
API_BASE = "https://app.alwaysfull.com/alwaysfull-biz"
APP_ID = "appBiz"
APP_TYPE = "android"
APP_VERSION = "1.2.29"
SIGN_SECRET = "e688769fcccc44cd3fd6f7dsfsdvdse"

CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 600

# `notify/getConfig` is account-level and changes only when the user edits it,
# so polling it every cycle is pure waste against a rate-limited cloud API.
NOTIFY_CONFIG_EVERY_N_POLLS = 10

# How often the "the account returned no bowls" warning repeats while the
# condition persists. It always fires on the transition to zero; this is what
# governs the reminders after that.
#
# Not every poll: at the default sixty-second interval that is 1,440 identical
# lines a day, and a warning nobody can scroll past is a warning nobody reads.
# Not once-only either: a person who restarts Home Assistant an hour after the
# transition would find a silent log describing an integration with no
# entities, which is the exact situation this line exists to explain. Ten
# polls is ten minutes at the default interval and forty at the maximum.
EMPTY_DEVICE_LIST_EVERY_N_POLLS = 10

CODE_OK = "200"

# The session token was rejected. The credentials may still be fine, so this
# is worth exactly one silent re-login.
CODE_TOKEN_EXPIRED = "651"

# The email/password pair itself was rejected. Verified against the live API:
# a real account with a deliberately wrong password AND an email with no
# account BOTH answer `652 "Invalid email address or password."`, so these two
# cases are NOT distinguishable and no logic may try to tell them apart.
CODE_CREDENTIALS_REJECTED = "652"

# Observed once from the same login endpoint and not reproducible on demand.
# Its exact meaning is unknown; what is certain is that it is a refusal to
# authenticate, and treating a refusal as anything else would strand the user
# on "unexpected error" instead of "check your password".
CODE_CREDENTIALS_REJECTED_ALT = "602"

CREDENTIAL_REJECTION_CODES = frozenset(
    {CODE_CREDENTIALS_REJECTED, CODE_CREDENTIALS_REJECTED_ALT}
)

# The vendor's RATE LIMIT, and the only one it has: every rate limit
# observed against the live service arrived as this envelope code under
# HTTP 200, never as HTTP 429. VERIFIED -- eight logins in a few seconds
# answered `603 "Too many requests, please try again later."`
#
# It was previously documented and handled as a generic "system error",
# which is why `AlwaysFullRateLimitError` had never once fired against the
# real service and every rate limit surfaced to users as an unexpected
# failure.
#
# CAVEAT, and it cannot be resolved from here: `603` is ALSO returned by
# `/app/ota/check` and by `/app/pay/get/product` on an account with no
# subscription, neither of which is plausibly a rate limit. So the vendor
# has either overloaded one code or genuinely means "temporarily
# unavailable" by it. We cannot distinguish the readings, and we do not
# try: "back off and retry later" is the correct response to all of them,
# and it is the only reading that cannot trap a user in a reauth loop.
CODE_RATE_LIMITED = "603"

# How many re-logins this integration will attempt for one account in any
# `RELOGIN_WINDOW_SECONDS`, across BOTH the poll path and the write path.
#
# The vendor allows one session per account, so a second client -- the
# owner's phone app, or a second Home Assistant -- evicts this one every
# time it talks to the server. With a re-login on every rejected session,
# two clients sharing an account do not settle: each re-login invalidates
# the other's token, that client re-logins, and both sides spend requests
# as fast as the network allows. That is a login storm, and a login storm
# is exactly what the vendor answers with `603` (see `CODE_RATE_LIMITED`):
# the measured trigger was roughly EIGHT logins in a few seconds.
#
# The bound is three attempts per five minutes, and both numbers are
# chosen rather than round:
#
# - Three, not one, because a burst is the ORDINARY case. Somebody opening
#   their phone app, looking at two screens and closing it evicts us
#   several times in a row, and every one of those is a session this
#   integration should heal silently. A single-attempt bound would make a
#   normal minute of app use look like a fault.
# - Three per five minutes is at most 36 logins an hour in the very worst
#   case, against an observed limit of about eight in a few SECONDS. Two
#   orders of magnitude of headroom, on a limit whose real threshold is
#   not documented and which we must not probe (see `docs/VENDOR-API.md`,
#   "Known unknowns").
# - Five minutes, because the recovery it gates has to be quicker than a
#   person noticing and restarting Home Assistant, and slower than the
#   30-second floor on the poll interval -- a bound shorter than the poll
#   interval would not bound anything.
#
# A healthy installation never reaches this. One re-login heals a session
# and the next poll succeeds; the budget only bites when re-logins keep
# being needed, which is precisely the contention case.
#
# What happens past the bound is `AlwaysFullReloginThrottledError`, which
# is NOT an auth error: the poll degrades to `UpdateFailed` (stale, and
# retried on the next cycle) rather than pushing the user into a reauth
# prompt that would succeed and fix nothing. The window is a sliding one,
# so the budget refills on its own -- there is no state a restart clears
# that time would not.
RELOGIN_MAX_ATTEMPTS = 3
RELOGIN_WINDOW_SECONDS = 300

# The one endpoint that authenticates with the stored email/password pair
# rather than with a token. Which endpoint answered a credential-rejection
# code decides what that code MEANS -- see `api.AlwaysFullClient._handle_response`.
LOGIN_PATH = "/app/user/login"

ALERT_TYPES = [
    "Tilted",
    "Daily_Maximum",
    "Fill_Failed",
    "Not_Attached",
    "High_Water_Level",
    "Replace_Wall_Filter",
    "Replace_Bowl_Filter",
    "Daily_Decreased",
    "Operation_Confirmation",
    "Hardware_Fault",
]

# The value reported when the vendor sends something outside the enum we
# know. Deliberately the same string Home Assistant uses for "no value":
# the thing really is unknown to us, and the vendor's own app labels
# anything outside its enum "Unknown" too.
UNKNOWN = "unknown"

# The vendor's own alert spelling -> the option this integration reports.
#
# It lives here, not in a platform module, because TWO platforms report it:
# the `last_alert` sensor's enum options and the alert event entity's
# `event_types`. Two lists that disagreed on casing -- or drifted apart by
# one entry -- would be a trap for anyone writing automations against both,
# so there is exactly one mapping and both platforms import it.
#
# Written out in full rather than derived with `.lower()` so that a vendor
# type which is NOT a plain lowercasing (say `HighWaterLevel`) cannot
# silently produce a new, undeclared option.
#
# Normalising throws information away, so the vendor's exact string is also
# published verbatim, in the `raw_type` attribute of both platforms.
ALERT_TYPE_OPTIONS = {
    "Tilted": "tilted",
    "Daily_Maximum": "daily_maximum",
    "Fill_Failed": "fill_failed",
    "Not_Attached": "not_attached",
    "High_Water_Level": "high_water_level",
    "Replace_Wall_Filter": "replace_wall_filter",
    "Replace_Bowl_Filter": "replace_bowl_filter",
    "Daily_Decreased": "daily_decreased",
    "Operation_Confirmation": "operation_confirmation",
    "Hardware_Fault": "hardware_fault",
}

# Everything an alert can be reported as: the ten known options plus the
# fallback. The sensor publishes this as its enum `options` and the event
# entity as its `event_types`, so the two can never disagree.
#
# A TUPLE, and each platform is handed its own `list(...)` copy. Home
# Assistant wants a list in both places, and one shared list handed to two
# entity descriptions is a mutable global that anything holding a reference
# could reorder or extend for both platforms at once. Nothing does that
# today; this removes the possibility rather than relying on nobody trying.
ALERT_OPTIONS = (*ALERT_TYPE_OPTIONS.values(), UNKNOWN)

# The vendor's untouched `type` string, carried alongside the normalised
# value by every entity that normalises it. Without it, an alert type the
# vendor ships after this table was written is indistinguishable from no
# alert at all.
ATTR_RAW_TYPE = "raw_type"
