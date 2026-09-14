"""Metra integration: coordinator, network-wide entities, and response actions.

Replaces the metra_mqtt.py MQTT-discovery publisher (2026-09-06). Consolidated
2026-09-14: one "Metra" service device carries sensor.metra_active_trains and
sensor.metra_schedule for every line, running trains are geo_location entities
for the map card, and the actions are domain actions that take `line`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.template import async_load_custom_templates
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import gtfs
from .const import CACHE_DIR, DOMAIN, LEGACY_CACHE_DIR, STATIC_URL, TEMPLATE_FILE

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "geo_location"]
UPDATE_INTERVAL = timedelta(minutes=2)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

LINES = vol.All(cv.ensure_list, [cv.string])
HERE = Path(__file__).parent


def _prepare_files(config_dir: Path) -> bool:
    """Point the GTFS cache at <config>/.metra_cache and install the macros.

    A pre-HACS install kept its cache in .metra_mqtt; it is renamed once so the
    GTFS download is not repeated. The bundled metra.jinja is copied into
    <config>/custom_templates only when it differs. Returns True when it did.
    """
    cache, legacy = config_dir / CACHE_DIR, config_dir / LEGACY_CACHE_DIR
    if legacy.is_dir() and not cache.exists():
        legacy.rename(cache)
    gtfs.set_cache_dir(cache)
    bundled = (HERE / "templates" / TEMPLATE_FILE).read_bytes()
    installed = config_dir / "custom_templates" / TEMPLATE_FILE
    if installed.is_file() and installed.read_bytes() == bundled:
        return False
    installed.parent.mkdir(exist_ok=True)
    partial = installed.with_name(TEMPLATE_FILE + ".partial")
    partial.write_bytes(bundled)
    partial.replace(installed)
    return True


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

    @property
    def span_days(self) -> int:
        return int(self.entry.options.get("span_days", 28))

    def _compute(self) -> dict:
        self.idx = gtfs.load_index()
        rt, pos = gtfs.realtime(self.token)
        now = datetime.now(gtfs.TZ)
        today = now.date()
        data: dict = {
            "routes": self.idx["routes"],
            "lines": self._lines(),
            "updated": now.strftime("%H:%M"),
            "active": {}, "schedule": {}, "favorites": {},
        }
        span_key = (today.isoformat(), self.idx["version"], tuple(data["lines"]), self.span_days)
        if self._span_cache.get("key") != span_key:
            self._span_cache = {"key": span_key, "lines": {
                line: gtfs.schedule_span(self.idx, line, today, days=self.span_days) for line in data["lines"]}}
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


# ---- response actions ------------------------------------------------------
# Domain actions, not entity services: `line` takes one line name or a list,
# and the response is always keyed by line name (response["UP-W"]), so one
# call answers for several lines and no consumer has to unwrap an entity id.

def _service_day(value) -> date:
    return date.fromisoformat(str(value)) if value else datetime.now(gtfs.TZ).date()


def _coordinator(hass: HomeAssistant) -> MetraCoordinator:
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if coordinator.data:
            return coordinator
    raise ServiceValidationError("Metra is not loaded yet")


async def _answer(hass: HomeAssistant, call: ServiceCall, work, realtime: bool) -> dict:
    """Run work(idx, line, rt, pos) for every requested line, keyed by line."""
    coordinator = _coordinator(hass)
    valid = coordinator.data["lines"]
    lines = call.data["line"]
    unknown = [line for line in lines if line not in valid]
    if unknown:
        raise ServiceValidationError(
            f"unknown line(s): {', '.join(unknown)}; configured lines: {', '.join(valid)}")
    idx = coordinator.idx or await hass.async_add_executor_job(gtfs.load_index)
    if realtime:
        rt, pos = await hass.async_add_executor_job(gtfs.realtime, coordinator.token)
    else:
        rt, pos = {}, {}

    def _run() -> dict:
        return {line: work(idx, line, rt, pos) for line in lines}

    try:
        return await hass.async_add_executor_job(_run)
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Install files, serve the map icons, and register the response actions."""
    if await hass.async_add_executor_job(_prepare_files, Path(hass.config.path())):
        _LOGGER.info("Installed %s into custom_templates", TEMPLATE_FILE)
        await async_load_custom_templates(hass)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(HERE / "www"), True)])

    async def schedule(call: ServiceCall) -> dict:
        return await _answer(hass, call, lambda idx, line, rt, pos: gtfs.schedule_day(
            idx, line, _service_day(call.data.get("date"))), realtime=False)

    async def arrivals(call: ServiceCall) -> dict:
        return await _answer(hass, call, lambda idx, line, rt, pos: gtfs.arrivals(
            idx, line, call.data["station"], rt, call.data["n"]), realtime=True)

    async def query(call: ServiceCall) -> dict:
        return await _answer(hass, call, lambda idx, line, rt, pos: gtfs.query_pair(
            idx, line, call.data["origin"], call.data["destination"], rt, call.data["n"]),
            realtime=True)

    async def train(call: ServiceCall) -> dict:
        return await _answer(hass, call, lambda idx, line, rt, pos: gtfs.train_details(
            idx, line, str(call.data["train"]), _service_day(call.data.get("date")), rt, pos),
            realtime=True)

    only = SupportsResponse.ONLY
    hass.services.async_register(DOMAIN, "schedule", schedule, schema=vol.Schema(
        {vol.Required("line"): LINES, vol.Optional("date"): cv.string}), supports_response=only)
    hass.services.async_register(DOMAIN, "arrivals", arrivals, schema=vol.Schema(
        {vol.Required("line"): LINES, vol.Required("station"): cv.string,
         vol.Optional("n", default=5): vol.Coerce(int)}), supports_response=only)
    hass.services.async_register(DOMAIN, "query", query, schema=vol.Schema(
        {vol.Required("line"): LINES, vol.Required("origin"): cv.string,
         vol.Required("destination"): cv.string,
         vol.Optional("n", default=3): vol.Coerce(int)}), supports_response=only)
    hass.services.async_register(DOMAIN, "train", train, schema=vol.Schema(
        {vol.Required("line"): LINES, vol.Required("train"): cv.string,
         vol.Optional("date"): cv.string}), supports_response=only)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = MetraCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload_on_change))
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
