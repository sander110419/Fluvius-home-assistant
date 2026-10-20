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
    FluviusCredentialsError,
    FluviusInvalidRefreshError,
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
    GAS_DAY_START_HOUR,
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

ENERGIEDELEN_API_BASE = "https://mijn.fluvius.be/energiedelen/api"
# chartPeriodType values used by the Mijn Fluvius energiedelen app:
# 6 = month view (day-bucketed), 7 = year view (month-bucketed).
CHART_PERIOD_TYPE_MONTH = 6
CHART_PERIOD_TYPE_YEAR = 7
# Maximum number of past calendar years to backfill for energiedelen history.
MAX_SHARING_HISTORY_YEARS = 5


class FluviusApiError(RuntimeError):
    """Raised when the Fluvius API call fails."""


class FluviusAuthenticationError(FluviusApiError):
    """Credentials must be updated through Home Assistant reauthentication."""


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


@dataclass(slots=True)
class FluviusEnergySharingMeasurement:
    """Container for a single exchanged-energy bucket (day or month)."""

    start: datetime
    consumption: float  # shared energy received in this bucket (kWh)
    injection: float  # shared energy injected in this bucket (kWh)


@dataclass(slots=True)
class FluviusEnergySharingSummary:
    """Exchanged-energy buckets and totals for one energy exchange (energiedelen)."""

    exchange_id: int
    name: str | None
    daily: list[FluviusEnergySharingMeasurement] = field(default_factory=list)
    monthly: list[FluviusEnergySharingMeasurement] = field(default_factory=list)
    totals: dict[str, float] = field(default_factory=dict)

    def compute_totals(self, tz) -> dict[str, float]:
        """Compute received/injected totals for today, this month and this year."""
        now = datetime.now(tz)
        today = now.date()

        def _received(measurements: list[FluviusEnergySharingMeasurement], pred) -> float:
            return round(
                sum(m.consumption for m in measurements if pred(m.start.astimezone(tz))), 3
            )

        def _injected(measurements: list[FluviusEnergySharingMeasurement], pred) -> float:
            return round(sum(m.injection for m in measurements if pred(m.start.astimezone(tz))), 3)

        self.totals = {
            "received_today": _received(self.daily, lambda d: d.date() == today),
            "injected_today": _injected(self.daily, lambda d: d.date() == today),
            "received_month": _received(
                self.daily, lambda d: (d.year, d.month) == (now.year, now.month)
            ),
            "injected_month": _injected(
                self.daily, lambda d: (d.year, d.month) == (now.year, now.month)
            ),
            "received_year": _received(self.monthly, lambda d: d.year == now.year),
            "injected_year": _injected(self.monthly, lambda d: d.year == now.year),
        }
        return self.totals


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
        token_store: Any = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._ean = ean
        self._meter_serial = meter_serial
        self._meter_type = meter_type
        self._remember_me = remember_me
        self._options = options or {}
        self._token_store = token_store
        self._access_token: str | None = None
        self._token_expires = 0.0
        self._refresh_token: str | None = None
        self._stored_tokens_loaded = False
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

    async def fetch_energy_sharing(
        self, *, history_for: set[int] | None = None
    ) -> list[FluviusEnergySharingSummary]:
        """Fetch energiedelen (energy sharing) exchanged energy, if any.

        Accounts without energy sharing get an empty list. Failures never block
        the regular consumption polling: callers should treat FluviusApiError
        as "sharing temporarily unavailable".

        ``history_for`` lists exchange IDs that need a multi-year backfill: for
        those, month buckets are also requested for past calendar years (walking
        back until an empty year, capped at MAX_SHARING_HISTORY_YEARS).
        """
        if self._meter_type == METER_TYPE_GAS:
            return []
        payload = await self._request_energiedelen("GET", "energy-exchanges")
        if isinstance(payload, dict):
            exchanges = payload.get("data") or []
        elif isinstance(payload, list):
            exchanges = payload
        else:
            exchanges = []
        if not exchanges:
            return []

        tz = self._resolve_timezone(self._options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE))
        (month_start, month_end), (year_start, year_end) = self._energy_sharing_ranges(tz)
        history_for = history_for or set()
        summaries: list[FluviusEnergySharingSummary] = []
        for exchange in exchanges:
            if not isinstance(exchange, dict):
                continue
            exchange_id = exchange.get("id") or exchange.get("energyExchangeId")
            if exchange_id is None:
                continue
            summary = FluviusEnergySharingSummary(
                exchange_id=int(exchange_id),
                name=exchange.get("name") or exchange.get("description"),
            )
            summary.daily = await self._fetch_exchanged_energy(
                summary.exchange_id, CHART_PERIOD_TYPE_MONTH, month_start, month_end
            )
            summary.monthly = await self._fetch_exchanged_energy(
                summary.exchange_id, CHART_PERIOD_TYPE_YEAR, year_start, year_end
            )
            if summary.exchange_id in history_for:
                summary.monthly = await self._fetch_sharing_history(
                    summary.exchange_id, summary.monthly, year_start, tz
                )
            summary.compute_totals(tz)
            summaries.append(summary)
        return summaries

    async def _fetch_sharing_history(
        self,
        exchange_id: int,
        current_monthly: list[FluviusEnergySharingMeasurement],
        current_year_start: datetime,
        tz,
    ) -> list[FluviusEnergySharingMeasurement]:
        """Walk back past calendar years until one returns no month buckets."""
        merged = {m.start: m for m in current_monthly}
        for years_back in range(1, MAX_SHARING_HISTORY_YEARS + 1):
            start = current_year_start.replace(year=current_year_start.year - years_back)
            end = current_year_start.replace(year=current_year_start.year - years_back + 1)
            buckets = await self._fetch_exchanged_energy(
                exchange_id, CHART_PERIOD_TYPE_YEAR, start, end
            )
            if not buckets:
                break
            merged.update({m.start: m for m in buckets})
        return [merged[key] for key in sorted(merged)]

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
        await self._async_load_stored_tokens()
        if self._access_token and time.monotonic() < self._token_expires:
            return self._access_token
        if self._refresh_token:
            try:
                access_token, tokens = await async_refresh_bearer_token(
                    self._session, self._refresh_token, verbose=self._verbose
                )
            except FluviusInvalidRefreshError:
                self._log_verbose("Refresh token rejected; falling back to full login")
                self._refresh_token = None
                await self._async_persist_tokens()
            except (FluviusAuthError, aiohttp.ClientError, TimeoutError) as err:
                # Transient failure (WAF, throttling, 5xx, network). Retry next
                # cycle instead of triggering a reauthentication flow.
                raise FluviusApiError("Fluvius token refresh is temporarily unavailable") from err
            else:
                self._store_tokens(access_token, tokens)
                await self._async_persist_tokens()
                return access_token
        try:
            access_token, tokens = await async_get_bearer_token(
                self._session,
                self._email,
                self._password,
                remember_me=self._remember_me,
                verbose=self._verbose,
            )
        except FluviusCredentialsError as err:
            raise FluviusAuthenticationError("Fluvius rejected the credentials") from err
        except FluviusAuthError as err:
            # HTML scraping or endpoint hiccup during full login: transient.
            raise FluviusApiError("Fluvius authentication is temporarily unavailable") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FluviusApiError("Cannot reach Fluvius authentication") from err
        if not access_token:
            raise FluviusApiError("Fluvius returned no access token")
        self._store_tokens(access_token, tokens)
        await self._async_persist_tokens()
        return access_token

    def _store_tokens(self, access_token: str, tokens: dict[str, Any]) -> None:
        self._access_token = access_token
        self._token_expires = time.monotonic() + max(0, int(tokens.get("expires_in", 3600)) - 60)
        if tokens.get("refresh_token"):
            self._refresh_token = tokens["refresh_token"]

    async def _async_load_stored_tokens(self) -> None:
        if self._stored_tokens_loaded:
            return
        self._stored_tokens_loaded = True
        if self._token_store is None:
            return
        try:
            data = await self._token_store.async_load()
        except Exception:  # noqa: BLE001 - token storage must never break auth
            LOGGER.debug("Unable to load stored Fluvius tokens", exc_info=True)
            return
        if not data:
            return
        self._refresh_token = data.get("refresh_token")
        expires_at = float(data.get("access_expires_at") or 0.0)
        remaining = expires_at - time.time()
        if data.get("access_token") and remaining > 120:
            self._access_token = data["access_token"]
            self._token_expires = time.monotonic() + remaining - 60

    async def _async_persist_tokens(self) -> None:
        if self._token_store is None:
            return
        expires_in = max(0.0, self._token_expires - time.monotonic())
        data = {
            "refresh_token": self._refresh_token,
            "access_token": self._access_token,
            "access_expires_at": time.time() + expires_in if self._access_token else 0.0,
        }
        try:
            await self._token_store.async_save(data)
        except Exception:  # noqa: BLE001 - token storage must never break auth
            LOGGER.debug("Unable to persist Fluvius tokens", exc_info=True)

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
                            # A freshly issued token was still rejected: the
                            # credentials were just validated by the token
                            # endpoint, so this is a service-side problem.
                            raise FluviusApiError("Fluvius rejected a fresh access token")
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
        raise FluviusApiError("Fluvius rejected a fresh access token")

    async def _request_energiedelen(
        self, method: str, path: str, *, json_body: dict | None = None
    ) -> Any:
        """Call the energiedelen API with the same token as the history API.

        Returns None when the feature is unavailable for the account (403/404).
        """
        url = f"{ENERGIEDELEN_API_BASE}/{path}"
        self._log_verbose("Energiedelen request %s %s", method, path)
        for attempt in range(2):
            access_token = self._access_token or await self._async_get_access_token()
            try:
                async with self._session.request(
                    method,
                    url,
                    json=json_body,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                    timeout=30,
                ) as response:
                    if response.status == 401:
                        self._access_token = None
                        if attempt:
                            raise FluviusApiError("Fluvius rejected a fresh access token")
                    elif response.status in (403, 404):
                        return None
                    else:
                        response.raise_for_status()
                        return await response.json()
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise FluviusApiError(
                    f"Fluvius energiedelen request failed ({type(err).__name__})"
                ) from err
        raise FluviusApiError("Fluvius rejected a fresh access token")

    async def _fetch_exchanged_energy(
        self, exchange_id: int, period_type: int, start: datetime, end: datetime
    ) -> list[FluviusEnergySharingMeasurement]:
        body = {
            "energyExchangeId": exchange_id,
            "eans": [self._ean],
            "chartPeriodType": period_type,
            "startDateUtc": start.isoformat(timespec="milliseconds"),
            "endDateUtc": end.isoformat(timespec="milliseconds"),
        }
        payload = await self._request_energiedelen(
            "POST", f"energy-exchanges/{exchange_id}/chart-exchanged-energy", json_body=body
        )
        return self._exchanged_energy_from_payload(payload)

    def _exchanged_energy_from_payload(self, payload: Any) -> list[FluviusEnergySharingMeasurement]:
        if isinstance(payload, dict):
            items = payload.get("exchangedEnergy") or payload.get("data") or []
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        if payload is not None and not items:
            self._log_verbose("Energiedelen payload contained no items: %s", str(payload)[:200])
        measurements: list[FluviusEnergySharingMeasurement] = []
        dropped = 0
        for item in items:
            if not isinstance(item, dict):
                dropped += 1
                continue
            start = self._parse_datetime(
                item.get("startDateUtc") or item.get("startDate") or item.get("d")
            )
            measurement = item.get("measurement")
            if not isinstance(measurement, dict):
                measurement = item
            consumption = measurement.get("consumptionValue")
            injection = measurement.get("injectionValue")
            if start is None or (consumption is None and injection is None):
                dropped += 1
                continue
            measurements.append(
                FluviusEnergySharingMeasurement(
                    start=start,
                    consumption=self._safe_float(consumption),
                    injection=self._safe_float(injection),
                )
            )
        if dropped:
            self._log_verbose("Dropped %d energiedelen items with unexpected shape", dropped)
        return sorted(measurements, key=lambda item: item.start)

    @staticmethod
    def _energy_sharing_ranges(tz) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
        now = datetime.now(tz)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if month_start.month == 12:
            month_end = month_start.replace(year=month_start.year + 1, month=1)
        else:
            month_end = month_start.replace(month=month_start.month + 1)
        year_start = month_start.replace(month=1)
        year_end = year_start.replace(year=year_start.year + 1)
        return (month_start, month_end), (year_start, year_end)

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
        if self._meter_type == METER_TYPE_GAS:
            return self._build_gas_day_range(days_back)
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

    def _build_gas_day_range(self, days_back: int) -> dict[str, str]:
        """Align a gas request on the 06:00 -> 06:00 gas day.

        Gas daily summaries span the gas day, and statistics.py only replaces a daily
        total with interval readings when those tile the summary period exactly. A
        calendar-day window cannot tile a gas day, so the fallback fires and the whole
        day lands in the 06:00 bucket.

        Unlike electricity the range is therefore never clamped to a history cutoff
        falling inside a gas day: a truncated window would not tile either. The anchor
        steps back to the last gas day that had closed by the cutoff instead.
        """

        end = self.history_end
        anchor = (end - timedelta(microseconds=1)).replace(
            hour=GAS_DAY_START_HOUR, minute=0, second=0, microsecond=0
        )
        # Wall-clock arithmetic, so the span stays a real day across a DST change.
        if anchor + timedelta(days=1) > end:
            anchor -= timedelta(days=1)
        start = anchor - timedelta(days=max(days_back, 1) - 1)
        until = start + timedelta(days=1) - timedelta(milliseconds=1)
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
