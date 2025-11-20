"""HTTP client helpers for the Fluvius Energy integration."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from .const import (
    ALL_METRICS,
    CONF_DAYS_BACK,
    CONF_GRANULARITY,
    CONF_TIMEZONE,
    DEFAULT_DAYS_BACK,
    DEFAULT_GRANULARITY,
    DEFAULT_TIMEZONE,
)
from .auth import FluviusAuthError, get_bearer_token_http

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Windows without tzdata
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore


class FluviusApiError(RuntimeError):
    """Raised when the Fluvius API call fails."""


@dataclass(slots=True)
class FluviusDailySummary:
    """Container for a single day of energy data."""

    day_id: str
    start: datetime
    end: datetime
    metrics: Dict[str, float]


class FluviusApiClient:
    """Thin wrapper around the HTTP helpers used by the CLI script."""

    def __init__(
        self,
        *,
        email: str,
        password: str,
        ean: str,
        meter_serial: str,
        remember_me: bool = False,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._email = email
        self._password = password
        self._ean = ean
        self._meter_serial = meter_serial
        self._remember_me = remember_me
        self._session = requests.Session()
        self._options = options or {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def fetch_daily_summaries(self) -> List[FluviusDailySummary]:
        """Retrieve the most recent consumption data and return parsed summaries."""

        payload = self._fetch_raw_consumption()
        summaries = self._summaries_from_payload(payload)
        if not summaries:
            raise FluviusApiError("No consumption rows returned by the Fluvius API")
        return summaries

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _fetch_raw_consumption(self) -> List[Dict[str, Any]]:
        try:
            access_token, _ = get_bearer_token_http(
                self._email,
                self._password,
                remember_me=self._remember_me,
                verbose=False,
            )
        except FluviusAuthError as err:
            raise FluviusApiError(f"Authentication failed: {err}") from err
        except requests.RequestException as err:
            raise FluviusApiError(f"Network error while authenticating: {err}") from err

        if not access_token:
            raise FluviusApiError("Authentication response did not include an access token")

        history_params = self._build_history_range()
        params = {
            **history_params,
            "granularity": str(self._options.get(CONF_GRANULARITY, DEFAULT_GRANULARITY)),
            "asServiceProvider": "false",
            "meterSerialNumber": self._meter_serial,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (HomeAssistant-FluviusEnergy)",
        }
        url = f"https://mijn.fluvius.be/verbruik/api/meter-measurement-history/{self._ean}"

        try:
            response = self._session.get(url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
        except requests.RequestException as err:
            raise FluviusApiError(f"Consumption API call failed: {err}") from err

        try:
            data: Any = response.json()
        except ValueError as err:  # pragma: no cover - defensive
            raise FluviusApiError(f"Failed to decode Fluvius JSON: {err}") from err

        if not isinstance(data, list):
            raise FluviusApiError("Fluvius API returned an unexpected payload (expected list)")
        return data

    def _build_history_range(self) -> Dict[str, str]:
        tzinfo = self._resolve_timezone(self._options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE))
        days_back = max(int(self._options.get(CONF_DAYS_BACK, DEFAULT_DAYS_BACK)), 1)
        local_now = datetime.now(tzinfo)
        start_date = (local_now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = local_now.replace(hour=23, minute=59, second=59, microsecond=999000)
        return {
            "historyFrom": start_date.isoformat(timespec="milliseconds"),
            "historyUntil": end_date.isoformat(timespec="milliseconds"),
        }

    def _resolve_timezone(self, tz_name: Optional[str]):
        if tz_name and ZoneInfo is not None:
            try:
                return ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:  # pragma: no cover - fallback path
                pass
        if tz_name:
            # The provided timezone string exists but zoneinfo is unavailable.
            pass
        local = datetime.now().astimezone().tzinfo
        if local:
            return local
        return timezone.utc

    # ------------------------------------------------------------------
    # Payload parsing helpers
    # ------------------------------------------------------------------
    def _summaries_from_payload(self, payload: List[Dict[str, Any]]) -> List[FluviusDailySummary]:
        summaries: List[FluviusDailySummary] = []
        for day_data in payload:
            summary = self._summarize_day(day_data)
            if summary:
                summaries.append(summary)
        summaries.sort(key=lambda item: item.start)
        return summaries

    def _summarize_day(self, day_data: Dict[str, Any]) -> Optional[FluviusDailySummary]:
        start = self._parse_datetime(day_data.get("d"))
        if not start:
            return None
        end = self._parse_datetime(day_data.get("de")) or (start + timedelta(days=1))
        metrics: Dict[str, float] = {metric: 0.0 for metric in ALL_METRICS}

        for reading in day_data.get("v", []) or []:
            try:
                direction = int(reading.get("dc", 0))
            except (TypeError, ValueError):
                direction = 0
            try:
                tariff = int(reading.get("t", 0))
            except (TypeError, ValueError):
                tariff = 0
            try:
                value = float(reading.get("v", 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0

            if direction == 1:  # Consumption from the grid
                if tariff == 1:
                    metrics["consumption_high"] += value
                else:
                    metrics["consumption_low"] += value
            elif direction == 2:  # Injection into the grid
                if tariff == 1:
                    metrics["injection_high"] += value
                else:
                    metrics["injection_low"] += value

        metrics["consumption_total"] = metrics["consumption_high"] + metrics["consumption_low"]
        metrics["injection_total"] = metrics["injection_high"] + metrics["injection_low"]
        metrics["net_consumption"] = metrics["consumption_total"] - metrics["injection_total"]

        day_id = start.isoformat()
        return FluviusDailySummary(day_id=day_id, start=start, end=end, metrics=metrics)

    @staticmethod
    def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        fixed = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(fixed)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
