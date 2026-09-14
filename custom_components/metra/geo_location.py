"""Metra geo_location: one entity per running train, for the map card.

Entities exist only while a train reports a position: they are created and
removed with the trains, so there are no fixed slots, no empty placeholders,
and nothing in the entity registry (no unique_id). Each train's `source` is
`metra_<line>` (e.g. metra_up_w), so a map card shows one line with
`geo_location_sources: [metra_up_w]` or several by listing them.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.location import distance as geo_distance

from . import MetraCoordinator
from .const import DOMAIN, STATIC_URL
from .gtfs import slug


class MetraTrainLocation(GeolocationEvent):
    _attr_should_poll = False
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:train"

    def __init__(self, hass: HomeAssistant, line: str, train: dict) -> None:
        self._line = line
        self._train: dict = {}
        self._attr_source = f"metra_{slug(line)}"
        self._attr_name = f"Metra {line} {train['train']}"
        self._attr_entity_picture = f"{STATIC_URL}/engine_{slug(line)}_{train['direction']}.svg"
        self.update_from(hass, train)

    def update_from(self, hass: HomeAssistant, train: dict) -> None:
        self._train = train
        self._attr_latitude = train["latitude"]
        self._attr_longitude = train["longitude"]
        meters = geo_distance(hass.config.latitude, hass.config.longitude,
                              train["latitude"], train["longitude"])
        self._attr_distance = meters / 1000 if meters is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        t = self._train
        stops = t.get("stops") or []
        return {"line": self._line, "train": t.get("train"), "direction": t.get("direction"),
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
        for key in [k for k in tracked if k not in seen]:
            entity = tracked.pop(key)
            if entity.hass is not None:
                hass.async_create_task(entity.async_remove(force_remove=True))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))
