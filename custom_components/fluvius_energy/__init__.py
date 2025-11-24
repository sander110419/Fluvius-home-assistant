"""Home Assistant custom integration for the Fluvius energy API."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import FluviusApiClient
from .const import (
    CONF_DAYS_BACK,
    CONF_EMAIL,
    CONF_EAN,
    CONF_GRANULARITY,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    CONF_PASSWORD,
    CONF_TIMEZONE,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DATA_STORE,
    DEFAULT_DAYS_BACK,
    DEFAULT_GRANULARITY,
    DEFAULT_METER_TYPE,
    DEFAULT_REMEMBER_ME,
    DEFAULT_TIMEZONE,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import FluviusEnergyDataUpdateCoordinator
from .config_flow import FluviusOptionsFlowHandler
from .store import FluviusEnergyStore


def _build_options(entry: ConfigEntry) -> dict:
    options = {
        CONF_TIMEZONE: entry.options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE),
        CONF_DAYS_BACK: entry.options.get(CONF_DAYS_BACK, DEFAULT_DAYS_BACK),
        CONF_GRANULARITY: entry.options.get(CONF_GRANULARITY, DEFAULT_GRANULARITY),
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

    client = FluviusApiClient(
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        ean=entry.data[CONF_EAN],
        meter_serial=entry.data[CONF_METER_SERIAL],
        meter_type=meter_type,
        remember_me=DEFAULT_REMEMBER_ME,
        options=options,
    )

    store = FluviusEnergyStore(hass, entry.entry_id)
    await store.async_load()

    coordinator = FluviusEnergyDataUpdateCoordinator(hass, client, store)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        raise
    except Exception as err:  # pragma: no cover - defensive
        raise ConfigEntryNotReady(str(err)) from err

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_CLIENT: client,
        DATA_COORDINATOR: coordinator,
        DATA_STORE: store,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_get_options_flow(config_entry: ConfigEntry) -> FluviusOptionsFlowHandler:
    return FluviusOptionsFlowHandler(config_entry)
