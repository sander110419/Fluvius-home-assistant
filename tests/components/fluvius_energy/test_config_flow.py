"""Tests for the Fluvius Energy config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fluvius.api import FluviusAuthenticationError
from custom_components.fluvius.const import (
    CONF_EAN,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    DOMAIN,
    METER_TYPE_ELECTRICITY,
)

pytestmark = pytest.mark.usefixtures("recorder_mock")

USER_INPUT = {
    CONF_EMAIL: "test@example.com",
    CONF_PASSWORD: "hunter2",
    CONF_EAN: "541448800000000000",
    CONF_METER_SERIAL: "1SAGTEST",
    CONF_METER_TYPE: METER_TYPE_ELECTRICITY,
}


async def test_user_flow_success(hass):
    """Test the happy path of the config flow."""

    with (
        patch(
            "custom_components.fluvius.config_flow.async_create_fluvius_session",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.fluvius.config_flow.FluviusApiClient",
            autospec=True,
        ) as mock_client,
    ):
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

    with (
        patch(
            "custom_components.fluvius.config_flow.async_create_fluvius_session",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.fluvius.config_flow.FluviusApiClient",
            autospec=True,
        ) as mock_client,
    ):
        instance = mock_client.return_value
        instance.fetch_daily_summaries = AsyncMock(
            side_effect=FluviusAuthenticationError("auth failure")
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data=USER_INPUT,
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def _submit_user_flow(hass, user_input):
    with (
        patch(
            "custom_components.fluvius.config_flow.async_create_fluvius_session",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.fluvius.config_flow.FluviusApiClient",
            autospec=True,
        ) as mock_client,
        patch("custom_components.fluvius.async_setup_entry", AsyncMock(return_value=True)),
    ):
        mock_client.return_value.fetch_daily_summaries = AsyncMock(return_value=[])
        return await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data=user_input,
        )


async def test_first_meter_owns_the_bare_ean(hass):
    """A single meter keeps the EAN as unique ID, like every existing entry."""

    result = await _submit_user_flow(hass, USER_INPUT)

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == USER_INPUT[CONF_EAN]


async def test_second_meter_on_same_ean_gets_its_own_entry(hass):
    """Two meters behind one EAN are told apart by their serial number."""

    MockConfigEntry(domain=DOMAIN, data=USER_INPUT, unique_id=USER_INPUT[CONF_EAN]).add_to_hass(
        hass
    )

    result = await _submit_user_flow(hass, {**USER_INPUT, CONF_METER_SERIAL: "1SAG-Second"})

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Fluvius {USER_INPUT[CONF_EAN]} (1SAG-Second)"
    assert result["result"].unique_id == f"{USER_INPUT[CONF_EAN]}_1sag_second"
    assert result["data"][CONF_METER_SERIAL] == "1SAG-Second"


@pytest.mark.parametrize("unique_id", [USER_INPUT[CONF_EAN], None])
async def test_second_meter_never_claims_an_owned_ean(hass, unique_id):
    """An entry without unique ID still owns the EAN-only statistics."""

    MockConfigEntry(domain=DOMAIN, data=USER_INPUT, unique_id=unique_id).add_to_hass(hass)

    result = await _submit_user_flow(hass, {**USER_INPUT, CONF_METER_SERIAL: "OTHER"})

    assert result["result"].unique_id == f"{USER_INPUT[CONF_EAN]}_other"


@pytest.mark.parametrize("serial", ["1SAGTEST", "1sagtest", " 1SAGTEST "])
async def test_same_meter_cannot_be_added_twice(hass, serial):
    MockConfigEntry(domain=DOMAIN, data=USER_INPUT, unique_id=USER_INPUT[CONF_EAN]).add_to_hass(
        hass
    )

    result = await _submit_user_flow(hass, {**USER_INPUT, CONF_METER_SERIAL: serial})

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_additional_meter_cannot_be_added_twice(hass):
    ean = USER_INPUT[CONF_EAN]
    MockConfigEntry(domain=DOMAIN, data=USER_INPUT, unique_id=ean).add_to_hass(hass)
    MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, CONF_METER_SERIAL: "1SAG-Second"},
        unique_id=f"{ean}_1sag_second",
    ).add_to_hass(hass)

    result = await _submit_user_flow(hass, {**USER_INPUT, CONF_METER_SERIAL: "1SAG-Second"})

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_same_serial_on_another_ean_is_a_different_meter(hass):
    MockConfigEntry(domain=DOMAIN, data=USER_INPUT, unique_id=USER_INPUT[CONF_EAN]).add_to_hass(
        hass
    )
    other_ean = "541448800000000001"

    result = await _submit_user_flow(hass, {**USER_INPUT, CONF_EAN: other_ean})

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == other_ean


async def test_reauth_updates_entry(hass):
    """Test the reauthentication path updates stored credentials."""

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.fluvius.config_flow.async_create_fluvius_session",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.fluvius.config_flow.FluviusApiClient",
            autospec=True,
        ) as mock_client,
    ):
        instance = mock_client.return_value
        instance.fetch_daily_summaries = AsyncMock(return_value=[])

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=USER_INPUT,
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    with (
        patch(
            "custom_components.fluvius.config_flow.FluviusApiClient.fetch_daily_summaries",
            AsyncMock(return_value=[]),
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload", AsyncMock(return_value=True)
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=USER_INPUT
        )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
