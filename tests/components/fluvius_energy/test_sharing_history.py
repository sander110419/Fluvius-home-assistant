"""Tests for energiedelen multi-year backfill, statistics isolation and sensor naming.

The isolation tests matter most: energiedelen history is imported as external
statistics, and the IDs used there must never overlap with the consumption
series fed by 15-minute/hourly readings.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.fluvius.api import (
    ENERGIEDELEN_API_BASE,
    MAX_SHARING_HISTORY_YEARS,
    FluviusApiClient,
    FluviusEnergySharingMeasurement,
    FluviusEnergySharingSummary,
)
from custom_components.fluvius.statistics import FluviusSharingStatistics, FluviusStatistics


class DummyResponse:
    def __init__(self, status: int = 200, payload=None) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self._payload


class FakeSession:
    """Routes energiedelen calls; chart payloads keyed by (chartPeriodType, start year)."""

    def __init__(self, exchanges) -> None:
        self.overview_payload = {"data": exchanges, "totalItems": len(exchanges)}
        self.chart_payloads: dict[tuple[int, int], object] = {}
        self.calls: list[dict] = []

    def request(self, method, url, *, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json})
        if url == f"{ENERGIEDELEN_API_BASE}/energy-exchanges":
            return DummyResponse(200, self.overview_payload)
        if "/chart-exchanged-energy" in url:
            body = json or {}
            year = int(str(body.get("startDateUtc", "0000"))[:4])
            payload = self.chart_payloads.get((body.get("chartPeriodType"), year))
            return DummyResponse(200, payload if payload is not None else [])
        return DummyResponse(404, None)


def _make_client(session: FakeSession) -> FluviusApiClient:
    client = FluviusApiClient(
        session=session,  # type: ignore[arg-type]
        email="user@example.com",
        password="secret",
        ean="541448800000000000",
        meter_serial="1SAGTEST",
        options={"timezone": "Europe/Brussels"},
    )
    client._access_token = "cached-token"  # pylint: disable=protected-access
    client._token_expires = time.monotonic() + 3600  # pylint: disable=protected-access
    return client


def _month_bucket(year: int, month: int, received: float, injected: float = 0.0) -> dict:
    return {
        "startDateUtc": datetime(year, month, 1, tzinfo=UTC).isoformat(),
        "measurement": {"consumptionValue": received, "injectionValue": injected},
    }


def _freeze_now(monkeypatch, fixed_now: datetime) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz:
                return fixed_now.astimezone(tz)
            return fixed_now

    monkeypatch.setattr("custom_components.fluvius.api.datetime", FixedDateTime)


async def test_backfill_walks_back_until_first_empty_year(monkeypatch):
    """History walk: current year + 2025 data, 2024 empty -> stop before 2023."""

    _freeze_now(monkeypatch, datetime(2026, 10, 20, 10, 0, tzinfo=UTC))
    session = FakeSession([{"id": 42, "name": "Group"}])
    session.chart_payloads[(7, 2026)] = [_month_bucket(2026, 8, 10.0), _month_bucket(2026, 9, 12.0)]
    session.chart_payloads[(7, 2025)] = [_month_bucket(2025, 11, 5.0), _month_bucket(2025, 12, 6.0)]
    # 2024: no payload -> empty -> stop.
    client = _make_client(session)

    summaries = await client.fetch_energy_sharing(history_for={42})

    assert len(summaries) == 1
    monthly = summaries[0].monthly
    assert [m.start.year for m in monthly] == [2025, 2025, 2026, 2026]
    # 1 overview GET + 1 month POST + 3 year POSTs (2026, 2025, 2024-empty)
    post_years = sorted(
        c["json"]["startDateUtc"][:4] for c in session.calls if c["method"] == "POST"
    )
    assert post_years == ["2024", "2025", "2026", "2026"]
    # Totals only consider the current year even with old buckets present.
    assert summaries[0].totals["received_year"] == pytest.approx(22.0)


async def test_backfill_is_capped(monkeypatch):
    """Even with data forever, the walk stops after MAX_SHARING_HISTORY_YEARS."""

    _freeze_now(monkeypatch, datetime(2026, 10, 20, 10, 0, tzinfo=UTC))
    session = FakeSession([{"id": 42}])
    for year in range(2021, 2027):
        session.chart_payloads[(7, year)] = [_month_bucket(year, 6, 1.0)]
    client = _make_client(session)

    summaries = await client.fetch_energy_sharing(history_for={42})

    years = {m.start.year for m in summaries[0].monthly}
    assert years == set(range(2026 - MAX_SHARING_HISTORY_YEARS, 2027))
    year_posts = [
        c for c in session.calls if c["method"] == "POST" and c["json"]["chartPeriodType"] == 7
    ]
    assert len(year_posts) == MAX_SHARING_HISTORY_YEARS + 1  # current year + cap


async def test_no_history_requested_by_default(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 10, 20, 10, 0, tzinfo=UTC))
    session = FakeSession([{"id": 42}])
    session.chart_payloads[(7, 2026)] = [_month_bucket(2026, 10, 1.0)]
    client = _make_client(session)

    summaries = await client.fetch_energy_sharing()

    posts = [c for c in session.calls if c["method"] == "POST"]
    assert len(posts) == 2  # month view + current year view only
    assert {m.start.year for m in summaries[0].monthly} == {2026}


def _sharing(hass):
    obj = FluviusSharingStatistics(hass, "test_electricity_kwh", "Test meter")
    obj._store = MagicMock(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
    return obj


def _summary(
    exchange_id: int, months: list[FluviusEnergySharingMeasurement]
) -> FluviusEnergySharingSummary:
    summary = FluviusEnergySharingSummary(exchange_id=exchange_id, name="Group")
    summary.monthly = months
    summary.compute_totals(UTC)
    return summary


def _months(*pairs: tuple[int, int, float]) -> list[FluviusEnergySharingMeasurement]:
    return [
        FluviusEnergySharingMeasurement(datetime(y, m, 1, tzinfo=UTC), received, 0.0)
        for y, m, received in pairs
    ]


async def test_import_writes_only_dedicated_sharing_ids(hass):
    """Every imported statistic_id must carry _sharing_<id>_; none may look like
    a consumption series (those are fed by the 15-minute/hourly pipeline)."""

    stats = _sharing(hass)
    summary = _summary(42, _months((2025, 11, 5.0), (2025, 12, 6.0), (2026, 1, 7.0)))
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add_stats:
        await stats.async_update([summary], backfilled_ids={42})

    written_ids = {call.args[1]["statistic_id"] for call in add_stats.call_args_list}
    assert written_ids == {
        "fluvius:test_electricity_kwh_sharing_42_received",
        "fluvius:test_electricity_kwh_sharing_42_injected",
    }
    for call in add_stats.call_args_list:
        metadata = call.args[1]
        assert "_sharing_" in metadata["statistic_id"]
        assert metadata["unit_of_measurement"] == "kWh"
        assert metadata["has_sum"] is True
        # Every row start must be hour-aligned (recorder requirement).
        for row in call.args[2]:
            assert row["start"].minute == 0 and row["start"].second == 0


async def test_cumulative_sums_baseline_and_idempotent_rewrite(hass):
    stats = _sharing(hass)
    summary = _summary(42, _months((2025, 11, 5.0), (2025, 12, 6.0), (2026, 1, 7.0)))
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add_stats:
        await stats.async_update([summary], backfilled_ids={42})

    received = next(
        call
        for call in add_stats.call_args_list
        if call.args[1]["statistic_id"].endswith("received")
    )
    rows = received.args[2]
    assert rows[0]["start"] == datetime(2025, 11, 1, tzinfo=UTC) - timedelta(hours=1)
    assert rows[0]["sum"] == 0.0  # baseline so the first month is not lost
    assert [row["sum"] for row in rows[1:]] == [5.0, 11.0, 18.0]

    # Identical data again: no save, no re-import.
    with patch(
        "custom_components.fluvius.statistics.async_add_external_statistics"
    ) as add_stats_again:
        await stats.async_update([summary])
    assert add_stats_again.call_args_list == []


async def test_correction_rebuilds_cumulative_series(hass):
    stats = _sharing(hass)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics"):
        await stats.async_update([_summary(42, _months((2025, 11, 5.0), (2025, 12, 6.0)))])

    corrected = _summary(42, _months((2025, 11, 8.0), (2025, 12, 6.0)))
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add_stats:
        await stats.async_update([corrected])
    received = next(
        call
        for call in add_stats.call_args_list
        if call.args[1]["statistic_id"].endswith("received")
    )
    rows = received.args[2]
    assert [row["sum"] for row in rows[1:]] == [8.0, 14.0]


async def test_needs_backfill_flag_per_exchange(hass):
    stats = _sharing(hass)
    assert stats.needs_backfill(42) is True
    with patch("custom_components.fluvius.statistics.async_add_external_statistics"):
        await stats.async_update([_summary(42, _months((2026, 1, 1.0)))], backfilled_ids={42})
    assert stats.needs_backfill(42) is False
    assert stats.needs_backfill(7) is True  # a new exchange still needs backfill


async def test_consumption_and_sharing_series_are_disjoint(hass):
    """Run both importers; the ID sets must not overlap, so the 15-minute and
    hourly consumption statistics can never be overwritten by sharing imports."""

    from custom_components.fluvius.api import FluviusDailySummary

    consumption = FluviusStatistics(hass, "test_electricity_kwh", "Test meter", "kWh")
    consumption._store = MagicMock(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
    sharing = _sharing(hass)

    day_start = datetime(2026, 10, 1, tzinfo=UTC)
    daily = FluviusDailySummary(
        day_start.isoformat(), day_start, day_start, {"consumption_high": 3.0}
    )
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add_stats:
        await consumption.async_update([daily], [])
        await sharing.async_update([_summary(42, _months((2026, 10, 2.5)))], backfilled_ids={42})

    ids = {call.args[1]["statistic_id"] for call in add_stats.call_args_list}
    consumption_ids = {sid for sid in ids if "_sharing_" not in sid}
    sharing_ids = {sid for sid in ids if "_sharing_" in sid}
    assert consumption_ids  # the consumption importer really wrote something
    assert sharing_ids
    assert not consumption_ids & sharing_ids
    assert all(sid.startswith("fluvius:test_electricity_kwh_") for sid in ids)
