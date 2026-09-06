"""Config, options, and favorite-subentry flows for Metra."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
)

from . import gtfs
from .const import DOMAIN

LINE_IDS = ["BNSF", "HC", "MD-N", "MD-W", "ME", "NCS", "RI", "SWS", "UP-N", "UP-NW", "UP-W"]


def _line_ids(hass) -> list[str]:
    for coord in hass.data.get(DOMAIN, {}).values():
        if getattr(coord, "idx", None):
            return [r["id"] for r in coord.idx["routes"]]
    return LINE_IDS


class MetraConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input["api_token"].strip()

            def _check() -> None:
                gtfs._fetch(f"{gtfs.RT_BASE}/tripupdates?api_token={token}", 20)

            try:
                await self.hass.async_add_executor_job(_check)
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Metra", data={"api_token": token})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("api_token"): TextSelector()}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MetraOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {"favorite": FavoriteSubentryFlow}


class MetraOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        ids = _line_ids(self.hass)
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("lines", default=opts.get("lines", ids)): SelectSelector(
                    SelectSelectorConfig(options=ids, multiple=True)),
                vol.Required("map_lines", default=opts.get("map_lines", ids)): SelectSelector(
                    SelectSelectorConfig(options=ids, multiple=True)),
                vol.Required("map_slots", default=opts.get("map_slots", 12)): NumberSelector(
                    NumberSelectorConfig(min=1, max=12, step=1, mode="box")),
            }),
        )


class FavoriteSubentryFlow(ConfigSubentryFlow):
    """Add a commute pair: 6 next/upcoming/en-route sensors on the line device."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            return self.async_create_entry(
                title=f"{user_input['line']} {user_input['origin']} → {user_input['destination']}",
                data=user_input,
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("line", default="UP-W"): SelectSelector(
                    SelectSelectorConfig(options=_line_ids(self.hass))),
                vol.Required("origin"): TextSelector(),
                vol.Required("destination"): TextSelector(),
            }),
            errors=errors,
        )
