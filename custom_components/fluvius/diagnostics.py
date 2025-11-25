"""Diagnostics support for the Fluvius Energy integration."""
from __future__ import annotations

from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_EAN,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    DEFAULT_METER_TYPE,
    DOMAIN,
)
from .models import FluviusRuntimeData


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> Dict[str, Any]:
    """Return diagnostics for a config entry without exposing secrets."""

    runtime_data: FluviusRuntimeData = entry.runtime_data
    coordinator = runtime_data.coordinator
    store = runtime_data.store
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
        "peak_measurements": [
            {
                "period_start": peak.period_start.isoformat(),
                "period_end": peak.period_end.isoformat(),
                "spike_start": peak.spike_start.isoformat(),
                "spike_end": peak.spike_end.isoformat(),
                "value_kw": peak.value_kw,
            }
            for peak in (coordinator.data.peak_measurements if coordinator.data else [])
        ],
        "store_state": {
            "last_day": store.get_last_day_id(),
        },
    }
    return diagnostics
