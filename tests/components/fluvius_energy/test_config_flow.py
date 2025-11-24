"""Tests for the Fluvius Energy config flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
)

USER_INPUT = {
    CONF_EMAIL: "test@example.com",
    CONF_PASSWORD: "hunter2",
    CONF_EAN: "541448800000000000",
    CONF_METER_SERIAL: "1SAGTEST",
    CONF_METER_TYPE: METER_TYPE_ELECTRICITY,
}


async def test_user_flow_success(hass):
    """Test the happy path of the config flow."""

    with patch(
        "custom_components.fluvius.config_flow.async_create_fluvius_session",
        return_value=MagicMock(),
    ), patch(
        "custom_components.fluvius.config_flow.FluviusApiClient",
        autospec=True,
    ) as mock_client:
        instance = mock_client.return_value
        instance.fetch_daily_summaries = AsyncMock(return_value=[])

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data=USER_INPUT,
        )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Fluvius {USER_INPUT[CONF_EAN]}"
    assert result["data"] == USER_INPUT


async def test_user_flow_invalid_auth(hass):
    """Ensure invalid credentials bubble up as form errors."""

    with patch(
        "custom_components.fluvius.config_flow.async_create_fluvius_session",
        return_value=MagicMock(),
    ), patch(
        "custom_components.fluvius.config_flow.FluviusApiClient",
        autospec=True,
    ) as mock_client:
        instance = mock_client.return_value
        instance.fetch_daily_summaries = AsyncMock(side_effect=Exception("auth failure"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data=USER_INPUT,
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_reauth_updates_entry(hass):
    """Test the reauthentication path updates stored credentials."""

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.fluvius.config_flow.async_create_fluvius_session",
        return_value=MagicMock(),
    ), patch(
        "custom_components.fluvius.config_flow.FluviusApiClient",
        autospec=True,
    ) as mock_client:
        instance = mock_client.return_value
        instance.fetch_daily_summaries = AsyncMock(return_value=[])

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=USER_INPUT,
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"