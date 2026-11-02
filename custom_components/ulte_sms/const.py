"""Constants for ulte_sms."""
from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "ulte_sms"

CONF_PRIVATE_KEY: Final = "private_key"
CONF_HOST_KEY: Final = "host_key"

OPT_SCAN_INTERVAL: Final = "scan_interval"
OPT_DELETE: Final = "delete_after_receive"
OPT_RECIPIENTS: Final = "default_recipients"
OPT_ACCEPT_HOST_KEY: Final = "accept_host_key_change"

DEFAULT_PORT: Final = 22
DEFAULT_SCAN_INTERVAL: Final = 60  # seconds
MIN_SCAN_INTERVAL: Final = 15
MAX_SCAN_INTERVAL: Final = 3600
DEFAULT_DELETE: Final = True
DEFAULT_ACCEPT_HOST_KEY: Final = False

# Multipart SMS whose parts are still missing are delivered incomplete after
# this time (the oldest part's SMSC timestamp counts).
INCOMPLETE_AFTER: Final = timedelta(hours=1)

EVENT_RECEIVED: Final = "ulte_sms_received"
SIGNAL_RECEIVED: Final = f"{DOMAIN}_received"

SERVICE_SEND_SMS: Final = "send_sms"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_RECIPIENTS: Final = "recipients"
ATTR_MESSAGE: Final = "message"
