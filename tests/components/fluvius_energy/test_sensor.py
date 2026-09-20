"""Platform-level tests for the Fluvius Energy integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fluvius.api import FluviusDailySummary, FluviusPeakMeasurement  # noqa: E402
from custom_components.fluvius.const import (  # noqa: E402
    CONF_EAN,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    DOMAIN,
    METER_TYPE_ELECTRICITY,
)

pytestmark = pytest.mark.usefixtures("recorder_mock")


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

    start = datetime(2025, 11, 24, tzinfo=UTC)
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

    with (
        patch(
            "custom_components.fluvius.async_create_fluvius_session",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.fluvius.FluviusApiClient.fetch_daily_summaries_with_spikes",
            AsyncMock(return_value=([summary], [peak])),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    # 7 energy sensors, 1 peak sensor and 2 interval display sensors.
    assert len(entities) == 10

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


async def test_gas_volume_sensor_classes(hass):
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

    from custom_components.fluvius.coordinator import FluviusEnergyDataUpdateCoordinator
    from custom_components.fluvius.models import FluviusRuntimeData
    from custom_components.fluvius.sensor import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EAN: "541448800000000000", CONF_METER_SERIAL: "TEST", CONF_METER_TYPE: "gas"},
        options={"gas_unit": "m3"},
    )
    entry.add_to_hass(hass)
    coordinator = FluviusEnergyDataUpdateCoordinator(hass, MagicMock(), MagicMock())
    entry.runtime_data = FluviusRuntimeData(
        client=MagicMock(), coordinator=coordinator, store=MagicMock()
    )
    entities = []
    await async_setup_entry(hass, entry, entities.extend)
    assert len(entities) == 5
    for entity in entities:
        assert (
            entity.native_unit_of_measurement == "m³" or entity.native_unit_of_measurement == "m3"
        )
        if entity.device_class == SensorDeviceClass.GAS:
            assert entity.state_class in (None, SensorStateClass.TOTAL)
    interval = next(e for e in entities if e.entity_description.key == "quarter_hourly_consumption")
    assert interval.entity_description.translation_key == "hourly_consumption"
    assert interval.state_class is None
    # A gas meter only ever consumes, so an injection sensor would sit at zero forever.
    assert not [e for e in entities if "injection" in e.entity_description.key]


async def test_diagnostics_report_the_granularity_probe(hass):
    """The probe outcome is recorded, so an empty interval import is not opaque."""

    from custom_components.fluvius.api import FluviusApiClient, FluviusQuarterHourlyMeasurement
    from custom_components.fluvius.coordinator import (
        FluviusCoordinatorData,
        FluviusEnergyDataUpdateCoordinator,
    )
    from custom_components.fluvius.diagnostics import async_get_config_entry_diagnostics
    from custom_components.fluvius.models import FluviusRuntimeData

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EAN: "541448800000000000",
            CONF_METER_SERIAL: "TEST",
            CONF_METER_TYPE: METER_TYPE_ELECTRICITY,
        },
    )
    entry.add_to_hass(hass)

    client = FluviusApiClient(
        session=MagicMock(),
        email="user@example.com",
        password="secret",
        ean="541448800000000000",
        meter_serial="TEST",
    )
    client._interval_granularity = "1"
    client._probe_outcomes = {"3": "no data", "1": "15-minute intervals"}

    start = datetime(2026, 4, 1, 10, tzinfo=UTC)
    coordinator = FluviusEnergyDataUpdateCoordinator(hass, client, MagicMock())
    coordinator.data = FluviusCoordinatorData(
        None,
        {},
        [],
        [FluviusQuarterHourlyMeasurement(start, start + timedelta(minutes=15), 0.5, 0.125, {})],
    )
    entry.runtime_data = FluviusRuntimeData(
        client=client,
        coordinator=coordinator,
        store=MagicMock(get_last_day_id=lambda: None),
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["interval_granularity"] == {
        "expected_interval_minutes": 15,
        "resolved_granularity": "1",
        "probe_outcomes": {"3": "no data", "1": "15-minute intervals"},
        "unavailable": False,
    }
    assert result["interval_data"]["interval_count"] == 1
    assert result["interval_data"]["consumption_sum"] == pytest.approx(0.5)
    assert result["interval_data"]["injection_sum"] == pytest.approx(0.125)
