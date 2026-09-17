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
ALERT_OPTIONS = [*ALERT_TYPE_OPTIONS.values(), UNKNOWN]

# The vendor's untouched `type` string, carried alongside the normalised
# value by every entity that normalises it. Without it, an alert type the
# vendor ships after this table was written is indistinguishable from no
# alert at all.
ATTR_RAW_TYPE = "raw_type"
