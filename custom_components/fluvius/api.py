"""HTTP client helpers for the Fluvius Energy integration."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .auth import FluviusAuthError, async_get_bearer_token
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
    INTERVAL_GRANULARITY_CANDIDATES,
    INTERVAL_MINUTES_BY_METER_TYPE,
    METER_TYPE_ELECTRICITY,
    METER_TYPE_GAS,
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
        self._token_expires = 0.0
        self._verbose = bool(self._options.get(CONF_VERBOSE_LOGGING, DEFAULT_VERBOSE_LOGGING))
        # Resolved on the first successful interval fetch and sticky for the lifetime
        # of the client, so the probe costs a few extra requests only once.
        self._interval_granularity: str | None = None
        self._resolved_interval_minutes: int | None = None
        # What each probed code answered, for a single diagnostic line.
        self._probe_outcomes: dict[str, str] = {}
        # Set when a full probe found nothing, to stop re-asking every hour. Reloading
        # the entry rebuilds the client, so a future Fluvius change is still picked up.
        self._interval_unavailable = False

    def _log_verbose(self, message: str, *args: Any) -> None:
        """Log a message only if verbose logging is enabled."""
        if self._verbose:
            LOGGER.debug("[VERBOSE] " + message, *args)

    @property
    def interval_minutes(self) -> int:
        """Native resolution of this meter: 15 for electricity, 60 for gas."""

        return self._expected_interval_minutes()

    @property
    def resolved_granularity(self) -> str | None:
        """Granularity code the probe settled on, None while still unresolved."""

        return self._interval_granularity

    @property
    def probe_outcomes(self) -> dict[str, str]:
        """What each probed granularity code answered, for diagnostics."""

        return dict(self._probe_outcomes)

    @property
    def interval_unavailable(self) -> bool:
        """True once a full probe established this meter has no sub-daily data."""

        return self._interval_unavailable

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

        Electricity is registered per quarter-hour, gas per hour, but Fluvius does not
        document which granularity code serves which, so the code is probed once and
        then reused. The legacy method name is retained for callers. Each request
        covers one local day, aligned on the meter's own day.
        """
        if self._interval_unavailable:
            LOGGER.debug(
                "Skipping the interval fetch for meter %s: a full probe already "
                "established that Fluvius serves no sub-daily data here. "
                "Reload the entry to retry.",
                self._meter_serial,
            )
            return []

        count = self.days_back if days_back is None else max(1, int(days_back))
        token = await self._async_get_access_token()
        measurements = {}
        # Oldest first, so the probe hits the day most likely to hold data.
        for offset in range(count, 0, -1):
            for measurement in await self._async_fetch_interval_day(token, offset):
                if measurement.end <= self.history_end:
                    measurements[measurement.start] = measurement

        if not measurements:
            self._report_empty_interval_window(count)
        return sorted(measurements.values(), key=lambda item: item.start)

    def _report_empty_interval_window(self, count: int) -> None:
        """Explain an empty window, and stop probing when no code ever worked."""

        if self._interval_granularity is not None:
            # The code is known to work, so this is a transient gap (publication
            # delay, outage). Keep trying on the next refresh.
            LOGGER.debug(
                "No interval data this refresh for meter %s, although granularity=%s "
                "is known to work. Fluvius may not have published the requested "
                "days yet.",
                self._meter_serial,
                self._interval_granularity,
            )
            return
        failed = [code for code, outcome in self._probe_outcomes.items() if "failed" in outcome]
        if failed or not self._probe_outcomes:
            # Requests were rejected rather than answered, so this says nothing about
            # what the meter holds. Stay enabled and retry on the next refresh.
            LOGGER.warning(
                "The interval probe could not reach Fluvius for meter %s (granularity "
                "code(s) %s failed). Outcome per code: %s. Retrying on the next refresh.",
                self._meter_serial,
                ", ".join(sorted(failed)) or "none",
                self._format_probe_report(),
            )
            return
        # Every candidate was tried on every day of the window and each answered with
        # an empty payload: this meter has no sub-daily data. Stop asking, because the
        # probe costs one request per candidate per day, every refresh.
        self._interval_unavailable = True
        LOGGER.warning(
            "No %d-minute data for this %s meter after probing %d day(s). Outcome per "
            "granularity code: %s. The interval fetch is now disabled for this entry; "
            "the daily sensors are unaffected. Reload the entry to probe again.",
            self._expected_interval_minutes(),
            self._meter_type,
            count,
            self._format_probe_report(),
        )

    def _format_probe_report(self) -> str:
        """Render the per-candidate probe outcomes for a single log line."""

        if not self._probe_outcomes:
            return "no request was made"
        return ", ".join(
            f"granularity={code}: {outcome}"
            for code, outcome in sorted(self._probe_outcomes.items())
        )

    async def _async_fetch_interval_day(
        self, access_token: str, days_back: int
    ) -> list[FluviusQuarterHourlyMeasurement]:
        """Fetch one day, resolving the granularity code on first use."""

        if self._interval_granularity is None:
            return await self._async_probe_granularity(access_token, days_back)
        payload = await self._fetch_raw_quarter_hourly(
            access_token, days_back, self._interval_granularity
        )
        return self._quarter_hourly_from_payload(payload)

    async def _async_probe_granularity(
        self, access_token: str, days_back: int
    ) -> list[FluviusQuarterHourlyMeasurement]:
        """Try each candidate code until one returns the expected interval length.

        Nothing is remembered until real data comes back: an empty payload only means
        Fluvius has not published that day yet, so it must not disqualify a code.
        """

        expected = self._expected_interval_minutes()
        best_effort: tuple[str, list[FluviusQuarterHourlyMeasurement], int] | None = None

        for candidate in self._granularity_candidates():
            try:
                payload = await self._fetch_raw_quarter_hourly(access_token, days_back, candidate)
            except FluviusAuthenticationError:
                raise
            except FluviusApiError as err:
                LOGGER.debug("Interval probe: granularity=%s rejected (%s)", candidate, err)
                self._probe_outcomes[candidate] = f"request failed ({err})"
                continue

            measurements = self._intervals_from_payload(payload)
            detected = self._detect_interval_minutes(measurements)
            self._probe_outcomes[candidate] = (
                "no data" if detected is None else f"{detected}-minute intervals"
            )
            if detected == expected:
                LOGGER.debug(
                    "Interval probe: confirmed %d-minute intervals with granularity=%s for %s",
                    detected,
                    candidate,
                    self._meter_type,
                )
                return self._settle_on_granularity(candidate, detected, measurements)
            if detected is None:
                continue
            if best_effort is None:
                best_effort = (candidate, measurements, detected)

        if best_effort is not None:
            candidate, measurements, detected = best_effort
            LOGGER.warning(
                "No granularity code returned %d-minute intervals for this %s meter. "
                "Falling back to granularity=%s, which serves %d-minute intervals; "
                "the data is still imported at that resolution.",
                expected,
                self._meter_type,
                candidate,
                detected,
            )
            return self._settle_on_granularity(candidate, detected, measurements)

        LOGGER.debug(
            "Interval probe: no candidate returned data for day -%d; will retry next refresh",
            days_back,
        )
        return []

    def _settle_on_granularity(
        self,
        candidate: str,
        detected: int,
        measurements: list[FluviusQuarterHourlyMeasurement],
    ) -> list[FluviusQuarterHourlyMeasurement]:
        """Remember the probed code and length, then keep only matching intervals."""

        self._interval_granularity = candidate
        self._resolved_interval_minutes = detected
        return self._keep_resolved_intervals(measurements)

    def _granularity_candidates(self) -> tuple[str, ...]:
        """Codes to probe, the configured one first so the option still counts."""

        candidates = INTERVAL_GRANULARITY_CANDIDATES.get(
            self._meter_type, INTERVAL_GRANULARITY_CANDIDATES[METER_TYPE_ELECTRICITY]
        )
        configured = str(self._options.get(CONF_GRANULARITY, DEFAULT_GRANULARITY))
        if configured in (DEFAULT_GRANULARITY, ""):
            return candidates
        return (configured, *(code for code in candidates if code != configured))

    def _expected_interval_minutes(self) -> int:
        return INTERVAL_MINUTES_BY_METER_TYPE.get(
            self._meter_type, INTERVAL_MINUTES_BY_METER_TYPE[METER_TYPE_ELECTRICITY]
        )

    @staticmethod
    def _detect_interval_minutes(
        measurements: list[FluviusQuarterHourlyMeasurement],
    ) -> int | None:
        """Return the most frequent interval length, in minutes."""

        counts: dict[int, int] = {}
        for item in measurements:
            minutes = round((item.end - item.start).total_seconds() / 60)
            if minutes <= 0:
                continue
            counts[minutes] = counts.get(minutes, 0) + 1
        if not counts:
            return None
        return max(counts, key=counts.__getitem__)

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
        try:
            access_token, tokens = await async_get_bearer_token(
                self._session,
                self._email,
                self._password,
                remember_me=self._remember_me,
                verbose=self._verbose,
            )
        except FluviusAuthError as err:
            raise FluviusAuthenticationError("Fluvius authentication failed") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FluviusApiError("Cannot reach Fluvius authentication") from err
        if not access_token:
            raise FluviusAuthenticationError("Fluvius returned no access token")
        self._access_token = access_token
        self._token_expires = time.monotonic() + max(0, int(tokens.get("expires_in", 3600)) - 60)
        return access_token

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
                            raise FluviusAuthenticationError("Fluvius rejected the access token")
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
        raise FluviusAuthenticationError("Fluvius rejected the access token")

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
        self, access_token: str, days_back: int, granularity: str
    ) -> list[dict[str, Any]]:
        return await self._request_history(
            access_token,
            {
                **self._build_quarter_hourly_range(days_back),
                "granularity": granularity,
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
        """Parse the payload, keeping only intervals of the resolved length."""

        return self._keep_resolved_intervals(self._intervals_from_payload(payload))

    def _intervals_from_payload(
        self, payload: list[dict[str, Any]]
    ) -> list[FluviusQuarterHourlyMeasurement]:
        """Parse every interval in the payload, whatever its length.

        The granularity probe needs the raw lengths to tell what a candidate code
        actually served; every other caller wants _quarter_hourly_from_payload.
        """

        measurements = {}
        for interval in payload:
            summary = self._summarize_day(interval)
            if summary is None or not interval.get("de"):
                continue
            measurements[summary.start] = FluviusQuarterHourlyMeasurement(
                start=summary.start,
                end=summary.end,
                consumption=summary.metrics["consumption_total"],
                injection=summary.metrics["injection_total"],
                metrics=summary.metrics,
            )
        return sorted(measurements.values(), key=lambda item: item.start)

    def _keep_resolved_intervals(
        self, measurements: list[FluviusQuarterHourlyMeasurement]
    ) -> list[FluviusQuarterHourlyMeasurement]:
        """Drop intervals that are not the length this meter is being read at.

        Mixed lengths in one import would double-count, since the statistics layer
        buckets everything into whole hours. Before the probe resolves, the meter's
        native resolution is assumed; afterwards, whatever the probe settled on.
        """

        seconds = (self._resolved_interval_minutes or self._expected_interval_minutes()) * 60
        return [item for item in measurements if (item.end - item.start).total_seconds() == seconds]

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
