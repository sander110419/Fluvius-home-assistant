"""Constants for the Fluvius Energy integration."""
from __future__ import annotations

from datetime import timedelta
from homeassistant.const import Platform

DOMAIN = "fluvius_energy"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_EAN = "ean"
CONF_METER_SERIAL = "meter_serial"
CONF_DAYS_BACK = "days_back"
CONF_GRANULARITY = "granularity"
CONF_TIMEZONE = "timezone"
CONF_REMEMBER_ME = "remember_me"
CONF_METER_TYPE = "meter_type"

DEFAULT_TIMEZONE = "Europe/Brussels"
DEFAULT_DAYS_BACK = 7
DEFAULT_GRANULARITY = "4"
DEFAULT_REMEMBER_ME = False
DEFAULT_UPDATE_INTERVAL = timedelta(minutes=60)
DEFAULT_METER_TYPE = "electricity"
METER_TYPE_ELECTRICITY = "electricity"
METER_TYPE_GAS = "gas"
METER_TYPE_OPTIONS = (METER_TYPE_ELECTRICITY, METER_TYPE_GAS)
GAS_MIN_LOOKBACK_DAYS = 7
GAS_SUPPORTED_GRANULARITY = "4"

PLATFORMS: list[Platform] = [Platform.SENSOR]

STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = "fluvius_energy_{entry_id}"

LIFETIME_METRICS = (
    "consumption_high",
    "consumption_low",
    "injection_high",
    "injection_low",
)

ALL_METRICS = LIFETIME_METRICS + (
    "consumption_total",
    "injection_total",
    "net_consumption",
)
