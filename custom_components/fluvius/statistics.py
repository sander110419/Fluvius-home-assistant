"""Import delayed readings at their measurement time, independently of entities."""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from homeassistant.components.recorder.models import StatisticData, StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import FluviusDailySummary, FluviusEnergySharingSummary, FluviusQuarterHourlyMeasurement
from .const import DOMAIN, LIFETIME_METRICS

METRICS = (*LIFETIME_METRICS, "consumption_total", "injection_total")


def statistic_prefix(ean: str, meter_type: str, unit: str, history_until: str | None = None) -> str:
    """Keep IDs stable across entry recreation and separate incompatible histories."""
    prefix = f"{ean}_{meter_type}_{unit}"
    if history_until:
        prefix += "_until_" + sha256(history_until.encode()).hexdigest()[:12]
    return prefix


def _metrics(value: dict) -> dict[str, float]:
    result = {key: value.get(key, 0.0) for key in LIFETIME_METRICS}
    result["consumption_total"] = result["consumption_high"] + result["consumption_low"]
    result["injection_total"] = result["injection_high"] + result["injection_low"]
    return result


class FluviusStatistics:
    """An idempotent source ledger for corrections and history reimports.

    Source intervals are retained so a retry, restart, larger lookback or switch
    from daily to detailed readings can rebuild cumulative sums consistently.
    Recorder's supported external statistics API accepts whole UTC hours only.
    """

    def __init__(self, hass: HomeAssistant, prefix: str, name: str, unit: str) -> None:
        self.hass = hass
        self.prefix = prefix
        self.name = name
        self.unit = UnitOfVolume.CUBIC_METERS if unit == "m3" else unit
        self._store = Store(hass, 1, f"{DOMAIN}_history_{prefix}")
        self._data: dict = {"days": {}, "intervals": {}}
        self._needs_import = True

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {"days": {}, "intervals": {}}

    async def async_update(
        self,
        summaries: list[FluviusDailySummary],
        intervals: list[FluviusQuarterHourlyMeasurement],
    ) -> None:
        changed = False
        for table, items in (("days", summaries), ("intervals", intervals)):
            for item in items:
                start = item.start.astimezone(UTC).isoformat()
                metrics = item.metrics
                if not metrics and isinstance(item, FluviusQuarterHourlyMeasurement):
                    metrics = {
                        "consumption_high": item.consumption,
                        "injection_high": item.injection,
                    }
                value = {"end": item.end.astimezone(UTC).isoformat(), "metrics": _metrics(metrics)}
                if self._data[table].get(start) != value:
                    self._data[table][start] = value
                    changed = True
        if changed:
            await self._store.async_save(self._data)
        if changed or self._needs_import:
            self._import()
            self._needs_import = False

    def _hours(self) -> dict[datetime, dict[str, float]]:
        hours: dict[datetime, dict[str, float]] = defaultdict(lambda: dict.fromkeys(METRICS, 0.0))
        intervals = {datetime.fromisoformat(k): v for k, v in self._data["intervals"].items()}
        starts = sorted(intervals)
        consumed: set[datetime] = set()
        for key, day in sorted(self._data["days"].items()):
            start, end = datetime.fromisoformat(key), datetime.fromisoformat(day["end"])
            selected = starts[bisect_left(starts, start) : bisect_left(starts, end)]
            cursor = start
            for t in selected:
                if t != cursor:
                    break
                cursor = datetime.fromisoformat(intervals[t]["end"])
            # Prefer fine readings only when they cover the whole daily period.
            complete = bool(selected) and cursor == end
            if not complete:
                for metric, value in day["metrics"].items():
                    hours[start.replace(minute=0, second=0, microsecond=0)][metric] += value
                consumed.update(selected)
            # Retain zero hours when replacing a previous daily allocation.
            hour = start.replace(minute=0, second=0, microsecond=0)
            while hour < end:
                hours[hour]
                hour += timedelta(hours=1)
        for start, interval in sorted(intervals.items()):
            if start in consumed:
                continue
            hour = start.replace(minute=0, second=0, microsecond=0)
            for metric, value in interval["metrics"].items():
                hours[hour][metric] += value
        return dict(sorted(hours.items()))

    def _import(self) -> None:
        hours = self._hours()
        if not hours:
            return
        # An explicit baseline prevents the first hour being lost in delta charts.
        first_hour, last_hour = min(hours), max(hours)
        baseline = first_hour - timedelta(hours=1)
        for metric in METRICS:
            if "gas" in self.prefix and metric.startswith("injection"):
                continue
            total = 0.0
            statistics: list[StatisticData] = [{"start": baseline, "sum": 0.0, "state": 0.0}]
            start = first_hour
            while start <= last_hour:
                values = hours.get(start, dict.fromkeys(METRICS, 0.0))
                total = round(total + values[metric], 6)
                statistics.append({"start": start, "sum": total, "state": total})
                start += timedelta(hours=1)
            async_add_external_statistics(
                self.hass,
                {
                    "source": DOMAIN,
                    "statistic_id": f"{DOMAIN}:{self.prefix}_{metric}",
                    "name": f"{self.name} {metric.replace('_', ' ')} (historical)",
                    "unit_of_measurement": self.unit,
                    "unit_class": "volume" if self.unit == UnitOfVolume.CUBIC_METERS else "energy",
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                },
                statistics,
            )

    def get_totals(self) -> dict[str, float]:
        """Include detailed-only meters in the cumulative display sensors."""
        hours = self._hours()
        totals = {
            metric: round(sum(row[metric] for row in hours.values()), 6) for metric in METRICS
        }
        totals["net_consumption"] = totals["consumption_total"] - totals["injection_total"]
        return totals


class FluviusSharingStatistics:
    """Import monthly energiedelen (energy sharing) buckets as external statistics.

    Statistic IDs always contain ``_sharing_<exchange_id>_``, which makes a
    collision with the consumption/injection series (and therefore overwriting
    the 15-minute or hourly data) impossible: external statistics upsert by ID.
    Raw month buckets are stored so a retry, restart or correction rebuilds the
    cumulative series idempotently.
    """

    def __init__(self, hass: HomeAssistant, prefix: str, name: str) -> None:
        self.hass = hass
        self.prefix = prefix
        self.name = name
        self._store = Store(hass, 1, f"{DOMAIN}_sharing_history_{prefix}")
        self._data: dict = {"exchanges": {}}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {"exchanges": {}}

    def needs_backfill(self, exchange_id: int) -> bool:
        """True when no multi-year history fetch has completed for this exchange."""
        exchange = self._data.get("exchanges", {}).get(str(exchange_id), {})
        return not exchange.get("backfilled", False)

    async def async_update(
        self,
        summaries: list[FluviusEnergySharingSummary],
        backfilled_ids: set[int] | None = None,
    ) -> None:
        """Store monthly buckets and (re)import cumulative series that changed."""
        backfilled_ids = backfilled_ids or set()
        changed = False
        for summary in summaries:
            exchange = self._data["exchanges"].setdefault(
                str(summary.exchange_id), {"months": {}, "backfilled": False}
            )
            months = exchange["months"]
            for measurement in summary.monthly:
                key = measurement.start.astimezone(UTC).isoformat()
                value = {
                    "received": round(measurement.consumption, 4),
                    "injected": round(measurement.injection, 4),
                }
                if months.get(key) != value:
                    months[key] = value
                    changed = True
            if summary.exchange_id in backfilled_ids and not exchange["backfilled"]:
                exchange["backfilled"] = True
                changed = True
        if changed:
            await self._store.async_save(self._data)
            self._import()

    def _import(self) -> None:
        for exchange_id, exchange in sorted(self._data["exchanges"].items()):
            months = exchange.get("months", {})
            if not months:
                continue
            starts = sorted(datetime.fromisoformat(key) for key in months)
            for metric, field in (("received", "received"), ("injected", "injected")):
                statistics: list[StatisticData] = [
                    {"start": starts[0] - timedelta(hours=1), "sum": 0.0, "state": 0.0}
                ]
                total = 0.0
                for start in starts:
                    total = round(total + months[start.isoformat()][field], 6)
                    statistics.append({"start": start, "sum": total, "state": total})
                async_add_external_statistics(
                    self.hass,
                    {
                        "source": DOMAIN,
                        "statistic_id": f"{DOMAIN}:{self.prefix}_sharing_{exchange_id}_{metric}",
                        "name": f"{self.name} shared energy {metric} (exchange {exchange_id})",
                        "unit_of_measurement": "kWh",
                        "unit_class": "energy",
                        "has_sum": True,
                        "mean_type": StatisticMeanType.NONE,
                    },
                    statistics,
                )
