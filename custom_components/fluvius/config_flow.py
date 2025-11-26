"""Config flow for the Fluvius Energy integration."""
from __future__ import annotations

from typing import Any, Dict, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import FluviusApiClient, FluviusApiError
from .const import (
    CONF_DAYS_BACK,
    CONF_EMAIL,
    CONF_EAN,
    CONF_GRANULARITY,
    CONF_METER_SERIAL,
    CONF_METER_TYPE,
    CONF_PASSWORD,
    CONF_TIMEZONE,
    DEFAULT_DAYS_BACK,
    DEFAULT_GRANULARITY,
    DEFAULT_METER_TYPE,
    DEFAULT_TIMEZONE,
    DOMAIN,
    METER_TYPE_ELECTRICITY,
    METER_TYPE_GAS,
    GAS_SUPPORTED_GRANULARITY,
)
from .http import async_create_fluvius_session

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
        vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Required(CONF_EAN): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
        vol.Required(CONF_METER_SERIAL): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
        vol.Required(
            CONF_METER_TYPE,
            default=DEFAULT_METER_TYPE,
        ): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=METER_TYPE_ELECTRICITY, label="Electricity meter"),
                    SelectOptionDict(value=METER_TYPE_GAS, label="Gas meter"),
                ],
                mode="dropdown",
            )
        ),
    }
)


class FluviusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for the integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        return await self._async_credentials_step("user", user_input)

    async def async_step_reauth(self, entry_data: Dict[str, Any]) -> FlowResult:
        _ = entry_data  # not used; included for signature compatibility
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        if self._reauth_entry is None:
            return self.async_abort(reason="unknown")
        return await self._async_credentials_step("reauth_confirm", user_input, reauth_entry=self._reauth_entry)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FluviusOptionsFlowHandler:
        return FluviusOptionsFlowHandler(config_entry)

    async def _async_credentials_step(
        self,
        step_id: str,
        user_input: Optional[Dict[str, Any]],
        reauth_entry: ConfigEntry | None = None,
    ) -> FlowResult:
        errors: Dict[str, str] = {}
        defaults: Dict[str, Any] = {}
        if reauth_entry:
            defaults = {
                CONF_EMAIL: reauth_entry.data[CONF_EMAIL],
                CONF_PASSWORD: reauth_entry.data[CONF_PASSWORD],
                CONF_EAN: reauth_entry.data[CONF_EAN],
                CONF_METER_SERIAL: reauth_entry.data[CONF_METER_SERIAL],
                CONF_METER_TYPE: reauth_entry.data.get(CONF_METER_TYPE, DEFAULT_METER_TYPE),
            }

        if user_input is not None:
            data = user_input if not reauth_entry else {**defaults, **user_input}
            try:
                await self._async_validate_input(self.hass, data)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pragma: no cover - defensive
                errors["base"] = "unknown"
            else:
                if reauth_entry:
                    self.hass.config_entries.async_update_entry(reauth_entry, data=data)
                    await self.hass.config_entries.async_reload(reauth_entry.entry_id)
                    return self.async_abort(reason="reauth_successful")

                await self.async_set_unique_id(data[CONF_EAN])
                self._abort_if_unique_id_configured()
                title = f"Fluvius {data[CONF_EAN]}"
                return self.async_create_entry(title=title, data=data)

        suggested = {**defaults}
        if user_input:
            suggested.update(user_input)
        schema = self.add_suggested_values_to_schema(DATA_SCHEMA, suggested)

        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def _async_validate_input(self, hass: HomeAssistant, data: Dict[str, Any]) -> None:
        session = async_create_fluvius_session(hass)
        client = FluviusApiClient(
            session=session,
            email=data[CONF_EMAIL],
            password=data[CONF_PASSWORD],
            ean=data[CONF_EAN],
            meter_serial=data[CONF_METER_SERIAL],
            meter_type=data.get(CONF_METER_TYPE, DEFAULT_METER_TYPE),
        )
        try:
            await client.fetch_daily_summaries()
        except FluviusApiError as err:
            message = str(err).lower()
            if any(key in message for key in ("auth", "password", "credentials")):
                raise InvalidAuth from err
            raise CannotConnect from err


class FluviusOptionsFlowHandler(config_entries.OptionsFlow):
    """Allow users to tweak history window or timezone after setup."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        current_meter_type = self._entry.data.get(CONF_METER_TYPE, DEFAULT_METER_TYPE)
        current_granularity = self._entry.options.get(CONF_GRANULARITY, DEFAULT_GRANULARITY)
        if current_meter_type == METER_TYPE_GAS:
            current_granularity = GAS_SUPPORTED_GRANULARITY

        if user_input is not None:
            new_meter_type = user_input.pop(CONF_METER_TYPE, current_meter_type)
            if new_meter_type == METER_TYPE_GAS:
                user_input[CONF_GRANULARITY] = GAS_SUPPORTED_GRANULARITY
            if new_meter_type != current_meter_type:
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data={**self._entry.data, CONF_METER_TYPE: new_meter_type},
                )
                self._entry = self.hass.config_entries.async_get_entry(self._entry.entry_id)
            return self.async_create_entry(data=user_input)

        granularity_options = [
            SelectOptionDict(value="3", label="Quarter-hour"),
            SelectOptionDict(value=DEFAULT_GRANULARITY, label="Daily"),
        ]
        if current_meter_type == METER_TYPE_GAS:
            granularity_options = [SelectOptionDict(value=GAS_SUPPORTED_GRANULARITY, label="Daily")]

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TIMEZONE,
                    default=self._entry.options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Required(
                    CONF_DAYS_BACK,
                    default=self._entry.options.get(CONF_DAYS_BACK, DEFAULT_DAYS_BACK),
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=31, mode="box"),
                ),
                vol.Required(
                    CONF_GRANULARITY,
                    default=current_granularity,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=granularity_options,
                        mode="dropdown",
                    )
                ),
                vol.Required(
                    CONF_METER_TYPE,
                    default=current_meter_type,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=METER_TYPE_ELECTRICITY, label="Electricity meter"),
                            SelectOptionDict(value=METER_TYPE_GAS, label="Gas meter"),
                        ],
                        mode="dropdown",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate the credentials are invalid."""
