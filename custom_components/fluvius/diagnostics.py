"""Diagnostics support for the Fluvius Energy integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_EAN,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    DEFAULT_METER_TYPE,
)
from .coordinator import FluviusCoordinatorData
from .models import FluviusRuntimeData


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry without exposing secrets."""

    runtime_data: FluviusRuntimeData = entry.runtime_data
    coordinator = runtime_data.coordinator
    client = runtime_data.client
    store = runtime_data.store
    latest = coordinator.data.latest_summary if coordinator.data else None

    diagnostics: dict[str, Any] = {
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
        # Fluvius does not document its granularity codes, so record what the probe
        # asked and what came back: an empty interval import is otherwise opaque.
        "interval_granularity": {
            "expected_interval_minutes": client.interval_minutes,
            "resolved_granularity": client.resolved_granularity,
            "probe_outcomes": client.probe_outcomes,
            "unavailable": client.interval_unavailable,
        },
        "interval_data": _interval_diagnostics(coordinator.data),
        "store_state": {
            "last_day": store.get_last_day_id(),
        },
    }
    return diagnostics


def _interval_diagnostics(data: FluviusCoordinatorData | None) -> dict[str, Any]:
    """Summarise the interval measurements without dumping hundreds of rows."""

    measurements = data.quarter_hourly_measurements if data else []
    if not measurements:
        return {"interval_count": 0, "intervals": []}
    return {
        "interval_count": len(measurements),
        "first_start": measurements[0].start.isoformat(),
        "last_end": measurements[-1].end.isoformat(),
        "consumption_sum": round(sum(item.consumption for item in measurements), 4),
        "injection_sum": round(sum(item.injection for item in measurements), 4),
        # Keep the payload readable: only the first and last few intervals.
        "intervals": [
            {
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "consumption": item.consumption,
                "injection": item.injection,
            }
            for item in (measurements[:5] + measurements[-5:])
        ],
    }
