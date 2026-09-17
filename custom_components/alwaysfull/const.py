"""Constants for the Always Full integration."""

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
