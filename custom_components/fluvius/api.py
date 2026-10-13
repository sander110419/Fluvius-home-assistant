"""HTTP client helpers for the Fluvius Energy integration."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .auth import (
    FluviusAuthError,
    FluviusInvalidCredentialsError,
    async_get_bearer_token,
    async_refresh_bearer_token,
)
from .const import (
    ALL_METRICS,
    CONF_DAYS_BACK,
    CONF_GAS_UNIT,
    CONF_GRANULARITY,
    CONF_HISTORY_UNTIL,
    CONF_TIMEZONE,
    CONF_VERBOSE_LOGGING,
    DEFAULT_DAYS_BACK,
    DEFAULT_GAS_UNIT,
    DEFAULT_GRANULARITY,
    DEFAULT_METER_TYPE,
    DEFAULT_TIMEZONE,
    DEFAULT_VERBOSE_LOGGING,
    GAS_MIN_LOOKBACK_DAYS,
    GAS_UNIT_CUBIC_METERS,
    HOURLY_GRANULARITY,
    METER_TYPE_GAS,
    QUARTER_HOURLY_GRANULARITY,
)

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


class FluviusAuthenticationError(FluviusApiError):
    """Credentials must be updated through Home Assistant reauthentication."""


def _transport_reason(err: aiohttp.ClientError | TimeoutError) -> str:
    # str(ClientResponseError) embeds the request URL, which carries the email as login_hint.
    if isinstance(err, aiohttp.ClientResponseError):
        return f"HTTP {err.status} {err.message}"
    return f"{type(err).__name__}: {err}" if str(err) else type(err).__name__


@dataclass(slots=True)
class FluviusDailySummary:
    """Container for a single day of energy data."""

    day_id: str
    start: datetime
    end: datetime
    metrics: dict[str, float]


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
    metrics: dict[str, float] = field(default_factory=dict)


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
        options: dict[str, Any] | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._ean = ean
        self._meter_serial = meter_serial
        self._meter_type = meter_type
        self._remember_me = remember_me
        self._options = options or {}
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires = 0.0
        self._verbose = bool(self._options.get(CONF_VERBOSE_LOGGING, DEFAULT_VERBOSE_LOGGING))

    def _log_verbose(self, message: str, *args: Any) -> None:
        """Log a message only if verbose logging is enabled."""
        if self._verbose:
            LOGGER.debug("[VERBOSE] " + message, *args)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def fetch_daily_summaries(self) -> list[FluviusDailySummary]:
        """Retrieve the most recent consumption data and return parsed summaries."""

        summaries, _ = await self._fetch_summaries_and_spikes(include_spikes=False)
        return summaries

    async def fetch_daily_summaries_with_spikes(
        self,
    ) -> tuple[
        list[FluviusDailySummary],
        list[FluviusPeakMeasurement],
    ]:
        """Return both the daily summaries and the monthly peak power values."""

        return await self._fetch_summaries_and_spikes(include_spikes=True)

    async def fetch_quarter_hourly_consumption(
        self,
        days_back: int | None = None,
    ) -> list[FluviusQuarterHourlyMeasurement]:
        """Fetch every day in the lookback, including delayed publications.

        Electricity provides quarters (1); gas provides hours (2). The legacy
        method name is retained for callers. Each request covers one local day.
        """
        count = self.days_back if days_back is None else max(1, int(days_back))
        token = await self._async_get_access_token()
        measurements = {}
        for offset in range(count, 0, -1):
            payload = await self._fetch_raw_quarter_hourly(token, offset)
            for measurement in self._quarter_hourly_from_payload(payload):
                if measurement.end <= self.history_end:
                    measurements[measurement.start] = measurement
        return sorted(measurements.values(), key=lambda item: item.start)

    async def _fetch_summaries_and_spikes(
        self,
        *,
        include_spikes: bool,
    ) -> tuple[list[FluviusDailySummary], list[FluviusPeakMeasurement]]:
        access_token = await self._async_get_access_token()
        payload = await self._fetch_raw_consumption(access_token)
        LOGGER.debug("Raw consumption payload has %d items", len(payload))
        if payload:
            LOGGER.debug(
                "First payload item keys: %s", list(payload[0].keys()) if payload[0] else "empty"
            )
        summaries = self._summaries_from_payload(payload)
        LOGGER.debug("Parsed %d summaries from payload", len(summaries))
        # Don't fail if no summaries - data may not be available yet for new setups
        # The coordinator will handle empty data gracefully

        peaks: list[FluviusPeakMeasurement] = []
        if include_spikes and self._meter_type != METER_TYPE_GAS:
            try:
                spike_payload = await self._fetch_raw_spikes(access_token)
                peaks = self._spikes_from_payload(spike_payload)
            except FluviusAuthenticationError:
                raise
            except FluviusApiError:
                LOGGER.debug("Peak power data is temporarily unavailable")
        return summaries, peaks

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    async def _async_get_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires:
            return self._access_token
        access_token, tokens = await self._async_refresh_access_token() or await self._async_login()
        if not access_token:
            raise FluviusApiError("Fluvius returned no access token")
        self._access_token = access_token
        self._token_expires = time.monotonic() + max(0, int(tokens.get("expires_in", 3600)) - 60)
        # B2C may rotate the refresh token on redemption; keep the old one if it does not.
        self._refresh_token = tokens.get("refresh_token") or self._refresh_token
        return access_token

    async def _async_refresh_access_token(self) -> tuple[str, dict[str, Any]] | None:
        """Return None when the caller must log in with the password.

        That is the case without a refresh token, or when Fluvius refuses it (the token
        is then forgotten). A transport failure raises and keeps the token, so a network
        blip does not cost a password login on the next poll.
        """

        if not self._refresh_token:
            return None
        try:
            return await async_refresh_bearer_token(
                self._session, self._refresh_token, verbose=self._verbose
            )
        except FluviusAuthError as err:
            LOGGER.debug("Fluvius refresh token no longer usable, logging in again: %s", err)
            self._refresh_token = None
            return None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FluviusApiError(
                f"Cannot reach Fluvius authentication ({_transport_reason(err)})"
            ) from err

    async def _async_login(self) -> tuple[str, dict[str, Any]]:
        """Only an explicit SelfAsserted rejection is a credential failure.

        The login page is scraped, so a changed or degraded page, a redirect we do not
        recognise or a token endpoint error says nothing about the password. Those stay
        retryable instead of forcing the user through reauth.
        """

        try:
            return await async_get_bearer_token(
                self._session,
                self._email,
                self._password,
                remember_me=self._remember_me,
                verbose=self._verbose,
            )
        except FluviusInvalidCredentialsError as err:
            raise FluviusAuthenticationError(f"Fluvius authentication failed: {err}") from err
        except FluviusAuthError as err:
            raise FluviusApiError(f"Fluvius login flow failed: {err}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FluviusApiError(
                f"Cannot reach Fluvius authentication ({_transport_reason(err)})"
            ) from err

    async def _fetch_raw_consumption(self, access_token: str) -> list[dict[str, Any]]:
        # Daily summaries must stay daily even when detailed history is enabled.
        return await self._request_history(
            access_token,
            {
                **self._build_history_range(),
                "granularity": DEFAULT_GRANULARITY,
            },
        )

    async def _request_history(
        self, access_token: str, params: dict, *, spikes: bool = False
    ) -> list[dict]:
        endpoint = "meter-measurement-spikes" if spikes else "meter-measurement-history"
        access_token = self._access_token or access_token
        params = {**params, "asServiceProvider": "false", "meterSerialNumber": self._meter_serial}
        self._log_verbose(
            "Request %s: granularity=%s, from=%s, until=%s",
            endpoint,
            params.get("granularity"),
            params["historyFrom"],
            params["historyUntil"],
        )
        for attempt in range(2):
            try:
                async with self._session.get(
                    f"https://mijn.fluvius.be/verbruik/api/{endpoint}/{self._ean}",
                    params=params,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                    timeout=30,
                ) as response:
                    if response.status == 401:
                        self._access_token = None
                        if attempt:
                            # The token we just got is refused too; start the next poll
                            # from a full login instead of the same refresh token.
                            self._refresh_token = None
                            raise FluviusApiError("Fluvius rejected a freshly issued access token")
                    else:
                        response.raise_for_status()
                        data = await response.json()
                        if not isinstance(data, list):
                            raise FluviusApiError(
                                "Fluvius returned an unexpected payload (expected list)"
                            )
                        return data
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise FluviusApiError(
                    f"Fluvius {endpoint} request failed ({type(err).__name__})"
                ) from err
            access_token = await self._async_get_access_token()
        raise FluviusApiError("Fluvius rejected a freshly issued access token")

    @property
    def days_back(self) -> int:
        days = max(1, int(self._options.get(CONF_DAYS_BACK, DEFAULT_DAYS_BACK)))
        return max(days, GAS_MIN_LOOKBACK_DAYS) if self._meter_type == METER_TYPE_GAS else days

    @property
    def history_end(self) -> datetime:
        tz = self._resolve_timezone(self._options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE))
        value = self._options.get(CONF_HISTORY_UNTIL)
        if value:
            end = datetime.fromisoformat(value)
            if end.tzinfo is None:
                end = end.replace(tzinfo=tz)
            return min(end.astimezone(tz), datetime.now(tz))
        return datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    @property
    def detailed_history(self) -> bool:
        return str(self._options.get(CONF_GRANULARITY, DEFAULT_GRANULARITY)) != DEFAULT_GRANULARITY

    def _build_history_range(self) -> dict[str, str]:
        end = self.history_end
        start = (end - timedelta(days=self.days_back)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return {
            "historyFrom": start.isoformat(timespec="milliseconds"),
            "historyUntil": (end - timedelta(milliseconds=1)).isoformat(timespec="milliseconds"),
        }

    async def _fetch_raw_quarter_hourly(
        self, access_token: str, days_back: int
    ) -> list[dict[str, Any]]:
        return await self._request_history(
            access_token,
            {
                **self._build_quarter_hourly_range(days_back),
                "granularity": HOURLY_GRANULARITY
                if self._meter_type == METER_TYPE_GAS
                else QUARTER_HOURLY_GRANULARITY,
            },
        )

    def _build_quarter_hourly_range(self, days_back: int) -> dict[str, str]:
        end = self.history_end
        # A cutoff inside a day includes that day's completed intervals.
        anchor = (end - timedelta(microseconds=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start = anchor - timedelta(days=max(days_back, 1) - 1)
        until = min(start + timedelta(days=1), end) - timedelta(milliseconds=1)
        return {
            "historyFrom": start.isoformat(timespec="milliseconds"),
            "historyUntil": until.isoformat(timespec="milliseconds"),
        }

    async def _fetch_raw_spikes(self, access_token: str) -> list[dict[str, Any]]:
        return await self._request_history(
            access_token, self._build_spike_history_range(), spikes=True
        )

    def _build_spike_history_range(self) -> dict[str, str]:
        tzinfo = self._resolve_timezone(self._options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE))
        local_now = datetime.now(tzinfo)
        start_date = local_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end_date = local_now.replace(hour=23, minute=59, second=59, microsecond=999000)
        return {
            "historyFrom": start_date.isoformat(timespec="milliseconds"),
            "historyUntil": end_date.isoformat(timespec="milliseconds"),
        }

    def _resolve_timezone(self, tz_name: str | None):
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
        return UTC

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

    def _summaries_from_payload(self, payload: list[dict[str, Any]]) -> list[FluviusDailySummary]:
        summaries = {}
        for item in payload:
            summary = self._summarize_day(item)
            if summary and summary.end <= self.history_end:
                summaries[summary.start] = summary
        return sorted(summaries.values(), key=lambda item: item.start)

    def _spikes_from_payload(self, payload: list[dict[str, Any]]) -> list[FluviusPeakMeasurement]:
        peaks: list[FluviusPeakMeasurement] = []
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
        self, payload: list[dict[str, Any]]
    ) -> list[FluviusQuarterHourlyMeasurement]:
        measurements = {}
        for interval in payload:
            summary = self._summarize_day(interval)
            if summary is None or not interval.get("de"):
                continue
            duration = (summary.end - summary.start).total_seconds()
            expected = 3600 if self._meter_type == METER_TYPE_GAS else 900
            if duration != expected:
                continue
            measurements[summary.start] = FluviusQuarterHourlyMeasurement(
                start=summary.start,
                end=summary.end,
                consumption=summary.metrics["consumption_total"],
                injection=summary.metrics["injection_total"],
                metrics=summary.metrics,
            )
        return sorted(measurements.values(), key=lambda item: item.start)

    def _summarize_day(self, day_data: dict[str, Any]) -> FluviusDailySummary | None:
        start = self._parse_datetime(day_data.get("d"))
        if not start:
            return None
        end = self._parse_datetime(day_data.get("de")) or (start + timedelta(days=1))
        metrics: dict[str, float] = {metric: 0.0 for metric in ALL_METRICS}
        target_unit = self._target_unit_code()
        matched = False

        for reading in day_data.get("v", []) or []:
            direction = self._safe_int(reading.get("dc"))
            tariff = self._safe_int(reading.get("t"), default=1)
            unit = self._safe_int(reading.get("u"))
            raw_value = reading.get("v")
            if raw_value is None:
                continue
            try:
                value = float(raw_value)
            except TypeError, ValueError:
                continue
            if not math.isfinite(value) or value < 0:
                continue

            if target_unit is not None and unit != target_unit:
                # Skip duplicate gas readings in the non-selected unit.
                continue
            if target_unit is None and unit != KILO_WATT_HOUR_UNIT_CODE:
                # Gas meters return both m3 and kWh. Skip the volume reading when
                # keeping the default energy-based sensors.
                continue

            metric_key = self._metric_from_reading(direction, tariff)
            if not metric_key:
                continue
            matched = True
            metrics[metric_key] += value

        if not matched or end <= start:
            return None
        metrics["consumption_total"] = metrics["consumption_high"] + metrics["consumption_low"]
        metrics["injection_total"] = metrics["injection_high"] + metrics["injection_low"]
        metrics["net_consumption"] = metrics["consumption_total"] - metrics["injection_total"]

        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        day_id = start.isoformat()
        return FluviusDailySummary(day_id=day_id, start=start, end=end, metrics=metrics)

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        fixed = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(fixed)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed

    @staticmethod
    def _metric_from_reading(direction: int, tariff: int) -> str | None:
        """Return the metric bucket that should be incremented for a reading."""

        if tariff not in (1, 2):
            return None
        is_high_tariff = tariff == 1
        if direction == 0:
            return "consumption_high" if is_high_tariff else "injection_high"
        if direction == 1:
            return "consumption_high" if is_high_tariff else "injection_high"
        if direction == 2:
            return "consumption_low" if is_high_tariff else "injection_low"
        return None

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except TypeError, ValueError:
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or 0.0)
        except TypeError, ValueError:
            return default
