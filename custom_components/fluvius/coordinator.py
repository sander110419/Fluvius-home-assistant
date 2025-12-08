"""DataUpdateCoordinator for the Fluvius Energy integration."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Dict, List

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    FluviusApiClient,
    FluviusApiError,
    FluviusDailySummary,
    FluviusPeakMeasurement,
    FluviusQuarterHourlyMeasurement,
)
from .const import DEFAULT_UPDATE_INTERVAL
from .store import FluviusEnergyStore

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FluviusCoordinatorData:
    """Container returned by the coordinator."""

    latest_summary: FluviusDailySummary | None
    lifetime_totals: Dict[str, float]
    peak_measurements: list[FluviusPeakMeasurement]
    quarter_hourly_measurements: List[FluviusQuarterHourlyMeasurement]


class FluviusEnergyDataUpdateCoordinator(DataUpdateCoordinator[FluviusCoordinatorData]):
    """Periodically fetch and store Fluvius energy data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: FluviusApiClient,
        store: FluviusEnergyStore,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name="Fluvius energy coordinator",
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )
        self._client = client
        self._store = store

    async def _async_update_data(self) -> FluviusCoordinatorData:
        try:
            summaries, peak_measurements = await self._client.fetch_daily_summaries_with_spikes()
        except FluviusApiError as err:
            raise UpdateFailed(str(err)) from err

        if not summaries:
            LOGGER.warning("No daily consumption data returned by the Fluvius API")

        # Quarter-hourly data fetch is separate - don't fail the whole update if it's empty
        quarter_hourly: list[FluviusQuarterHourlyMeasurement] = []
        try:
            quarter_hourly = await self._client.fetch_quarter_hourly_consumption()
        except FluviusApiError as err:
            # Log the error but don't fail - quarter-hourly data may not be available yet
            LOGGER.warning("Could not fetch quarter-hourly data: %s", err)

        for summary in summaries:
            await self._store.async_process_summary(summary.day_id, summary.metrics)

        totals = self._store.get_lifetime_totals()
        latest_summary = summaries[-1] if summaries else None
        return FluviusCoordinatorData(
            latest_summary=latest_summary,
            lifetime_totals=totals,
            peak_measurements=peak_measurements,
            quarter_hourly_measurements=quarter_hourly,
        )
