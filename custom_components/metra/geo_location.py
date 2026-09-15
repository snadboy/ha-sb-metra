"""Metra geo_location: one entity per train, for the map card.

Entities are created as trains report a position, so there are no fixed slots
and nothing in the entity registry (no unique_id). Each train's `source` is
`metra_<line>` (e.g. metra_up_w), so a map card shows one line with
`geo_location_sources: [metra_up_w]` or several by listing them.

When a train drops out of the feed its entity is PARKED, not removed: it loses
its coordinates (so the map card stops drawing it) and gets status "finished".
Parked entities are purged overnight, when no trains run. Removing them at once
would leave stale markers behind: the Home Assistant frontend keeps entities
that were removed while it was disconnected (a restart, a network blip, a tab
hidden for five minutes), because it merges the state snapshot it receives on
reconnect into its old store. An entity that still exists, without a location,
reaches a reconnecting client and clears its marker.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util.location import distance as geo_distance

from . import MetraCoordinator
from .const import DOMAIN, STATIC_URL
from .gtfs import slug

# The GTFS schedule has no train running between 02:30 and 04:00 (the first
# departure is BNSF #1200 at 04:00), so parked trains are deleted in that gap.
PURGE_HOUR, PURGE_MINUTE = 3, 30


class MetraTrainLocation(GeolocationEvent):
    _attr_should_poll = False
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:train"

    def __init__(self, hass: HomeAssistant, line: str, train: dict) -> None:
        self._line = line
        self._train: dict = {}
        self.parked = False
        self._attr_source = f"metra_{slug(line)}"
        self._attr_name = f"Metra {line} {train['train']}"
        self._picture = f"{STATIC_URL}/engine_{slug(line)}_{train['direction']}.svg"
        self.update_from(hass, train)

    def update_from(self, hass: HomeAssistant, train: dict) -> None:
        self._train = train
        self.parked = False
        self._attr_entity_picture = self._picture
        self._attr_latitude = train["latitude"]
        self._attr_longitude = train["longitude"]
        meters = geo_distance(hass.config.latitude, hass.config.longitude,
                              train["latitude"], train["longitude"])
        self._attr_distance = meters / 1000 if meters is not None else None

    def park(self) -> None:
        """Keep the entity but drop its location, so maps stop drawing it."""
        self.parked = True
        self._attr_entity_picture = None
        self._attr_latitude = None
        self._attr_longitude = None
        self._attr_distance = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        t = self._train
        base = {"line": self._line, "train": t.get("train"), "direction": t.get("direction")}
        if self.parked:
            return {**base, "status": "finished"}
        stops = t.get("stops") or []
        return {**base, "status": "running",
                "destination": t.get("destination"), "next_station": t.get("next_station"),
                "eta": t.get("eta"), "delay_min": t.get("delay_min"),
                "terminal_eta": stops[-1]["eta"] if stops else None}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: MetraCoordinator = hass.data[DOMAIN][entry.entry_id]
    tracked: dict[tuple[str, str, str], MetraTrainLocation] = {}

    @callback
    def _sync() -> None:
        data = coordinator.data or {}
        seen: set[tuple[str, str, str]] = set()
        new: list[MetraTrainLocation] = []
        for line in data.get("lines", []):
            for train in data.get("active", {}).get(line, []):
                if train.get("latitude") is None or train.get("longitude") is None:
                    continue
                key = (line, str(train["train"]), train["direction"])
                seen.add(key)
                entity = tracked.get(key)
                if entity is None:
                    entity = tracked[key] = MetraTrainLocation(hass, line, train)
                    new.append(entity)
                else:
                    entity.update_from(hass, train)
                    if entity.hass is not None:
                        entity.async_write_ha_state()
        for key, entity in tracked.items():
            if key not in seen and not entity.parked:
                entity.park()
                if entity.hass is not None:
                    entity.async_write_ha_state()
        if new:
            async_add_entities(new)

    @callback
    def _purge(_now: datetime) -> None:
        for key in [k for k, e in tracked.items() if e.parked]:
            entity = tracked.pop(key)
            if entity.hass is not None:
                hass.async_create_task(entity.async_remove(force_remove=True))

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))
    entry.async_on_unload(async_track_time_change(
        hass, _purge, hour=PURGE_HOUR, minute=PURGE_MINUTE, second=0))
