"""DataUpdateCoordinator for the Fluvius Energy integration."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Dict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import FluviusApiClient, FluviusApiError, FluviusDailySummary
from .const import DEFAULT_UPDATE_INTERVAL
from .store import FluviusEnergyStore

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FluviusCoordinatorData:
    """Container returned by the coordinator."""

    latest_summary: FluviusDailySummary | None
    lifetime_totals: Dict[str, float]


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
            summaries = await self._client.fetch_daily_summaries()
        except FluviusApiError as err:
            raise UpdateFailed(str(err)) from err

        for summary in summaries:
            await self._store.async_process_summary(summary.day_id, summary.metrics)

        totals = self._store.get_lifetime_totals()
        latest_summary = summaries[-1] if summaries else None
        return FluviusCoordinatorData(latest_summary=latest_summary, lifetime_totals=totals)
