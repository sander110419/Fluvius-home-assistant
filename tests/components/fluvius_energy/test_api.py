"""Tests for the Fluvius API helper logic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import asyncio
from unittest.mock import MagicMock

import aiohttp

from custom_components.fluvius_energy.api import FluviusApiClient
from custom_components.fluvius_energy.const import (
    CONF_DAYS_BACK,
    CONF_GRANULARITY,
    GAS_MIN_LOOKBACK_DAYS,
    GAS_SUPPORTED_GRANULARITY,
    METER_TYPE_GAS,
)


def _make_client(*, meter_type: str | None = None, options: dict | None = None) -> FluviusApiClient:
    kwargs = {}
    if meter_type is not None:
        kwargs["meter_type"] = meter_type
    session = MagicMock(spec=aiohttp.ClientSession)
    return FluviusApiClient(
        session=session,
        email="user@example.com",
        password="secret",
        ean="541448800000000000",
        meter_serial="1SAGTEST",
        options=options or {},
        **kwargs,
    )


def test_gas_day_uses_kwh_values_only():
    """Ensure gas readings drop the m³ duplicate and store the kWh value."""

    client = _make_client()
    summary = client._summarize_day(  # pylint: disable=protected-access
        {
            "d": "2025-11-18T05:00:00Z",
            "de": "2025-11-19T05:00:00Z",
            "v": [
                {"dc": 0, "t": 1, "v": 5.083, "u": 5},
                {"dc": 0, "t": 1, "v": 57.9398, "u": 3},
            ],
        }
    )

    assert summary is not None
    assert summary.metrics["consumption_high"] == pytest.approx(57.9398)
    assert summary.metrics["consumption_total"] == pytest.approx(57.9398)
    assert summary.metrics["injection_total"] == 0.0


def test_direction_mapping_handles_injection_low_tariff():
    """Verify that direction/tariff combinations map to the right metric."""

    client = _make_client()
    summary = client._summarize_day(  # pylint: disable=protected-access
        {
            "d": "2025-11-16T23:00:00Z",
            "de": "2025-11-17T23:00:00Z",
            "v": [
                {"dc": 1, "t": 2, "v": 4.5, "u": 3},
                {"dc": 2, "t": 2, "v": 1.25, "u": 3},
            ],
        }
    )

    assert summary is not None
    assert summary.metrics["consumption_low"] == pytest.approx(4.5)
    assert summary.metrics["injection_low"] == pytest.approx(1.25)
    assert summary.metrics["net_consumption"] == pytest.approx(4.5 - 1.25)


def test_gas_history_range_enforces_minimum(monkeypatch):
    """Gas meters should always request at least seven days of history."""

    client = _make_client(meter_type=METER_TYPE_GAS, options={CONF_DAYS_BACK: 1})
    client._resolve_timezone = lambda *_: timezone.utc  # type: ignore[assignment]

    fixed_now = datetime(2025, 11, 24, 6, 0, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz:
                return fixed_now.astimezone(tz)
            return fixed_now

    monkeypatch.setattr("custom_components.fluvius_energy.api.datetime", FixedDateTime)

    history_range = client._build_history_range()  # pylint: disable=protected-access
    start = datetime.fromisoformat(history_range["historyFrom"])
    end = datetime.fromisoformat(history_range["historyUntil"])

    assert (end - start) >= timedelta(days=GAS_MIN_LOOKBACK_DAYS)


def test_gas_requests_force_daily_granularity(monkeypatch):
    """Gas meters must always query the API using daily granularity."""

    client = _make_client(meter_type=METER_TYPE_GAS, options={CONF_GRANULARITY: "3"})

    async def fake_token(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "token", {}

    monkeypatch.setattr("custom_components.fluvius_energy.api.async_get_bearer_token", fake_token)
    client._build_history_range = lambda: {"historyFrom": "start", "historyUntil": "end"}  # type: ignore[assignment]

    captured: dict[str, str] = {}

    class DummyResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

        def raise_for_status(self):
            return None

        async def json(self):
            return [
                {
                    "d": "2025-11-17T05:00:00Z",
                    "de": "2025-11-18T05:00:00Z",
                    "v": [{"dc": 0, "t": 1, "v": 1, "u": 3}],
                }
            ]

    def fake_get(_url, *, params, headers, timeout):  # type: ignore[no-untyped-def]
        captured["granularity"] = params["granularity"]
        assert "Authorization" in headers
        assert timeout == 30
        return DummyResponse()

    monkeypatch.setattr(client._session, "get", fake_get)

    summaries = asyncio.run(client.fetch_daily_summaries())

    assert captured["granularity"] == GAS_SUPPORTED_GRANULARITY
    assert summaries[0].metrics["consumption_high"] == pytest.approx(1.0)
