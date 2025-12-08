"""HTTP client helpers for the Fluvius Energy integration."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from .const import (
    ALL_METRICS,
    CONF_DAYS_BACK,
    CONF_GRANULARITY,
    CONF_GAS_UNIT,
    CONF_TIMEZONE,
    CONF_VERBOSE_LOGGING,
    DEFAULT_DAYS_BACK,
    DEFAULT_GRANULARITY,
    DEFAULT_GAS_UNIT,
    DEFAULT_HOURLY_DAYS_BACK,
    DEFAULT_METER_TYPE,
    DEFAULT_TIMEZONE,
    DEFAULT_VERBOSE_LOGGING,
    GAS_MIN_LOOKBACK_DAYS,
    GAS_SUPPORTED_GRANULARITY,
    GAS_UNIT_CUBIC_METERS,
    HOURLY_GRANULARITY,
    METER_TYPE_GAS,
)
from .auth import FluviusAuthError, async_get_bearer_token

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Windows without tzdata
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore


LOGGER = logging.getLogger(__name__)

CUBIC_METER_UNIT_CODE = 5
KILO_WATT_HOUR_UNIT_CODE = 3


class FluviusApiError(RuntimeError):
    """Raised when the Fluvius API call fails."""


@dataclass(slots=True)
class FluviusDailySummary:
    """Container for a single day of energy data."""

    day_id: str
    start: datetime
    end: datetime
    metrics: Dict[str, float]


@dataclass(slots=True)
class FluviusPeakMeasurement:
    """Container describing the monthly peak power measurement."""

    period_start: datetime
    period_end: datetime
    spike_start: datetime
    spike_end: datetime
    value_kw: float


@dataclass(slots=True)
class FluviusQuarterHourlyMeasurement:
    """Container for a single 15-minute interval of energy data."""

    start: datetime
    end: datetime
    consumption: float  # kWh consumed in this interval
    injection: float  # kWh injected in this interval


class FluviusApiClient:
    """Thin wrapper around the HTTP helpers used by the CLI script."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        ean: str,
        meter_serial: str,
        meter_type: str = DEFAULT_METER_TYPE,
        remember_me: bool = False,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._ean = ean
        self._meter_serial = meter_serial
        self._meter_type = meter_type
        self._remember_me = remember_me
        self._options = options or {}
        self._verbose = bool(self._options.get(CONF_VERBOSE_LOGGING, DEFAULT_VERBOSE_LOGGING))

    def _log_verbose(self, message: str, *args: Any) -> None:
        """Log a message only if verbose logging is enabled."""
        if self._verbose:
            LOGGER.debug("[VERBOSE] " + message, *args)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def fetch_daily_summaries(self) -> List[FluviusDailySummary]:
        """Retrieve the most recent consumption data and return parsed summaries."""

        summaries, _ = await self._fetch_summaries_and_spikes(include_spikes=False)
        return summaries

    async def fetch_daily_summaries_with_spikes(self) -> tuple[
        List[FluviusDailySummary],
        List[FluviusPeakMeasurement],
    ]:
        """Return both the daily summaries and the monthly peak power values."""

        return await self._fetch_summaries_and_spikes(include_spikes=True)

    async def fetch_quarter_hourly_consumption(
        self,
        days_back: int = DEFAULT_HOURLY_DAYS_BACK,
    ) -> List[FluviusQuarterHourlyMeasurement]:
        """Retrieve 15-minute interval consumption data for the specified period.
        
        Args:
            days_back: Number of days to look back (default: 1 for today only).
                       Note: Fluvius counts time starting at 11PM the previous day.
        
        Returns:
            List of quarter-hourly measurements sorted by start time.
        """
        access_token = await self._async_get_access_token()
        payload = await self._fetch_raw_quarter_hourly(access_token, days_back)
        return self._quarter_hourly_from_payload(payload)

    async def _fetch_summaries_and_spikes(
        self,
        *,
        include_spikes: bool,
    ) -> tuple[List[FluviusDailySummary], List[FluviusPeakMeasurement]]:
        access_token = await self._async_get_access_token()
        payload = await self._fetch_raw_consumption(access_token)
        LOGGER.debug("Raw consumption payload has %d items", len(payload))
        if payload:
            LOGGER.debug("First payload item keys: %s", list(payload[0].keys()) if payload[0] else "empty")
        summaries = self._summaries_from_payload(payload)
        LOGGER.debug("Parsed %d summaries from payload", len(summaries))
        # Don't fail if no summaries - data may not be available yet for new setups
        # The coordinator will handle empty data gracefully

        peaks: List[FluviusPeakMeasurement] = []
        if include_spikes and self._meter_type != METER_TYPE_GAS:
            spike_payload = await self._fetch_raw_spikes(access_token)
            peaks = self._spikes_from_payload(spike_payload)
        return summaries, peaks

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    async def _async_get_access_token(self) -> str:
        self._log_verbose("Starting authentication for user: %s", self._email[:3] + "***")
        try:
            access_token, _ = await async_get_bearer_token(
                self._session,
                self._email,
                self._password,
                remember_me=self._remember_me,
                verbose=self._verbose,
            )
        except FluviusAuthError as err:
            error_msg = str(err)
            LOGGER.error(
                "FLUVIUS AUTH ERROR: Failed to authenticate with Fluvius. "
                "This usually means: 1) Invalid email/password, 2) Fluvius service is down, "
                "or 3) Your account needs re-verification at mijn.fluvius.be. "
                "Details: %s",
                error_msg,
            )
            raise FluviusApiError(
                f"Authentication failed - check your credentials or visit mijn.fluvius.be to verify your account. Error: {err}"
            ) from err
        except aiohttp.ClientError as err:
            LOGGER.error(
                "FLUVIUS NETWORK ERROR: Could not reach Fluvius authentication servers. "
                "Check your internet connection. Details: %s",
                err,
            )
            raise FluviusApiError(
                f"Network error while authenticating - check internet connection. Error: {err}"
            ) from err

        if not access_token:
            LOGGER.error(
                "FLUVIUS AUTH ERROR: Authentication completed but no access token was returned. "
                "This is unexpected - try re-authenticating or check Fluvius service status."
            )
            raise FluviusApiError(
                "Authentication succeeded but no access token was returned - try removing and re-adding the integration"
            )
        
        self._log_verbose("Authentication successful, received access token")
        return access_token

    async def _fetch_raw_consumption(self, access_token: str) -> List[Dict[str, Any]]:
        history_params = self._build_history_range()
        granularity = str(self._options.get(CONF_GRANULARITY, DEFAULT_GRANULARITY))
        if self._meter_type == METER_TYPE_GAS:
            granularity = GAS_SUPPORTED_GRANULARITY
        params = {
            **history_params,
            "granularity": granularity,
            "asServiceProvider": "false",
            "meterSerialNumber": self._meter_serial,
        }
        
        self._log_verbose(
            "API Request - URL: meter-measurement-history/%s, Params: granularity=%s, from=%s, until=%s, meter=%s",
            self._ean,
            granularity,
            history_params.get("historyFrom", ""),
            history_params.get("historyUntil", ""),
            self._meter_serial,
        )
        
        LOGGER.debug(
            "Fetching consumption: granularity=%s, from=%s, until=%s",
            granularity,
            history_params.get("historyFrom", "")[:10],
            history_params.get("historyUntil", "")[:10],
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (HomeAssistant-FluviusEnergy)",
        }
        url = f"https://mijn.fluvius.be/verbruik/api/meter-measurement-history/{self._ean}"

        try:
            async with self._session.get(url, params=params, headers=headers, timeout=30) as response:
                self._log_verbose(
                    "API Response - Status: %s, Content-Type: %s",
                    response.status,
                    getattr(response, 'content_type', 'unknown'),
                )
                if response.status != 200:
                    response_text = await response.text()
                    self._log_verbose("API Error Response Body: %s", response_text[:500])
                    LOGGER.error(
                        "FLUVIUS API ERROR: Consumption API returned HTTP %s. "
                        "EAN: %s, Meter: %s. Response: %s",
                        response.status,
                        self._ean,
                        self._meter_serial,
                        response_text[:200],
                    )
                response.raise_for_status()
                data: Any = await response.json()
        except aiohttp.ClientResponseError as err:
            LOGGER.error(
                "FLUVIUS API ERROR: Failed to fetch consumption data. HTTP Status: %s, Reason: %s. "
                "This could mean: 1) EAN '%s' is invalid, 2) Meter serial '%s' doesn't match, "
                "or 3) Fluvius API is experiencing issues.",
                err.status,
                err.message,
                self._ean,
                self._meter_serial,
            )
            raise FluviusApiError(
                f"Consumption API call failed (HTTP {err.status}): {err.message}. Check EAN and meter serial."
            ) from err
        except aiohttp.ClientError as err:
            LOGGER.error(
                "FLUVIUS NETWORK ERROR: Could not fetch consumption data. "
                "Check internet connection. Error: %s",
                err,
            )
            raise FluviusApiError(f"Consumption API call failed - network error: {err}") from err
        except ValueError as err:  # pragma: no cover - defensive
            LOGGER.error("FLUVIUS API ERROR: Received invalid JSON from Fluvius: %s", err)
            raise FluviusApiError(f"Failed to decode Fluvius JSON response: {err}") from err

        if not isinstance(data, list):
            LOGGER.error(
                "FLUVIUS API ERROR: Unexpected response format. Expected list, got %s. "
                "Response preview: %s",
                type(data).__name__,
                str(data)[:200],
            )
            raise FluviusApiError(
                f"Fluvius API returned unexpected response type: {type(data).__name__} (expected list)"
            )
        
        self._log_verbose("API Response - Received %d items in payload", len(data))
        if data and self._verbose:
            self._log_verbose("First item keys: %s", list(data[0].keys()) if data[0] else "empty")
        
        return data

    def _build_history_range(self) -> Dict[str, str]:
        tzinfo = self._resolve_timezone(self._options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE))
        days_back = max(int(self._options.get(CONF_DAYS_BACK, DEFAULT_DAYS_BACK)), 1)
        granularity = str(self._options.get(CONF_GRANULARITY, DEFAULT_GRANULARITY))
        
        if self._meter_type == METER_TYPE_GAS:
            days_back = max(days_back, GAS_MIN_LOOKBACK_DAYS)
        
        local_now = datetime.now(tzinfo)
        
        # For granularity=1 (15-min intervals), API only works for single day requests
        if granularity == HOURLY_GRANULARITY:
            # Request a single day (days_back days ago, but at least 1 day back for available data)
            target_date = (local_now - timedelta(days=max(days_back, 1))).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            start_date = target_date
            end_date = target_date.replace(hour=23, minute=59, second=59, microsecond=999000)
        else:
            # Granularity=3 (quarter-hour) and granularity=4 (daily) - multi-day requests ending today
            start_date = (local_now - timedelta(days=days_back)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            end_date = local_now.replace(hour=23, minute=59, second=59, microsecond=999000)
        
        return {
            "historyFrom": start_date.isoformat(timespec="milliseconds"),
            "historyUntil": end_date.isoformat(timespec="milliseconds"),
        }

    async def _fetch_raw_quarter_hourly(
        self,
        access_token: str,
        days_back: int,
    ) -> List[Dict[str, Any]]:
        """Fetch raw quarter-hourly (15-minute) consumption data from the API."""
        history_params = self._build_quarter_hourly_range(days_back)
        params = {
            **history_params,
            "granularity": HOURLY_GRANULARITY,
            "asServiceProvider": "false",
            "meterSerialNumber": self._meter_serial,
        }
        
        self._log_verbose(
            "Quarter-hourly API Request - from=%s, until=%s, days_back=%d",
            history_params.get("historyFrom", ""),
            history_params.get("historyUntil", ""),
            days_back,
        )
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (HomeAssistant-FluviusEnergy)",
        }
        url = f"https://mijn.fluvius.be/verbruik/api/meter-measurement-history/{self._ean}"

        try:
            async with self._session.get(url, params=params, headers=headers, timeout=30) as response:
                self._log_verbose("Quarter-hourly API Response - Status: %s", response.status)
                if response.status != 200:
                    response_text = await response.text()
                    LOGGER.warning(
                        "FLUVIUS API WARNING: Quarter-hourly API returned HTTP %s. "
                        "This data may not be available for your meter. Response: %s",
                        response.status,
                        response_text[:200],
                    )
                response.raise_for_status()
                data: Any = await response.json()
        except aiohttp.ClientResponseError as err:
            LOGGER.warning(
                "FLUVIUS API WARNING: Could not fetch quarter-hourly data (HTTP %s). "
                "15-minute interval data may not be available for meter %s.",
                err.status,
                self._meter_serial,
            )
            raise FluviusApiError(
                f"Quarter-hourly consumption API failed (HTTP {err.status}) - this data may not be available for your meter"
            ) from err
        except aiohttp.ClientError as err:
            LOGGER.warning("FLUVIUS NETWORK WARNING: Could not fetch quarter-hourly data: %s", err)
            raise FluviusApiError(f"Quarter-hourly consumption API call failed: {err}") from err
        except ValueError as err:  # pragma: no cover - defensive
            raise FluviusApiError(f"Failed to decode Fluvius JSON: {err}") from err

        if not isinstance(data, list):
            raise FluviusApiError("Fluvius API returned an unexpected payload (expected list)")
        
        self._log_verbose("Quarter-hourly API Response - Received %d intervals", len(data))
        return data

    def _build_quarter_hourly_range(self, days_back: int) -> Dict[str, str]:
        """Build the date range for quarter-hourly data requests.
        
        Note: Quarter-hourly data (granularity=1) requires SINGLE DAY requests.
        Data is only available for PREVIOUS days, not the current day.
        We request data for a single day: (days_back) days ago.
        """
        tzinfo = self._resolve_timezone(self._options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE))
        local_now = datetime.now(tzinfo)
        # Request a single day: days_back days ago (must be at least yesterday)
        target_date = (local_now - timedelta(days=max(days_back, 1))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # For single day requests, start and end are on the same day
        start_date = target_date
        end_date = target_date.replace(hour=23, minute=59, second=59, microsecond=999000)
        return {
            "historyFrom": start_date.isoformat(timespec="milliseconds"),
            "historyUntil": end_date.isoformat(timespec="milliseconds"),
        }

    async def _fetch_raw_spikes(self, access_token: str) -> List[Dict[str, Any]]:
        spike_params = self._build_spike_history_range()
        params = {
            **spike_params,
            "asServiceProvider": "false",
            "meterSerialNumber": self._meter_serial,
        }
        
        self._log_verbose(
            "Peak power API Request - from=%s, until=%s",
            spike_params.get("historyFrom", "")[:10],
            spike_params.get("historyUntil", "")[:10],
        )
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (HomeAssistant-FluviusEnergy)",
        }
        url = f"https://mijn.fluvius.be/verbruik/api/meter-measurement-spikes/{self._ean}"

        try:
            async with self._session.get(url, params=params, headers=headers, timeout=30) as response:
                self._log_verbose("Peak power API Response - Status: %s", response.status)
                if response.status != 200:
                    response_text = await response.text()
                    LOGGER.warning(
                        "FLUVIUS API WARNING: Peak power API returned HTTP %s. Response: %s",
                        response.status,
                        response_text[:200],
                    )
                response.raise_for_status()
                data: Any = await response.json()
        except aiohttp.ClientResponseError as err:
            LOGGER.warning(
                "FLUVIUS API WARNING: Could not fetch peak power data (HTTP %s). "
                "Peak power data may not be available for meter %s.",
                err.status,
                self._meter_serial,
            )
            raise FluviusApiError(f"Peak power API call failed (HTTP {err.status})") from err
        except aiohttp.ClientError as err:
            LOGGER.warning("FLUVIUS NETWORK WARNING: Could not fetch peak power data: %s", err)
            raise FluviusApiError(f"Peak power API call failed: {err}") from err
        except ValueError as err:  # pragma: no cover - defensive
            raise FluviusApiError(f"Failed to decode Fluvius JSON: {err}") from err

        if not isinstance(data, list):
            raise FluviusApiError("Fluvius spike API returned an unexpected payload (expected list)")
        
        self._log_verbose("Peak power API Response - Received %d items", len(data))
        return data

    def _build_spike_history_range(self) -> Dict[str, str]:
        tzinfo = self._resolve_timezone(self._options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE))
        local_now = datetime.now(tzinfo)
        start_date = local_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
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
    def _target_unit_code(self) -> int | None:
        """Return the unit code that should be processed for this entry."""

        if self._meter_type != METER_TYPE_GAS:
            return None
        gas_unit = str(self._options.get(CONF_GAS_UNIT, DEFAULT_GAS_UNIT))
        if gas_unit == GAS_UNIT_CUBIC_METERS:
            return CUBIC_METER_UNIT_CODE
        return KILO_WATT_HOUR_UNIT_CODE

    def _summaries_from_payload(self, payload: List[Dict[str, Any]]) -> List[FluviusDailySummary]:
        summaries: List[FluviusDailySummary] = []
        for i, day_data in enumerate(payload):
            summary = self._summarize_day(day_data)
            if summary:
                summaries.append(summary)
            else:
                LOGGER.debug("Could not parse day_data at index %d: d=%s", i, day_data.get("d"))
        summaries.sort(key=lambda item: item.start)
        return summaries

    def _spikes_from_payload(self, payload: List[Dict[str, Any]]) -> List[FluviusPeakMeasurement]:
        peaks: List[FluviusPeakMeasurement] = []
        for chunk in payload:
            period_start = self._parse_datetime(chunk.get("d"))
            period_end = self._parse_datetime(chunk.get("de")) or period_start
            if not period_start or not period_end:
                continue
            for reading in chunk.get("v", []) or []:
                value = self._safe_float(reading.get("v"))
                spike_start = self._parse_datetime(reading.get("sst"))
                spike_end = self._parse_datetime(reading.get("set"))
                if spike_start is None or spike_end is None:
                    continue
                peaks.append(
                    FluviusPeakMeasurement(
                        period_start=period_start,
                        period_end=period_end,
                        spike_start=spike_start,
                        spike_end=spike_end,
                        value_kw=value,
                    )
                )
        peaks.sort(key=lambda item: item.period_start)
        return peaks

    def _quarter_hourly_from_payload(
        self,
        payload: List[Dict[str, Any]],
    ) -> List[FluviusQuarterHourlyMeasurement]:
        """Parse the raw API response into quarter-hourly measurements.
        
        Each item in the payload represents a 15-minute interval with:
        - d: start datetime (ISO format)
        - de: end datetime (ISO format)
        - v: array of values with t=1 for consumption, t=2 for injection
        """
        measurements: List[FluviusQuarterHourlyMeasurement] = []
        target_unit = self._target_unit_code()

        for interval in payload:
            start = self._parse_datetime(interval.get("d"))
            end = self._parse_datetime(interval.get("de"))
            if not start or not end:
                continue

            consumption = 0.0
            injection = 0.0

            for reading in interval.get("v", []) or []:
                value_type = self._safe_int(reading.get("t"))  # 1=consumption, 2=injection
                unit = self._safe_int(reading.get("u"))
                value = self._safe_float(reading.get("v"))

                # Skip readings that don't match the target unit (for gas meters)
                if target_unit is not None and unit != target_unit:
                    continue
                # Skip volume readings for electricity meters
                if target_unit is None and unit == CUBIC_METER_UNIT_CODE:
                    continue

                if value_type == 1:
                    consumption += value
                elif value_type == 2:
                    injection += value

            measurements.append(
                FluviusQuarterHourlyMeasurement(
                    start=start,
                    end=end,
                    consumption=consumption,
                    injection=injection,
                )
            )

        measurements.sort(key=lambda item: item.start)
        return measurements

    def _summarize_day(self, day_data: Dict[str, Any]) -> Optional[FluviusDailySummary]:
        start = self._parse_datetime(day_data.get("d"))
        if not start:
            return None
        end = self._parse_datetime(day_data.get("de")) or (start + timedelta(days=1))
        metrics: Dict[str, float] = {metric: 0.0 for metric in ALL_METRICS}
        target_unit = self._target_unit_code()

        for reading in day_data.get("v", []) or []:
            direction = self._safe_int(reading.get("dc"))
            tariff = self._safe_int(reading.get("t"), default=1)
            unit = self._safe_int(reading.get("u"))
            value = self._safe_float(reading.get("v"))

            if target_unit is not None and unit != target_unit:
                # Skip duplicate gas readings in the non-selected unit.
                continue
            if target_unit is None and unit == CUBIC_METER_UNIT_CODE:
                # Gas meters return both m3 and kWh. Skip the volume reading when
                # keeping the default energy-based sensors.
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
        if direction == 0:
            return "consumption_high" if is_high_tariff else "consumption_low"
        if direction == 1:
            return "consumption_high" if is_high_tariff else "injection_high"
        if direction == 2:
            return "consumption_low" if is_high_tariff else "injection_low"
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
