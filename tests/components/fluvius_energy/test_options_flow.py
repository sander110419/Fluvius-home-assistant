"""Tests for the Fluvius Energy options flow."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

tests_common = pytest.importorskip("tests.common")
MockConfigEntry = tests_common.MockConfigEntry

from custom_components.fluvius.const import (
    CONF_EAN,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    DOMAIN,
    METER_TYPE_ELECTRICITY,
    CONF_GRANULARITY,
    DEFAULT_GRANULARITY
)

USER_INPUT = {
    CONF_EMAIL: "test@example.com",
    CONF_PASSWORD: "password",
    CONF_EAN: "541448800000000000",
    CONF_METER_SERIAL: "1SAGTEST",
    CONF_METER_TYPE: METER_TYPE_ELECTRICITY,
}

async def test_options_flow(hass):
    """Test options flow."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_GRANULARITY: "3",
            CONF_METER_TYPE: METER_TYPE_ELECTRICITY,
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GRANULARITY] == "3"
