"""Diagnostics support for the Fluvius Energy integration."""
from __future__ import annotations

from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_EAN,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    DATA_COORDINATOR,
    DATA_STORE,
    DEFAULT_METER_TYPE,
    DOMAIN,
)
from .coordinator import FluviusEnergyDataUpdateCoordinator
from .store import FluviusEnergyStore


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> Dict[str, Any]:
    """Return diagnostics for a config entry without exposing secrets."""

    domain_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: FluviusEnergyDataUpdateCoordinator = domain_data[DATA_COORDINATOR]
    store: FluviusEnergyStore = domain_data[DATA_STORE]
    latest = coordinator.data.latest_summary if coordinator.data else None

    diagnostics: Dict[str, Any] = {
        "config": {
            "ean": entry.data[CONF_EAN],
            "meter_serial": entry.data[CONF_METER_SERIAL],
            "meter_type": entry.data.get(CONF_METER_TYPE, DEFAULT_METER_TYPE),
        },
        "lifetime_totals": coordinator.data.lifetime_totals if coordinator.data else {},
        "latest_day": {
            "day_id": latest.day_id if latest else None,
            "start": latest.start.isoformat() if latest else None,
            "end": latest.end.isoformat() if latest else None,
            "metrics": latest.metrics if latest else {},
        },
        "store_state": {
            "last_day": store.get_last_day_id(),
        },
    }
    return diagnostics
