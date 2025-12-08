"""Platform-level tests for the Fluvius Energy integration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.helpers import entity_registry as er
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

tests_common = pytest.importorskip("tests.common")
MockConfigEntry = tests_common.MockConfigEntry

from custom_components.fluvius.api import FluviusDailySummary, FluviusPeakMeasurement  # noqa: E402
from custom_components.fluvius.const import (  # noqa: E402
    CONF_EAN,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    DOMAIN,
    METER_TYPE_ELECTRICITY,
)


@pytest.mark.asyncio
async def test_sensors_populate_state(hass):
    """End-to-end setup creates sensors with expected values."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_EAN: "541448800000000000",
            CONF_METER_SERIAL: "1SAGTEST",
            CONF_METER_TYPE: METER_TYPE_ELECTRICITY,
        },
    )
    entry.add_to_hass(hass)

    start = datetime(2025, 11, 24, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    summary = FluviusDailySummary(
        day_id=start.isoformat(),
        start=start,
        end=end,
        metrics={
            "consumption_high": 10.0,
            "consumption_low": 5.0,
            "injection_high": 0.0,
            "injection_low": 0.0,
            "consumption_total": 15.0,
            "injection_total": 0.0,
            "net_consumption": 15.0,
        },
    )
    peak = FluviusPeakMeasurement(
        period_start=start,
        period_end=end,
        spike_start=start,
        spike_end=start + timedelta(minutes=15),
        value_kw=5.5,
    )

    with patch(
        "custom_components.fluvius.async_create_fluvius_session",
        return_value=MagicMock(),
    ), patch(
        "custom_components.fluvius.FluviusApiClient.fetch_daily_summaries_with_spikes",
        AsyncMock(return_value=([summary], [peak])),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    # 7 energy sensors + 1 peak power sensor for electricity meters
    assert len(entities) == 8

    entity_ids = {
        desc: registry.async_get_entity_id("sensor", "fluvius", f"{entry.entry_id}_{desc}")
        for desc in [
            "consumption_total",
            "consumption_high",
            "consumption_low",
            "injection_total",
            "injection_high",
            "injection_low",
            "net_consumption_day",
            "peak_power",
        ]
    }
    assert all(entity_ids.values())

    assert float(hass.states.get(entity_ids["consumption_total"]).state) == pytest.approx(15.0)
    assert float(hass.states.get(entity_ids["net_consumption_day"]).state) == pytest.approx(15.0)
    assert float(hass.states.get(entity_ids["peak_power"]).state) == pytest.approx(5.5)
