"""Metra integration: coordinator + native response services.

Replaces the metra_mqtt.py MQTT-discovery publisher (2026-09-06). Entity ids
are kept identical to the publisher era so dashboards/automations survive.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import gtfs
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]
UPDATE_INTERVAL = timedelta(minutes=2)


class MetraCoordinator(DataUpdateCoordinator):
    """Fetches realtime + computes per-line data every 2 minutes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name="metra", update_interval=UPDATE_INTERVAL)
        self.entry = entry
        self.token: str = entry.data["api_token"]
        self.idx: dict | None = None
        self._span_cache: dict = {}

    def _lines(self) -> list[str]:
        opts = self.entry.options.get("lines")
        all_ids = [r["id"] for r in self.idx["routes"]]
        return [l for l in (opts or all_ids) if l in all_ids]

    def _map_lines(self) -> list[str]:
        opts = self.entry.options.get("map_lines")
        return [l for l in (opts or self._lines()) if l in self._lines()]

    @property
    def map_slots(self) -> int:
        return int(self.entry.options.get("map_slots", 12))

    def _compute(self) -> dict:
        self.idx = gtfs.load_index()
        rt, pos = gtfs.realtime(self.token)
        now = datetime.now(gtfs.TZ)
        today = now.date()
        data: dict = {
            "routes": self.idx["routes"],
            "lines": self._lines(),
            "map_lines": self._map_lines(),
            "updated": now.strftime("%H:%M"),
            "active": {}, "schedule": {}, "favorites": {},
        }
        span_key = (today.isoformat(), self.idx["version"], tuple(data["lines"]))
        if self._span_cache.get("key") != span_key:
            self._span_cache = {"key": span_key, "lines": {
                line: gtfs.schedule_span(self.idx, line, today) for line in data["lines"]}}
        for line in data["lines"]:
            data["active"][line] = gtfs.active_trains(
                self.idx, line, rt.get(line, {}), pos.get(line, {}))
            data["schedule"][line] = gtfs.schedule_day(self.idx, line, today)
            days_list, patterns = self._span_cache["lines"][line]
            data["schedule"][line]["days"] = days_list
            data["schedule"][line]["patterns"] = patterns
        for sub in self.entry.subentries.values():
            if sub.subentry_type != "favorite":
                continue
            line = sub.data["line"]
            if line not in data["lines"]:
                continue
            try:
                origin = gtfs.resolve_stop(self.idx, line, sub.data["origin"])
                dest = gtfs.resolve_stop(self.idx, line, sub.data["destination"])
            except ValueError as err:
                _LOGGER.warning("favorite %s skipped: %s", sub.title, err)
                continue
            rt_l, pos_l = rt.get(line, {}), pos.get(line, {})
            data["favorites"][sub.subentry_id] = {
                "line": line,
                "up_in": gtfs.upcoming(self.idx, line, origin, dest, rt_l, now),
                "up_out": gtfs.upcoming(self.idx, line, dest, origin, rt_l, now),
                "en_in": gtfs.enroute(self.idx, line, origin, dest, rt_l, pos_l),
                "en_out": gtfs.enroute(self.idx, line, dest, origin, rt_l, pos_l),
            }
        return data

    async def _async_update_data(self) -> dict:
        try:
            return await self.hass.async_add_executor_job(self._compute)
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(str(err)) from err


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = MetraCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload_on_change))
    _register_services(hass, coordinator)
    return True


async def _reload_on_change(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device) -> bool:
    """Allow deleting stale devices from the UI/registry."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok


def _register_services(hass: HomeAssistant, coordinator: MetraCoordinator) -> None:
    if hass.services.has_service(DOMAIN, "schedule"):
        return

    def _date(call: ServiceCall) -> date:
        raw = call.data.get("date")
        return date.fromisoformat(str(raw)) if raw else datetime.now(gtfs.TZ).date()

    async def _run(fn, *args):
        try:
            return await hass.async_add_executor_job(fn, *args)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def svc_schedule(call: ServiceCall) -> dict:
        idx = coordinator.idx or await hass.async_add_executor_job(gtfs.load_index)
        gtfs.check_line(idx, call.data["line"])
        return await _run(gtfs.schedule_day, idx, call.data["line"], _date(call))

    async def svc_arrivals(call: ServiceCall) -> dict:
        idx = coordinator.idx or await hass.async_add_executor_job(gtfs.load_index)
        gtfs.check_line(idx, call.data["line"])
        rt, _pos = await hass.async_add_executor_job(gtfs.realtime, coordinator.token)
        return await _run(gtfs.arrivals, idx, call.data["line"], call.data["station"],
                          rt, int(call.data.get("n", 5)))

    async def svc_query(call: ServiceCall) -> dict:
        idx = coordinator.idx or await hass.async_add_executor_job(gtfs.load_index)
        gtfs.check_line(idx, call.data["line"])
        rt, _pos = await hass.async_add_executor_job(gtfs.realtime, coordinator.token)
        return await _run(gtfs.query_pair, idx, call.data["line"], call.data["origin"],
                          call.data["destination"], rt, int(call.data.get("n", 3)))

    async def svc_train(call: ServiceCall) -> dict:
        idx = coordinator.idx or await hass.async_add_executor_job(gtfs.load_index)
        gtfs.check_line(idx, call.data["line"])
        rt, pos = await hass.async_add_executor_job(gtfs.realtime, coordinator.token)
        return await _run(gtfs.train_details, idx, call.data["line"],
                          str(call.data["train"]), _date(call), rt, pos)

    line_schema = {vol.Required("line"): str}
    hass.services.async_register(
        DOMAIN, "schedule", svc_schedule,
        schema=vol.Schema({**line_schema, vol.Optional("date"): str}),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, "arrivals", svc_arrivals,
        schema=vol.Schema({**line_schema, vol.Required("station"): str, vol.Optional("n"): vol.Coerce(int)}),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, "query", svc_query,
        schema=vol.Schema({**line_schema, vol.Required("origin"): str,
                           vol.Required("destination"): str, vol.Optional("n"): vol.Coerce(int)}),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, "train", svc_train,
        schema=vol.Schema({**line_schema, vol.Required("train"): vol.Coerce(str), vol.Optional("date"): str}),
        supports_response=SupportsResponse.ONLY)
