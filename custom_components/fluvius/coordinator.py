"""Fetch delayed Fluvius readings and publish their historical statistics."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    FluviusApiClient,
    FluviusApiError,
    FluviusAuthenticationError,
    FluviusDailySummary,
    FluviusEnergySharingSummary,
    FluviusPeakMeasurement,
    FluviusQuarterHourlyMeasurement,
)
from .const import DEFAULT_UPDATE_INTERVAL
from .statistics import FluviusSharingStatistics, FluviusStatistics
from .store import FluviusEnergyStore

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FluviusCoordinatorData:
    """Container returned by the coordinator."""

    latest_summary: FluviusDailySummary | None
    lifetime_totals: dict[str, float]
    peak_measurements: list[FluviusPeakMeasurement]
    quarter_hourly_measurements: list[FluviusQuarterHourlyMeasurement]
    energy_sharing: list[FluviusEnergySharingSummary] = field(default_factory=list)


class FluviusEnergyDataUpdateCoordinator(DataUpdateCoordinator[FluviusCoordinatorData]):
    """Periodically fetch and store Fluvius energy data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: FluviusApiClient,
        store: FluviusEnergyStore,
        statistics: FluviusStatistics | None = None,
        sharing_statistics: FluviusSharingStatistics | None = None,
    ) -> None:
        super().__init__(
            hass, LOGGER, name="Fluvius energy coordinator", update_interval=DEFAULT_UPDATE_INTERVAL
        )
        self._client = client
        self._store = store
        self._statistics = statistics
        self._sharing_statistics = sharing_statistics

    async def _async_update_data(self) -> FluviusCoordinatorData:
        summaries, peaks, intervals = [], [], []
        errors = []
        try:
            summaries, peaks = await self._client.fetch_daily_summaries_with_spikes()
        except FluviusAuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except FluviusApiError as err:
            errors.append(err)

        if self._client.detailed_history:
            try:
                intervals = await self._client.fetch_quarter_hourly_consumption()
            except FluviusAuthenticationError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except FluviusApiError as err:
                errors.append(err)

        if errors and not summaries and not intervals:
            raise UpdateFailed(str(errors[0])) from errors[0]
        for err in errors:
            LOGGER.debug("Some Fluvius data is temporarily unavailable: %s", err)

        if self._statistics:
            await self._statistics.async_update(summaries, intervals)
        for summary in summaries:
            await self._store.async_process_summary(summary.day_id, summary.metrics)

        previous = self.data
        energy_sharing = previous.energy_sharing if previous else []
        try:
            energy_sharing = await self._client.fetch_energy_sharing()
            if self._sharing_statistics is not None and energy_sharing:
                pending = {
                    summary.exchange_id
                    for summary in energy_sharing
                    if self._sharing_statistics.needs_backfill(summary.exchange_id)
                }
                if pending:
                    # One-off multi-year backfill per exchange; on failure the
                    # flag is not set, so the next refresh retries the walk.
                    energy_sharing = await self._client.fetch_energy_sharing(history_for=pending)
                await self._sharing_statistics.async_update(energy_sharing, backfilled_ids=pending)
        except FluviusAuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except FluviusApiError as err:
            LOGGER.debug("Energy sharing data is temporarily unavailable: %s", err)

        latest = summaries[-1] if summaries else (previous.latest_summary if previous else None)
        if latest is None and intervals:
            # Detailed-only meters still get a meaningful latest-period reading.
            item = intervals[-1]
            latest = FluviusDailySummary(item.start.isoformat(), item.start, item.end, item.metrics)
        if not summaries and not intervals:
            LOGGER.debug("No new published Fluvius readings; keeping the last available data")
        return FluviusCoordinatorData(
            latest_summary=latest,
            lifetime_totals=self._statistics.get_totals()
            if self._statistics
            else self._store.get_lifetime_totals(),
            peak_measurements=peaks or (previous.peak_measurements if previous else []),
            quarter_hourly_measurements=intervals
            or (previous.quarter_hourly_measurements if previous else []),
            energy_sharing=energy_sharing,
        )
