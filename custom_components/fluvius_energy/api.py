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
    DEFAULT_METER_TYPE,
    DEFAULT_TIMEZONE,
    GAS_MIN_LOOKBACK_DAYS,
    METER_TYPE_GAS,
)
from .auth import FluviusAuthError, get_bearer_token_http

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Windows without tzdata
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore


CUBIC_METER_UNIT_CODE = 5


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
        meter_type: str = DEFAULT_METER_TYPE,
        remember_me: bool = False,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._email = email
        self._password = password
        self._ean = ean
        self._meter_serial = meter_serial
        self._meter_type = meter_type
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
        if self._meter_type == METER_TYPE_GAS:
            days_back = max(days_back, GAS_MIN_LOOKBACK_DAYS)
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
            direction = self._safe_int(reading.get("dc"))
            tariff = self._safe_int(reading.get("t"), default=1)
            unit = self._safe_int(reading.get("u"))
            value = self._safe_float(reading.get("v"))

            if unit == CUBIC_METER_UNIT_CODE:
                # Gas meters return both m³ and kWh. Ignore the volume duplicate so
                # Home Assistant sensors keep reporting energy in kWh.
                continue

            metric_key = self._metric_from_reading(direction, tariff)
            if not metric_key:
                continue
            metrics[metric_key] += value

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

    @staticmethod
    def _metric_from_reading(direction: int, tariff: int) -> Optional[str]:
        """Return the metric bucket that should be incremented for a reading."""

        is_high_tariff = tariff == 1
        if direction in (0, 1):
            return "consumption_high" if is_high_tariff else "consumption_low"
        if direction == 2:
            return "injection_high" if is_high_tariff else "injection_low"
        return None

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return default
