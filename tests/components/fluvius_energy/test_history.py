"""Regression tests for delayed, corrected and overlapping consumption history."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.fluvius.api import (
    FluviusApiClient,
    FluviusDailySummary,
    FluviusQuarterHourlyMeasurement,
)
from custom_components.fluvius.const import CONF_DAYS_BACK, CONF_GRANULARITY, CONF_HISTORY_UNTIL
from custom_components.fluvius.statistics import FluviusStatistics, statistic_prefix
from custom_components.fluvius.store import FluviusEnergyStore

UTC = UTC


def client(**options):
    return FluviusApiClient(
        session=MagicMock(),
        email="test@example.com",
        password="test",
        ean="541448800000000000",
        meter_serial="TEST",
        options=options,
    )


def day(start, value, hours=24):
    return FluviusDailySummary(
        start.isoformat(), start, start + timedelta(hours=hours), {"consumption_high": value}
    )


def intervals(start, count, value=1):
    return [
        FluviusQuarterHourlyMeasurement(
            start + timedelta(minutes=i * 15),
            start + timedelta(minutes=(i + 1) * 15),
            value,
            0,
            {"consumption_high": value},
        )
        for i in range(count)
    ]


def history(hass):
    obj = FluviusStatistics(hass, "test_electricity_kwh", "Test", "kWh")
    obj._store = MagicMock(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
    return obj


@pytest.mark.parametrize(
    ("local_date", "count"), [("2026-03-29", 92), ("2026-10-25", 100), ("2026-04-01", 96)]
)
async def test_dst_and_actual_usage_date(hass, local_date, count):
    local = datetime.fromisoformat(local_date).replace(tzinfo=ZoneInfo("Europe/Brussels"))
    start = local.astimezone(UTC)
    obj = history(hass)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add:
        await obj.async_update([day(start, count, count // 4)], intervals(start, count))
    hourly = obj._hours()
    assert len(hourly) == count // 4
    assert sum(row["consumption_total"] for row in hourly.values()) == count
    assert {t.astimezone(local.tzinfo).date() for t in hourly} == {local.date()}
    rows = add.call_args_list[0].args[2]
    assert rows[0]["sum"] == 0
    assert rows[-1]["sum"] == count
    assert all(t["start"].minute == 0 for t in rows)


async def test_corrections_reimports_and_restart(hass):
    start = datetime(2026, 4, 1, tzinfo=UTC)
    obj = history(hass)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add:
        await obj.async_update([day(start, 10), day(start + timedelta(days=1), 20)], [])
        assert add.call_args_list[0].args[2][-1]["sum"] == 30
        add.reset_mock()
        await obj.async_update([day(start, 10)], [])
        add.assert_not_called()
        await obj.async_update([day(start, 7)], [])
        assert add.call_args_list[0].args[2][-1]["sum"] == 27
        recreated = history(hass)
        recreated._store.async_load.return_value = obj._data
        await recreated.async_load()
        add.reset_mock()
        await recreated.async_update([], [])
        assert add.call_args_list[0].args[2][-1]["sum"] == 27


async def test_detail_replaces_daily_without_double_counting(hass):
    start = datetime(2026, 4, 1, tzinfo=UTC)
    obj = history(hass)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics"):
        await obj.async_update([day(start, 96)], [])
        await obj.async_update([], intervals(start, 95))
        assert obj._hours()[start]["consumption_total"] == 96  # incomplete: retain daily total
        await obj.async_update([], intervals(start, 96))
        assert obj._hours()[start]["consumption_total"] == 4
        assert sum(v["consumption_total"] for v in obj._hours().values()) == 96
        await obj.async_update([], intervals(start, 1, value=0.5))
        assert sum(v["consumption_total"] for v in obj._hours().values()) == 95.5


async def test_store_downward_correction_and_long_lookback(hass):
    store = FluviusEnergyStore(hass, "test", "kwh")
    store._store = MagicMock(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
    for i in range(90):
        await store.async_process_summary(
            (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)).isoformat(),
            {"consumption_high": 1},
        )
    first = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    await store.async_process_summary(first, {"consumption_high": 1})
    assert store.get_lifetime_totals()["consumption_total"] == 90
    await store.async_process_summary(first, {"consumption_high": 0.5})
    assert store.get_lifetime_totals()["consumption_total"] == 89.5
    assert store.get_last_day_id() != first


async def test_fetches_entire_window_and_empty_recent_days():
    obj = client(**{CONF_DAYS_BACK: 7, CONF_GRANULARITY: "3"})
    obj._async_get_access_token = AsyncMock(return_value="test-token")
    obj._fetch_raw_quarter_hourly = AsyncMock(return_value=[])
    await obj.fetch_quarter_hourly_consumption()
    # Every day is still requested, oldest first; an unresolved granularity means each
    # one is asked for once per candidate code.
    offsets = [call.args[1] for call in obj._fetch_raw_quarter_hourly.call_args_list]
    assert sorted(set(offsets), reverse=True) == list(range(7, 0, -1))
    assert offsets == sorted(offsets, reverse=True)
    obj._async_get_access_token.assert_awaited_once()


async def test_empty_window_disables_further_probing():
    """A window that never answers latches the fetch off instead of probing hourly."""

    obj = client(**{CONF_DAYS_BACK: 2})
    obj._async_get_access_token = AsyncMock(return_value="test-token")
    obj._fetch_raw_quarter_hourly = AsyncMock(return_value=[])

    assert await obj.fetch_quarter_hourly_consumption() == []
    assert obj.interval_unavailable is True
    assert obj.resolved_granularity is None
    assert obj.probe_outcomes == dict.fromkeys(("1", "2", "3"), "no data")

    obj._fetch_raw_quarter_hourly.reset_mock()
    assert await obj.fetch_quarter_hourly_consumption() == []
    obj._fetch_raw_quarter_hourly.assert_not_awaited()


async def test_failing_probe_requests_do_not_disable_the_fetch():
    """A rejected request says nothing about the meter, so keep retrying."""

    from custom_components.fluvius.api import FluviusApiError

    obj = client(**{CONF_DAYS_BACK: 2})
    obj._async_get_access_token = AsyncMock(return_value="test-token")
    obj._fetch_raw_quarter_hourly = AsyncMock(side_effect=FluviusApiError("boom"))

    assert await obj.fetch_quarter_hourly_consumption() == []
    assert obj.interval_unavailable is False
    assert all("failed" in outcome for outcome in obj.probe_outcomes.values())


def test_cutoff_excludes_overlap_and_has_separate_statistics():
    cutoff = "2026-04-01T12:30:00+02:00"
    obj = client(**{CONF_HISTORY_UNTIL: cutoff})
    assert obj.history_end == datetime.fromisoformat(cutoff)
    assert (
        obj._summaries_from_payload(
            [
                {
                    "d": "2026-03-31T22:00:00Z",
                    "de": "2026-04-01T22:00:00Z",
                    "v": [{"dc": 1, "t": 1, "u": 3, "v": 10}],
                }
            ]
        )
        == []
    )
    assert statistic_prefix("ean", "electricity", "kwh") != statistic_prefix(
        "ean", "electricity", "kwh", cutoff
    )
    assert statistic_prefix("ean", "gas", "kwh") != statistic_prefix("ean", "gas", "m3")


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", -1, "invalid"])
def test_missing_invalid_readings_do_not_become_zero(value):
    obj = client()
    assert (
        obj._summarize_day(
            {"d": "2026-04-01T00:00:00Z", "v": [{"dc": 1, "t": 1, "u": 3, "v": value}]}
        )
        is None
    )


async def test_real_recorder_import_is_idempotent(hass, recorder_mock):
    from homeassistant.components.recorder.statistics import statistics_during_period
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_recorder_block_till_done,
    )

    start = datetime(2026, 4, 1, tzinfo=UTC)
    obj = history(hass)
    await obj.async_update([], intervals(start, 8))
    await async_recorder_block_till_done(hass)
    statistic_id = "fluvius:test_electricity_kwh_consumption_total"
    rows = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(hours=1),
        start + timedelta(hours=3),
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    assert [v["sum"] for v in rows[statistic_id]] == [0, 4, 8]
    await obj.async_update([], intervals(start, 1, 0.5))
    await async_recorder_block_till_done(hass)
    rows = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(hours=1),
        start + timedelta(hours=3),
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    assert [v["sum"] for v in rows[statistic_id]] == [0, 3.5, 7.5]


async def test_cutoff_filters_intervals_and_deduplicates():
    obj = client(**{CONF_HISTORY_UNTIL: "2026-04-01T12:30:00+02:00"})
    obj._async_get_access_token = AsyncMock(return_value="token")
    payload = [
        {
            "d": "2026-04-01T10:15:00Z",
            "de": "2026-04-01T10:30:00Z",
            "v": [{"dc": 1, "t": 1, "u": 3, "v": 1}],
        },
        {
            "d": "2026-04-01T10:30:00Z",
            "de": "2026-04-01T10:45:00Z",
            "v": [{"dc": 1, "t": 1, "u": 3, "v": 2}],
        },
    ]
    obj._fetch_raw_quarter_hourly = AsyncMock(return_value=payload)
    result = await obj.fetch_quarter_hourly_consumption(days_back=2)
    assert len(result) == 1
    assert result[0].consumption == 1
    assert result[0].end == obj.history_end


@pytest.mark.parametrize(("meter_type", "granularity"), [("electricity", "1"), ("gas", "2")])
async def test_interval_probe_tries_native_granularity_first(meter_type, granularity):
    obj = client()
    obj._meter_type = meter_type
    obj._request_history = AsyncMock(return_value=[])
    await obj._async_probe_granularity("token", 1)
    assert obj._request_history.await_args_list[0].args[1]["granularity"] == granularity


async def test_interval_probe_prefers_the_configured_granularity():
    """The option stays meaningful: its code is tried before the defaults."""

    obj = client(**{CONF_GRANULARITY: "7"})
    obj._request_history = AsyncMock(return_value=[])
    await obj._async_probe_granularity("token", 1)
    codes = [call.args[1]["granularity"] for call in obj._request_history.await_args_list]
    assert codes == ["7", "1", "3", "2"]


async def test_interval_probe_settles_on_the_code_that_answers():
    """A code returning the expected length is remembered and reused."""

    obj = client()
    quarters = [
        {
            "d": f"2026-04-01T10:{minute:02d}:00Z",
            "de": f"2026-04-01T10:{minute + 15:02d}:00Z",
            "v": [{"dc": 1, "t": 1, "u": 3, "v": 1}],
        }
        for minute in (0, 15, 30)
    ]
    obj._request_history = AsyncMock(side_effect=[[], quarters])
    obj._meter_type = "electricity"
    obj._options[CONF_GRANULARITY] = "9"

    measurements = await obj._async_probe_granularity("token", 1)

    assert len(measurements) == 3
    assert obj.resolved_granularity == "1"
    assert obj.probe_outcomes == {"9": "no data", "1": "15-minute intervals"}


async def test_interval_probe_falls_back_to_a_coarser_resolution():
    """No code serves quarters, so the hourly one is used rather than nothing."""

    obj = client()
    hours = [
        {
            "d": f"2026-04-01T{hour:02d}:00:00Z",
            "de": f"2026-04-01T{hour + 1:02d}:00:00Z",
            "v": [{"dc": 1, "t": 1, "u": 3, "v": 1}],
        }
        for hour in (10, 11)
    ]
    obj._request_history = AsyncMock(side_effect=[hours, [], []])

    measurements = await obj._async_probe_granularity("token", 1)

    assert obj.resolved_granularity == "1"
    assert obj._resolved_interval_minutes == 60
    # Kept at the resolution actually served, instead of being filtered out.
    assert len(measurements) == 2


async def test_access_token_is_reused():
    obj = client()
    with patch(
        "custom_components.fluvius.api.async_get_bearer_token",
        AsyncMock(return_value=("token", {"expires_in": 3600})),
    ) as auth:
        assert await obj._async_get_access_token() == "token"
        assert await obj._async_get_access_token() == "token"
        auth.assert_awaited_once()


async def test_timeout_is_retryable():
    from custom_components.fluvius.api import FluviusApiError

    obj = client()
    obj._session.get.side_effect = TimeoutError
    with pytest.raises(FluviusApiError):
        await obj._fetch_raw_consumption("token")


async def test_empty_refresh_preserves_data_and_auth_triggers_reauth(hass):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    from custom_components.fluvius.api import FluviusAuthenticationError
    from custom_components.fluvius.coordinator import FluviusEnergyDataUpdateCoordinator

    obj = client(**{CONF_GRANULARITY: "1"})
    start = datetime(2026, 4, 1, tzinfo=UTC)
    obj.fetch_daily_summaries_with_spikes = AsyncMock(return_value=([], []))
    obj.fetch_quarter_hourly_consumption = AsyncMock(return_value=intervals(start, 4))
    store = MagicMock(get_lifetime_totals=lambda: {})
    stats = history(hass)
    coordinator = FluviusEnergyDataUpdateCoordinator(hass, obj, store, stats)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics"):
        coordinator.data = await coordinator._async_update_data()
        assert coordinator.data.lifetime_totals["consumption_total"] == 4
        obj.fetch_quarter_hourly_consumption.return_value = []
        second = await coordinator._async_update_data()
        assert second.quarter_hourly_measurements == coordinator.data.quarter_hourly_measurements
        assert second.latest_summary is not None
        obj.fetch_daily_summaries_with_spikes.side_effect = FluviusAuthenticationError(
            "credentials"
        )
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()


async def test_real_recorder_gas_volume(hass, recorder_mock):
    from homeassistant.components.recorder.statistics import statistics_during_period
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_recorder_block_till_done,
    )

    obj = FluviusStatistics(hass, "test_gas_m3", "Gas", "m3")
    obj._store = MagicMock(async_save=AsyncMock())
    start = datetime(2026, 4, 1, tzinfo=UTC)
    await obj.async_update([day(start, 3)], [])
    await async_recorder_block_till_done(hass)
    key = "fluvius:test_gas_m3_consumption_total"
    rows = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(hours=1),
        start + timedelta(days=1),
        {key},
        "hour",
        None,
        {"sum"},
    )
    assert rows[key][-1]["sum"] == 3


async def test_older_noncontiguous_backfill_overwrites_previous_baseline(hass):
    start = datetime(2026, 4, 3, tzinfo=UTC)
    obj = history(hass)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add:
        await obj.async_update([], intervals(start, 4))
        add.reset_mock()
        await obj.async_update([], intervals(start - timedelta(days=2), 4))
    rows = {r["start"]: r["sum"] for r in add.call_args_list[0].args[2]}
    assert rows[start - timedelta(hours=1)] == 4
    assert rows[start] == 8


async def test_gas_statistics_never_carry_an_injection_series(hass):
    """A gas meter cannot inject, so no injection statistic is published for it."""

    obj = FluviusStatistics(hass, "test_gas_m3", "Test", "m3")
    obj._store = MagicMock(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
    start = datetime(2026, 4, 1, tzinfo=UTC)
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add:
        await obj.async_update([day(start, 3)], [])
    written = {call.args[1]["statistic_id"] for call in add.call_args_list}
    assert written
    assert not [sid for sid in written if "injection" in sid]


async def test_gas_day_readings_are_not_collapsed_into_the_06h_bucket(hass):
    """The reported symptom: a whole gas day showing as one spike at 06:00 local.

    Gas summaries span the gas day (05:00Z -> 05:00Z in winter). _hours() only lets
    interval readings replace a daily total when they tile that period exactly, so a
    calendar-day window leaves the day lumped in its first bucket.
    """

    gas_day = datetime(2025, 11, 18, 5, tzinfo=UTC)
    summary = FluviusDailySummary(
        gas_day.isoformat(), gas_day, gas_day + timedelta(days=1), {"consumption_high": 24.0}
    )

    def hourly(first):
        return [
            FluviusQuarterHourlyMeasurement(
                first + timedelta(hours=i),
                first + timedelta(hours=i + 1),
                1.0,
                0,
                {"consumption_high": 1.0},
            )
            for i in range(24)
        ]

    with patch("custom_components.fluvius.statistics.async_add_external_statistics"):
        # Calendar-day window: misaligned, so the fallback fires.
        misaligned = history(hass)
        await misaligned.async_update([summary], hourly(datetime(2025, 11, 17, 23, tzinfo=UTC)))
        assert misaligned._hours()[gas_day]["consumption_total"] == 24.0

        # Gas-day window: the readings tile the summary and land on their own hours.
        aligned = history(hass)
        await aligned.async_update([summary], hourly(gas_day))
    spread = aligned._hours()
    assert spread[gas_day]["consumption_total"] == 1.0
    assert spread[gas_day + timedelta(hours=12)]["consumption_total"] == 1.0
    assert sum(row["consumption_total"] for row in spread.values()) == 24.0


def test_gas_interval_window_aligns_on_the_gas_day():
    """A gas request must span the gas day, or its readings cannot tile the summary."""

    obj = client(**{CONF_HISTORY_UNTIL: "2026-04-08T00:00:00+02:00"})
    obj._meter_type = "gas"

    first = obj._build_quarter_hourly_range(1)
    assert first["historyFrom"] == "2026-04-06T06:00:00.000+02:00"
    assert first["historyUntil"] == "2026-04-07T05:59:59.999+02:00"

    # The cutoff falls inside the 07 -> 08 gas day, so that day is not requested:
    # a window truncated mid-gas-day could not tile the summary either.
    third = obj._build_quarter_hourly_range(3)
    assert third["historyFrom"] == "2026-04-04T06:00:00.000+02:00"
    assert third["historyUntil"] == "2026-04-05T05:59:59.999+02:00"


def test_electricity_interval_window_still_uses_calendar_days():
    """The gas alignment must not shift the electricity window."""

    obj = client(**{CONF_HISTORY_UNTIL: "2026-04-08T00:00:00+02:00"})
    day_range = obj._build_quarter_hourly_range(1)
    assert day_range["historyFrom"] == "2026-04-07T00:00:00.000+02:00"
    assert day_range["historyUntil"] == "2026-04-07T23:59:59.999+02:00"


def test_cutoff_lookback_uses_local_dst_offsets():
    obj = client(**{CONF_HISTORY_UNTIL: "2026-03-30T00:00:00+02:00"})
    date_range = obj._build_quarter_hourly_range(2)
    assert date_range["historyFrom"] == "2026-03-28T00:00:00.000+01:00"
    assert date_range["historyUntil"] == "2026-03-28T23:59:59.999+01:00"
    dst_day = obj._build_quarter_hourly_range(1)
    start = datetime.fromisoformat(dst_day["historyFrom"])
    end = datetime.fromisoformat(dst_day["historyUntil"]) + timedelta(milliseconds=1)
    assert end - start == timedelta(hours=23)
