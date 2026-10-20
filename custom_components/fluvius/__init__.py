"""Home Assistant custom integration for the Fluvius energy API."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .api import FluviusApiClient
from .const import (
    CONF_DAYS_BACK,
    CONF_EAN,
    CONF_EMAIL,
    CONF_GAS_UNIT,
    CONF_GRANULARITY,
    CONF_HISTORY_UNTIL,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    CONF_PASSWORD,
    CONF_TIMEZONE,
    CONF_VERBOSE_LOGGING,
    DEFAULT_DAYS_BACK,
    DEFAULT_GAS_UNIT,
    DEFAULT_GRANULARITY,
    DEFAULT_METER_TYPE,
    DEFAULT_REMEMBER_ME,
    DEFAULT_TIMEZONE,
    DOMAIN,
    GAS_UNIT_KWH,
    METER_TYPE_GAS,
    PLATFORMS,
)
from .coordinator import FluviusEnergyDataUpdateCoordinator
from .http import async_create_fluvius_session
from .models import FluviusRuntimeData
from .statistics import FluviusSharingStatistics, FluviusStatistics, statistic_prefix
from .store import FluviusEnergyStore, FluviusTokenStore

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _build_options(entry: ConfigEntry) -> dict:
    options = {
        CONF_TIMEZONE: entry.options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE),
        CONF_DAYS_BACK: entry.options.get(CONF_DAYS_BACK, DEFAULT_DAYS_BACK),
        CONF_GRANULARITY: entry.options.get(CONF_GRANULARITY, DEFAULT_GRANULARITY),
        CONF_GAS_UNIT: entry.options.get(CONF_GAS_UNIT, DEFAULT_GAS_UNIT),
        CONF_VERBOSE_LOGGING: entry.options.get(CONF_VERBOSE_LOGGING, False),
        CONF_HISTORY_UNTIL: entry.options.get(CONF_HISTORY_UNTIL),
    }
    return options


async def async_setup(hass: HomeAssistant, _: dict) -> bool:
    """Set up the integration via YAML (not supported)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create the Fluvius API client, storage helper and coordinator."""

    hass.data.setdefault(DOMAIN, {})
    options = _build_options(entry)
    meter_type = entry.data.get(CONF_METER_TYPE, DEFAULT_METER_TYPE)
    if CONF_METER_TYPE not in entry.data:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_METER_TYPE: meter_type},
        )

    session = async_create_fluvius_session(hass)
    token_store = FluviusTokenStore(hass, entry.entry_id)

    client = FluviusApiClient(
        session=session,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        ean=entry.data[CONF_EAN],
        meter_serial=entry.data[CONF_METER_SERIAL],
        meter_type=meter_type,
        remember_me=DEFAULT_REMEMBER_ME,
        options=options,
        token_store=token_store,
    )

    store_unit = options.get(CONF_GAS_UNIT, DEFAULT_GAS_UNIT)
    if meter_type != METER_TYPE_GAS:
        store_unit = GAS_UNIT_KWH
    prefix = statistic_prefix(
        entry.data[CONF_EAN], meter_type, store_unit, options[CONF_HISTORY_UNTIL]
    )
    store = FluviusEnergyStore(hass, prefix, store_unit)
    await store.async_load()

    statistics = FluviusStatistics(
        hass,
        prefix,
        f"Fluvius {entry.data[CONF_METER_SERIAL]}",
        "m3" if store_unit == "m3" else "kWh",
    )
    await statistics.async_load()
    sharing_statistics = FluviusSharingStatistics(
        hass,
        prefix,
        f"Fluvius {entry.data[CONF_METER_SERIAL]}",
    )
    await sharing_statistics.async_load()
    coordinator = FluviusEnergyDataUpdateCoordinator(
        hass, client, store, statistics, sharing_statistics
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady, ConfigEntryAuthFailed:
        raise
    except Exception as err:  # pragma: no cover - defensive
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = FluviusRuntimeData(client=client, coordinator=coordinator, store=store)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down a config entry."""

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
