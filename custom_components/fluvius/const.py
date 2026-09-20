"""Constants for the Fluvius Energy integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "fluvius"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_EAN = "ean"
CONF_METER_SERIAL = "meter_serial"
CONF_DAYS_BACK = "days_back"
CONF_GRANULARITY = "granularity"
CONF_TIMEZONE = "timezone"
CONF_REMEMBER_ME = "remember_me"
CONF_METER_TYPE = "meter_type"
CONF_GAS_UNIT = "gas_unit"
CONF_VERBOSE_LOGGING = "verbose_logging"
CONF_HISTORY_UNTIL = "history_until"

DEFAULT_TIMEZONE = "Europe/Brussels"
DEFAULT_DAYS_BACK = 7
DEFAULT_GRANULARITY = "4"
DEFAULT_REMEMBER_ME = False
DEFAULT_UPDATE_INTERVAL = timedelta(minutes=60)
DEFAULT_METER_TYPE = "electricity"
DEFAULT_GAS_UNIT = "kwh"
DEFAULT_VERBOSE_LOGGING = False
GAS_UNIT_KWH = "kwh"
GAS_UNIT_CUBIC_METERS = "m3"
GAS_UNIT_OPTIONS = (GAS_UNIT_KWH, GAS_UNIT_CUBIC_METERS)
METER_TYPE_ELECTRICITY = "electricity"
METER_TYPE_GAS = "gas"
METER_TYPE_OPTIONS = (METER_TYPE_ELECTRICITY, METER_TYPE_GAS)
GAS_MIN_LOOKBACK_DAYS = 7
GAS_SUPPORTED_GRANULARITY = "4"

# Values used by the Mijn Fluvius meter-measurement-history endpoint.
QUARTER_HOURLY_GRANULARITY = "1"
HOURLY_GRANULARITY = "2"

# Flemish digital meters register electricity per quarter-hour and gas per hour, so
# the resolution to expect depends on the meter type.
INTERVAL_MINUTES_BY_METER_TYPE = {
    METER_TYPE_ELECTRICITY: 15,
    METER_TYPE_GAS: 60,
}

# Fluvius does not document the granularity codes: "1" is confirmed to yield
# 15-minute intervals, "2" hourly ones and "4" daily ones, and the rest is guesswork.
# Rather than rely on a single hard-coded code, the client probes the candidates below
# and keeps the first one that actually returns the expected interval length.
INTERVAL_GRANULARITY_CANDIDATES = {
    METER_TYPE_ELECTRICITY: (QUARTER_HOURLY_GRANULARITY, "3", HOURLY_GRANULARITY),
    METER_TYPE_GAS: (HOURLY_GRANULARITY, QUARTER_HOURLY_GRANULARITY, "3"),
}

# Fluvius registers gas against a "gas day" running 06:00 -> 06:00 local: the daily
# summaries come back spanning 05:00Z -> 05:00Z in winter, not midnight to midnight.
# Interval requests must use the same boundaries, because the statistics layer only
# lets fine readings replace a daily total when they tile that period exactly. A
# calendar-day window never tiles a gas day, so every reading collapses into the
# single 06:00 bucket. Electricity uses plain calendar days.
GAS_DAY_START_HOUR = 6

PLATFORMS: list[Platform] = [Platform.SENSOR]

STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = "fluvius_{entry_id}"

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
