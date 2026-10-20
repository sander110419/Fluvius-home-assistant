"""Sensor platform for the Fluvius Energy integration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    FluviusEnergySharingSummary,
    FluviusPeakMeasurement,
    FluviusQuarterHourlyMeasurement,
)
from .const import (
    CONF_EAN,
    CONF_GAS_UNIT,
    CONF_HISTORY_UNTIL,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    DEFAULT_GAS_UNIT,
    DEFAULT_METER_TYPE,
    DOMAIN,
    GAS_UNIT_CUBIC_METERS,
    METER_TYPE_ELECTRICITY,
    METER_TYPE_GAS,
)
from .coordinator import FluviusCoordinatorData, FluviusEnergyDataUpdateCoordinator
from .models import FluviusRuntimeData
from .statistics import statistic_prefix


@dataclass(frozen=True, slots=True, kw_only=True)
class FluviusEnergySensorEntityDescription(SensorEntityDescription):
    """Describe a Fluvius Energy sensor."""

    metric: str
    is_lifetime: bool = True


SENSOR_TYPES: tuple[FluviusEnergySensorEntityDescription, ...] = (
    FluviusEnergySensorEntityDescription(
        key="consumption_total",
        translation_key="consumption_total",
        name="Fluvius consumption",
        metric="consumption_total",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="consumption_high",
        translation_key="consumption_high",
        name="Fluvius consumption (high tariff)",
        metric="consumption_high",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="consumption_low",
        translation_key="consumption_low",
        name="Fluvius consumption (low tariff)",
        metric="consumption_low",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="injection_total",
        translation_key="injection_total",
        name="Fluvius injection",
        metric="injection_total",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="injection_high",
        translation_key="injection_high",
        name="Fluvius injection (high tariff)",
        metric="injection_high",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="injection_low",
        translation_key="injection_low",
        name="Fluvius injection (low tariff)",
        metric="injection_low",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="net_consumption_day",
        translation_key="net_consumption_day",
        name="Fluvius net consumption (day)",
        metric="net_consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        is_lifetime=False,
    ),
)

PEAK_POWER_DESCRIPTION = SensorEntityDescription(
    key="peak_power",
    translation_key="peak_power",
    name="Fluvius peak power",
    device_class=SensorDeviceClass.POWER,
    native_unit_of_measurement=UnitOfPower.KILO_WATT,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=3,
)

# Quarter-hourly (15-minute interval) sensor descriptions
# These sensors display the latest interval, with the latest local day in attributes.
# Historical Energy sources are imported separately by statistics.py.
QUARTER_HOURLY_CONSUMPTION_DESCRIPTION = SensorEntityDescription(
    key="quarter_hourly_consumption",
    translation_key="quarter_hourly_consumption",
    name="Fluvius consumption (quarter-hourly)",
    device_class=SensorDeviceClass.ENERGY,
    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    state_class=None,
    suggested_display_precision=3,
)

QUARTER_HOURLY_INJECTION_DESCRIPTION = SensorEntityDescription(
    key="quarter_hourly_injection",
    translation_key="quarter_hourly_injection",
    name="Fluvius injection (quarter-hourly)",
    device_class=SensorDeviceClass.ENERGY,
    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    state_class=None,
    suggested_display_precision=3,
)

# Energy sharing (energiedelen): per exchange, received/injected x today/month/year.
# The metric key matches FluviusEnergySharingSummary.totals.
ENERGY_SHARING_SENSOR_KINDS: tuple[tuple[str, str, str], ...] = (
    ("received_today", "energy_sharing_received_today", "Fluvius shared energy received (today)"),
    (
        "received_month",
        "energy_sharing_received_month",
        "Fluvius shared energy received (this month)",
    ),
    (
        "received_year",
        "energy_sharing_received_year",
        "Fluvius shared energy received (this year)",
    ),
    ("injected_today", "energy_sharing_injected_today", "Fluvius shared energy injected (today)"),
    (
        "injected_month",
        "energy_sharing_injected_month",
        "Fluvius shared energy injected (this month)",
    ),
    (
        "injected_year",
        "energy_sharing_injected_year",
        "Fluvius shared energy injected (this year)",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Fluvius sensors."""

    runtime_data: FluviusRuntimeData = entry.runtime_data
    coordinator: FluviusEnergyDataUpdateCoordinator = runtime_data.coordinator
    ean = entry.data[CONF_EAN]
    meter_serial = entry.data[CONF_METER_SERIAL]
    meter_type = entry.data.get(CONF_METER_TYPE, DEFAULT_METER_TYPE)
    gas_unit = entry.options.get(CONF_GAS_UNIT, DEFAULT_GAS_UNIT)
    use_gas_volume = meter_type == METER_TYPE_GAS and gas_unit == GAS_UNIT_CUBIC_METERS

    descriptions = SENSOR_TYPES
    if use_gas_volume:
        descriptions = [
            replace(
                description,
                device_class=SensorDeviceClass.GAS if description.is_lifetime else None,
                state_class=SensorStateClass.TOTAL if description.is_lifetime else None,
                native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            )
            for description in SENSOR_TYPES
        ]

    entities = [
        FluviusEnergySensor(description, coordinator, entry.entry_id, ean, meter_serial)
        for description in descriptions
    ]
    if meter_type == METER_TYPE_ELECTRICITY:
        entities.append(
            FluviusPeakPowerSensor(
                PEAK_POWER_DESCRIPTION, coordinator, entry.entry_id, ean, meter_serial
            )
        )

    # Add quarter-hourly consumption and injection sensors
    quarter_hourly_consumption_desc = QUARTER_HOURLY_CONSUMPTION_DESCRIPTION
    quarter_hourly_injection_desc = QUARTER_HOURLY_INJECTION_DESCRIPTION
    if meter_type == METER_TYPE_GAS:
        quarter_hourly_consumption_desc = replace(
            quarter_hourly_consumption_desc,
            name="Fluvius consumption (hourly)",
            translation_key="hourly_consumption",
        )
    if use_gas_volume:
        quarter_hourly_consumption_desc = replace(
            quarter_hourly_consumption_desc,
            device_class=SensorDeviceClass.GAS,
            native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )
        quarter_hourly_injection_desc = replace(
            quarter_hourly_injection_desc,
            device_class=SensorDeviceClass.GAS,
            native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )
    entities.append(
        FluviusQuarterHourlyConsumptionSensor(
            quarter_hourly_consumption_desc, coordinator, entry.entry_id, ean, meter_serial
        )
    )
    entities.append(
        FluviusQuarterHourlyInjectionSensor(
            quarter_hourly_injection_desc, coordinator, entry.entry_id, ean, meter_serial
        )
    )
    unit = gas_unit if meter_type == METER_TYPE_GAS else "kwh"
    prefix = statistic_prefix(ean, meter_type, unit, entry.options.get(CONF_HISTORY_UNTIL))

    # Energy sharing (energiedelen): one sensor set per energy exchange. The
    # coordinator already fetched sharing data during the first refresh, so
    # accounts without energiedelen simply get no extra entities.
    sharing_data = coordinator.data.energy_sharing if coordinator.data else []
    if meter_type != METER_TYPE_GAS:
        for summary in sharing_data:
            if len(sharing_data) > 1:
                # Multiple exchanges: always disambiguate, even when Fluvius
                # reports identical (or no) names for the agreements.
                label = f"{summary.name} " if summary.name else ""
                suffix = f" ({label}#{summary.exchange_id})"
            else:
                suffix = ""
            for metric, translation_key, name in ENERGY_SHARING_SENSOR_KINDS:
                description = SensorEntityDescription(
                    key=f"energy_sharing_{metric}",
                    # translation_key wins over name, so only use it when no
                    # disambiguating suffix is present.
                    translation_key=None if suffix else translation_key,
                    name=f"{name}{suffix}",
                    device_class=SensorDeviceClass.ENERGY,
                    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                    state_class=SensorStateClass.TOTAL,
                    suggested_display_precision=3,
                )
                entities.append(
                    FluviusEnergySharingSensor(
                        description,
                        coordinator,
                        entry.entry_id,
                        ean,
                        meter_serial,
                        summary.exchange_id,
                        metric,
                    )
                )

    for entity in entities:
        metric = entity.entity_description.key
        if metric.startswith("quarter_hourly_"):
            metric = metric.removeprefix("quarter_hourly_") + "_total"
        if metric in {
            "consumption_total",
            "consumption_high",
            "consumption_low",
            "injection_total",
            "injection_high",
            "injection_low",
        }:
            entity._historical_statistic_id = f"{DOMAIN}:{prefix}_{metric}"
    if meter_type == METER_TYPE_GAS:
        entities = [
            entity for entity in entities if "injection" not in entity.entity_description.key
        ]
    async_add_entities(entities)


class FluviusEnergySensor(CoordinatorEntity[FluviusEnergyDataUpdateCoordinator], SensorEntity):
    """Define a Fluvius energy sensor."""

    entity_description: FluviusEnergySensorEntityDescription

    def __init__(
        self,
        description: FluviusEnergySensorEntityDescription,
        coordinator: FluviusEnergyDataUpdateCoordinator,
        entry_id: str,
        ean: str,
        meter_serial: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            manufacturer="Fluvius",
            model=meter_serial,
            name=f"Fluvius meter {meter_serial}",
        )

    @property
    def native_value(self) -> float | None:
        data: FluviusCoordinatorData | None = self.coordinator.data
        if data is None:
            return None
        metric = self.entity_description.metric
        if self.entity_description.is_lifetime:
            value = data.lifetime_totals.get(metric)
        else:
            latest = data.latest_summary
            value = latest.metrics.get(metric) if latest else None
        if value is None:
            return None
        return round(value, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data: FluviusCoordinatorData | None = self.coordinator.data
        if data is None or data.latest_summary is None:
            return None
        latest = data.latest_summary
        attributes: dict[str, Any] = {
            "historical_statistic_id": getattr(self, "_historical_statistic_id", None),
            "latest_period_start": latest.start.isoformat(),
            "latest_period_end": latest.end.isoformat(),
            "latest_consumption": round(latest.metrics.get("consumption_total", 0.0), 3),
            "latest_injection": round(latest.metrics.get("injection_total", 0.0), 3),
        }
        if not self.entity_description.is_lifetime:
            attributes["latest_net_consumption"] = round(
                latest.metrics.get("net_consumption", 0.0), 3
            )
        return attributes


class FluviusPeakPowerSensor(CoordinatorEntity[FluviusEnergyDataUpdateCoordinator], SensorEntity):
    """Expose the monthly peak power reported by Fluvius."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        description: SensorEntityDescription,
        coordinator: FluviusEnergyDataUpdateCoordinator,
        entry_id: str,
        ean: str,
        meter_serial: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            manufacturer="Fluvius",
            model=meter_serial,
            name=f"Fluvius meter {meter_serial}",
        )

    def _latest_peak(self) -> FluviusPeakMeasurement | None:
        data: FluviusCoordinatorData | None = self.coordinator.data
        if not data or not data.peak_measurements:
            return None
        return data.peak_measurements[-1]

    @property
    def native_value(self) -> float | None:
        latest = self._latest_peak()
        if not latest:
            return None
        return round(latest.value_kw, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data: FluviusCoordinatorData | None = self.coordinator.data
        latest = self._latest_peak()
        if not data or not latest:
            return None
        history = {
            peak.spike_start.strftime("%Y-%m"): round(peak.value_kw, 3)
            for peak in data.peak_measurements[-12:]
        }
        return {
            "period_start": latest.period_start.isoformat(),
            "period_end": latest.period_end.isoformat(),
            "spike_window_start": latest.spike_start.isoformat(),
            "spike_window_end": latest.spike_end.isoformat(),
            "monthly_peaks_kw": history,
        }


class FluviusQuarterHourlyConsumptionSensor(
    CoordinatorEntity[FluviusEnergyDataUpdateCoordinator], SensorEntity
):
    """Expose the latest quarter-hourly (15-minute) consumption data."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        description: SensorEntityDescription,
        coordinator: FluviusEnergyDataUpdateCoordinator,
        entry_id: str,
        ean: str,
        meter_serial: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            manufacturer="Fluvius",
            model=meter_serial,
            name=f"Fluvius meter {meter_serial}",
        )

    def _latest_measurement(self) -> FluviusQuarterHourlyMeasurement | None:
        """Get the most recent quarter-hourly measurement."""
        data: FluviusCoordinatorData | None = self.coordinator.data
        if not data or not data.quarter_hourly_measurements:
            return None
        return data.quarter_hourly_measurements[-1]

    @property
    def native_value(self) -> float | None:
        """Return the latest interval, without presenting a rolling sum as a meter."""
        data: FluviusCoordinatorData | None = self.coordinator.data
        if not data or not data.quarter_hourly_measurements:
            return None
        return round(data.quarter_hourly_measurements[-1].consumption, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data: FluviusCoordinatorData | None = self.coordinator.data
        latest = self._latest_measurement()
        if not data or not latest:
            return None

        # Select a local calendar day, including all intervals on DST transition days.
        from zoneinfo import ZoneInfo

        from .const import DEFAULT_TIMEZONE

        tz = ZoneInfo(DEFAULT_TIMEZONE)
        day = latest.start.astimezone(tz).date()
        recent_intervals = [
            m for m in data.quarter_hourly_measurements if m.start.astimezone(tz).date() == day
        ]
        hourly_data = {m.start.isoformat(): round(m.consumption, 3) for m in recent_intervals}

        # Calculate totals for the last day of data
        last_day_consumption = sum(m.consumption for m in recent_intervals)

        # Get the date range of available data
        first_measurement = (
            data.quarter_hourly_measurements[0] if data.quarter_hourly_measurements else None
        )

        return {
            "historical_statistic_id": getattr(self, "_historical_statistic_id", None),
            "period_start": latest.start.isoformat(),
            "period_end": latest.end.isoformat(),
            "data_from": first_measurement.start.isoformat() if first_measurement else None,
            "data_until": latest.end.isoformat(),
            "last_day_total": round(last_day_consumption, 3),
            "last_interval_value": round(latest.consumption, 3),
            "interval_count": len(data.quarter_hourly_measurements),
            "quarter_hourly_consumption": hourly_data,
        }


class FluviusQuarterHourlyInjectionSensor(
    CoordinatorEntity[FluviusEnergyDataUpdateCoordinator], SensorEntity
):
    """Expose the latest quarter-hourly (15-minute) injection data."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        description: SensorEntityDescription,
        coordinator: FluviusEnergyDataUpdateCoordinator,
        entry_id: str,
        ean: str,
        meter_serial: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            manufacturer="Fluvius",
            model=meter_serial,
            name=f"Fluvius meter {meter_serial}",
        )

    def _latest_measurement(self) -> FluviusQuarterHourlyMeasurement | None:
        """Get the most recent quarter-hourly measurement."""
        data: FluviusCoordinatorData | None = self.coordinator.data
        if not data or not data.quarter_hourly_measurements:
            return None
        return data.quarter_hourly_measurements[-1]

    @property
    def native_value(self) -> float | None:
        """Return the latest injection interval."""
        data: FluviusCoordinatorData | None = self.coordinator.data
        if not data or not data.quarter_hourly_measurements:
            return None
        return round(data.quarter_hourly_measurements[-1].injection, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data: FluviusCoordinatorData | None = self.coordinator.data
        latest = self._latest_measurement()
        if not data or not latest:
            return None

        # Select a local calendar day, including all intervals on DST transition days.
        from zoneinfo import ZoneInfo

        from .const import DEFAULT_TIMEZONE

        tz = ZoneInfo(DEFAULT_TIMEZONE)
        day = latest.start.astimezone(tz).date()
        recent_intervals = [
            m for m in data.quarter_hourly_measurements if m.start.astimezone(tz).date() == day
        ]
        hourly_data = {m.start.isoformat(): round(m.injection, 3) for m in recent_intervals}

        # Calculate totals for the last day of data
        last_day_injection = sum(m.injection for m in recent_intervals)

        # Get the date range of available data
        first_measurement = (
            data.quarter_hourly_measurements[0] if data.quarter_hourly_measurements else None
        )

        return {
            "historical_statistic_id": getattr(self, "_historical_statistic_id", None),
            "period_start": latest.start.isoformat(),
            "period_end": latest.end.isoformat(),
            "data_from": first_measurement.start.isoformat() if first_measurement else None,
            "data_until": latest.end.isoformat(),
            "last_day_total": round(last_day_injection, 3),
            "last_interval_value": round(latest.injection, 3),
            "interval_count": len(data.quarter_hourly_measurements),
            "quarter_hourly_injection": hourly_data,
        }


class FluviusEnergySharingSensor(
    CoordinatorEntity[FluviusEnergyDataUpdateCoordinator], SensorEntity
):
    """Expose energiedelen (energy sharing) totals for one energy exchange."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        description: SensorEntityDescription,
        coordinator: FluviusEnergyDataUpdateCoordinator,
        entry_id: str,
        ean: str,
        meter_serial: str,
        exchange_id: int,
        metric: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._exchange_id = exchange_id
        self._metric = metric
        self._attr_unique_id = f"{entry_id}_energy_sharing_{exchange_id}_{metric}"
        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            manufacturer="Fluvius",
            model=meter_serial,
            name=f"Fluvius meter {meter_serial}",
        )

    def _summary(self) -> FluviusEnergySharingSummary | None:
        data: FluviusCoordinatorData | None = self.coordinator.data
        if not data:
            return None
        for summary in data.energy_sharing:
            if summary.exchange_id == self._exchange_id:
                return summary
        return None

    @property
    def native_value(self) -> float | None:
        summary = self._summary()
        if not summary or not summary.totals:
            return None
        value = summary.totals.get(self._metric)
        return round(value, 3) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        summary = self._summary()
        if not summary:
            return None
        attributes: dict[str, Any] = {
            "energy_exchange_id": summary.exchange_id,
            "energy_exchange_name": summary.name,
            "daily_buckets": len(summary.daily),
            "monthly_buckets": len(summary.monthly),
        }
        if self._metric.endswith("_year"):
            # Full monthly history (including backfilled years) for inspection.
            field = "consumption" if self._metric.startswith("received") else "injection"
            attributes["monthly_history_kwh"] = {
                measurement.start.astimezone(UTC).isoformat()[:7]: round(
                    getattr(measurement, field), 3
                )
                for measurement in summary.monthly
            }
        return attributes
