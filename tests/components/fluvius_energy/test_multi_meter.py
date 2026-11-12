"""Meters sharing one EAN stay separate; single-meter setups keep their IDs (#30)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fluvius.api import FluviusApiClient, FluviusDailySummary
from custom_components.fluvius.const import (
    CONF_EAN,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    DOMAIN,
    METER_TYPE_ELECTRICITY,
)
from custom_components.fluvius.statistics import FluviusStatistics, meter_key, statistic_prefix

pytestmark = pytest.mark.usefixtures("recorder_mock")

EAN = "541448800000000000"
FIRST_SERIAL = "1SAGTEST"
SECOND_SERIAL = "1SAG-Second"
SECOND_KEY = f"{EAN}_1sag_second"
START = datetime(2025, 11, 24, tzinfo=UTC)


def meter_entry(serial: str = FIRST_SERIAL, unique_id: str | None = EAN) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_EAN: EAN,
            CONF_METER_SERIAL: serial,
            CONF_METER_TYPE: METER_TYPE_ELECTRICITY,
        },
    )


def summary(consumption: float, injection: float = 0.0) -> FluviusDailySummary:
    return FluviusDailySummary(
        START.isoformat(),
        START,
        START + timedelta(days=1),
        {"consumption_high": consumption, "injection_high": injection},
    )


@pytest.mark.parametrize("unique_id", [None, EAN, "541448800000000000x", "legacy"])
def test_single_meter_keeps_its_statistic_ids(unique_id):
    """Existing entries must resolve to exactly the IDs they used before."""
    entry = meter_entry(unique_id=unique_id)
    assert meter_key(entry) == EAN
    assert (
        statistic_prefix(meter_key(entry), "electricity", "kwh")
        == "541448800000000000_electricity_kwh"
    )


def test_additional_meter_is_keyed_on_ean_and_serial():
    entry = meter_entry(SECOND_SERIAL, unique_id=SECOND_KEY)
    assert meter_key(entry) == SECOND_KEY
    assert (
        statistic_prefix(meter_key(entry), "electricity", "kwh")
        == "541448800000000000_1sag_second_electricity_kwh"
    )


async def test_two_meters_on_one_ean_stay_separate(hass, recorder_mock):
    """Each meter gets its own device, sensors, storage and statistics."""
    from homeassistant.components.recorder.statistics import statistics_during_period
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_recorder_block_till_done,
    )

    first = meter_entry()
    second = meter_entry(SECOND_SERIAL, unique_id=SECOND_KEY)
    readings = {FIRST_SERIAL: 15.0, SECOND_SERIAL: 3.0}
    sharing_requests: list[str] = []

    async def fetch_summaries(self):
        return [summary(readings[self._meter_serial])], []

    async def fetch_sharing(self, history_for=None):
        sharing_requests.append(self._meter_serial)
        return []

    with (
        patch("custom_components.fluvius.async_create_fluvius_session", return_value=MagicMock()),
        patch.object(FluviusApiClient, "fetch_daily_summaries_with_spikes", fetch_summaries),
        patch.object(FluviusApiClient, "fetch_energy_sharing", fetch_sharing),
    ):
        for entry in (first, second):
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    expected = {first: (EAN, 15.0), second: (SECOND_KEY, 3.0)}
    statistic_ids = {}
    for entry, (key, consumption) in expected.items():
        device = devices.async_get_device(identifiers={(DOMAIN, key)})
        assert device is not None
        assert device.config_entries == {entry.entry_id}
        assert device.name == f"Fluvius meter {entry.data[CONF_METER_SERIAL]}"
        assert len(er.async_entries_for_config_entry(entities, entry.entry_id)) == 10

        entity_id = entities.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_consumption_total"
        )
        state = hass.states.get(entity_id)
        assert float(state.state) == pytest.approx(consumption)
        statistic_ids[entry] = state.attributes["historical_statistic_id"]

    assert statistic_ids[first] == f"fluvius:{EAN}_electricity_kwh_consumption_total"
    assert statistic_ids[second] == f"fluvius:{SECOND_KEY}_electricity_kwh_consumption_total"
    assert first.runtime_data.store._store.key != second.runtime_data.store._store.key

    await async_recorder_block_till_done(hass)
    rows = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        START - timedelta(hours=1),
        None,
        set(statistic_ids.values()),
        "hour",
        None,
        {"sum"},
    )
    assert rows[statistic_ids[first]][-1]["sum"] == pytest.approx(15.0)
    assert rows[statistic_ids[second]][-1]["sum"] == pytest.approx(3.0)

    # Energiedelen is registered per EAN: only the entry owning the bare EAN asks.
    assert sharing_requests == [FIRST_SERIAL]


@pytest.mark.parametrize(
    ("prefix", "has_injection"),
    [
        # A serial may spell "gas" without the meter being a gas meter.
        (statistic_prefix(f"{EAN}_1gas99", "electricity", "kwh"), True),
        (statistic_prefix(f"{EAN}_1gas99", "electricity", "kwh", "2026-01-01"), True),
        (statistic_prefix(EAN, "gas", "m3"), False),
        (statistic_prefix(EAN, "gas", "kwh", "2026-01-01"), False),
        (statistic_prefix(f"{EAN}_1sag_second", "gas", "m3"), False),
    ],
)
async def test_injection_follows_meter_type_not_serial(hass, prefix, has_injection):
    obj = FluviusStatistics(hass, prefix, "Test", "kWh")
    obj._store = MagicMock(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
    with patch("custom_components.fluvius.statistics.async_add_external_statistics") as add:
        await obj.async_update([summary(1.0, injection=2.0)], [])
    imported = {call.args[1]["statistic_id"] for call in add.call_args_list}
    assert f"fluvius:{prefix}_consumption_total" in imported
    assert (f"fluvius:{prefix}_injection_total" in imported) is has_injection
