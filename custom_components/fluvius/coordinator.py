"""DataUpdateCoordinator for the Fluvius Energy integration."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import time
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
        start_time = time.monotonic()
        LOGGER.debug("=== FLUVIUS UPDATE START ===")
        
        # Step 1: Fetch daily summaries and peak power
        try:
            LOGGER.debug("Step 1/3: Fetching daily consumption summaries and peak power...")
            summaries, peak_measurements = await self._client.fetch_daily_summaries_with_spikes()
            LOGGER.debug(
                "Step 1/3: SUCCESS - Received %d daily summaries, %d peak measurements",
                len(summaries),
                len(peak_measurements),
            )
        except FluviusApiError as err:
            elapsed = time.monotonic() - start_time
            LOGGER.error(
                "=== FLUVIUS UPDATE FAILED (%.2fs) === Step 1/3 failed: %s. "
                "Check the errors above for more details. Common causes: "
                "1) Authentication expired - try reloading the integration, "
                "2) Fluvius service is temporarily unavailable, "
                "3) Invalid EAN or meter serial number.",
                elapsed,
                err,
            )
            raise UpdateFailed(str(err)) from err

        if not summaries:
            LOGGER.warning(
                "Step 1/3: WARNING - No daily consumption data returned. "
                "This can happen if: 1) Your meter is newly installed, "
                "2) Fluvius hasn't processed recent data yet, "
                "3) The configured date range has no data."
            )

        # Step 2: Fetch quarter-hourly data (non-blocking on failure)
        quarter_hourly: list[FluviusQuarterHourlyMeasurement] = []
        try:
            LOGGER.debug("Step 2/3: Fetching quarter-hourly (15-minute) consumption data...")
            quarter_hourly = await self._client.fetch_quarter_hourly_consumption()
            LOGGER.debug("Step 2/3: SUCCESS - Received %d quarter-hourly intervals", len(quarter_hourly))
        except FluviusApiError as err:
            LOGGER.warning(
                "Step 2/3: SKIPPED - Could not fetch quarter-hourly data: %s. "
                "This is non-fatal; daily data will still work. "
                "Quarter-hourly data may not be available for all meters.",
                err,
            )

        # Step 3: Process and store data
        LOGGER.debug("Step 3/3: Processing and storing %d summaries...", len(summaries))
        for summary in summaries:
            await self._store.async_process_summary(summary.day_id, summary.metrics)

        totals = self._store.get_lifetime_totals()
        latest_summary = summaries[-1] if summaries else None
        
        elapsed = time.monotonic() - start_time
        LOGGER.debug(
            "=== FLUVIUS UPDATE COMPLETE (%.2fs) === "
            "Daily summaries: %d, Peak measurements: %d, Quarter-hourly intervals: %d",
            elapsed,
            len(summaries),
            len(peak_measurements),
            len(quarter_hourly),
        )
        
        return FluviusCoordinatorData(
            latest_summary=latest_summary,
            lifetime_totals=totals,
            peak_measurements=peak_measurements,
            quarter_hourly_measurements=quarter_hourly,
        )
