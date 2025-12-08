"""Lightweight test helpers mirroring Home Assistant's built-in test utilities."""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Iterable
from uuid import uuid4

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
)


class MockConfigEntry:
    """Minimal stand-in for Home Assistant's MockConfigEntry from core tests."""

    def __init__(
        self,
        *,
        domain: str,
        data: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        title: str | None = None,
        source: str = "user",
        entry_id: str | None = None,
        version: int = 1,
        minor_version: int = 1,
        unique_id: str | None = None,
        pref_disable_new_entities: bool | None = None,
        pref_disable_polling: bool | None = None,
        subentries_data: Iterable[Any] | None = None,
    ) -> None:
        self._entry = ConfigEntry(
            domain=domain,
            data=data or {},
            options=options or {},
            title=title or domain,
            source=source,
            version=version,
            minor_version=minor_version,
            entry_id=entry_id or uuid4().hex,
            unique_id=unique_id,
            discovery_keys=MappingProxyType({}),
            pref_disable_new_entities=pref_disable_new_entities,
            pref_disable_polling=pref_disable_polling,
            state=ConfigEntryState.NOT_LOADED,
            subentries_data=subentries_data,
            created_at=None,
            modified_at=None,
            disabled_by=None,
        )

    def add_to_hass(self, hass) -> None:
        """Register the entry with Home Assistant."""

        hass.config_entries._entries[self._entry.entry_id] = self._entry  # noqa: SLF001

    async def async_add_to_hass(self, hass) -> None:
        """Async helper to register the entry with Home Assistant."""

        self.add_to_hass(hass)

    def remove_from_hass(self, hass) -> None:
        """Remove the entry from Home Assistant."""

        if self._entry.entry_id in hass.config_entries._entries:  # noqa: SLF001
            del hass.config_entries._entries[self._entry.entry_id]  # noqa: SLF001

    # Delegate attribute access to the underlying ConfigEntry
    def __getattr__(self, item: str):
        return getattr(self._entry, item)

    def __setattr__(self, key, value):
        if key == "_entry":
            super().__setattr__(key, value)
        else:
            setattr(self._entry, key, value)
