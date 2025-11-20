"""Sensor platform for the Fluvius Energy integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_EAN, CONF_METER_SERIAL, DATA_COORDINATOR, DOMAIN
from .coordinator import FluviusCoordinatorData, FluviusEnergyDataUpdateCoordinator


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
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="consumption_high",
        translation_key="consumption_high",
        name="Fluvius consumption (high tariff)",
        metric="consumption_high",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="consumption_low",
        translation_key="consumption_low",
        name="Fluvius consumption (low tariff)",
        metric="consumption_low",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="injection_total",
        translation_key="injection_total",
        name="Fluvius injection",
        metric="injection_total",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="injection_high",
        translation_key="injection_high",
        name="Fluvius injection (high tariff)",
        metric="injection_high",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="injection_low",
        translation_key="injection_low",
        name="Fluvius injection (low tariff)",
        metric="injection_low",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    FluviusEnergySensorEntityDescription(
        key="net_consumption_day",
        translation_key="net_consumption_day",
        name="Fluvius net consumption (day)",
        metric="net_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        is_lifetime=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Fluvius sensors."""

    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: FluviusEnergyDataUpdateCoordinator = data[DATA_COORDINATOR]
    ean = entry.data[CONF_EAN]
    meter_serial = entry.data[CONF_METER_SERIAL]

    entities = [
        FluviusEnergySensor(description, coordinator, entry.entry_id, ean, meter_serial)
        for description in SENSOR_TYPES
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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ean)},
            manufacturer="Fluvius",
            model=meter_serial,
            name=f"Fluvius meter {meter_serial}",
        )

    @property
    def native_value(self) -> Optional[float]:
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
    def extra_state_attributes(self) -> Optional[Dict[str, Any]]:
        data: FluviusCoordinatorData | None = self.coordinator.data
        if data is None or data.latest_summary is None:
            return None
        latest = data.latest_summary
        attributes: Dict[str, Any] = {
            "latest_period_start": latest.start.isoformat(),
            "latest_period_end": latest.end.isoformat(),
            "latest_consumption": round(latest.metrics.get("consumption_total", 0.0), 3),
            "latest_injection": round(latest.metrics.get("injection_total", 0.0), 3),
        }
        if not self.entity_description.is_lifetime:
            attributes["latest_net_consumption"] = round(latest.metrics.get("net_consumption", 0.0), 3)
        return attributes
