"""Runtime dataclasses shared across the Fluvius Energy integration."""
from __future__ import annotations

from dataclasses import dataclass

from .api import FluviusApiClient
from .coordinator import FluviusEnergyDataUpdateCoordinator
from .store import FluviusEnergyStore


@dataclass(slots=True)
class FluviusRuntimeData:
    """Container stored on ConfigEntry.runtime_data."""

    client: FluviusApiClient
    coordinator: FluviusEnergyDataUpdateCoordinator
    store: FluviusEnergyStore
