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
