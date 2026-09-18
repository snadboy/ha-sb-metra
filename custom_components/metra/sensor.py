"""Metra sensors: network-wide active trains and schedule, plus favorite pairs.

Consolidated 2026-09-14. Every line lives on two entities of one "Metra"
service device:
  sensor.metra_active_trains  state = trains running now      attr lines[<line>] = trains
  sensor.metra_schedule       state = trains scheduled today  attr lines[<line>] = {days, patterns}
Favorite commute pairs keep their own device and their publisher-era entity
ids (the delay-push automation triggers on them). Train positions for maps come
from the geo_location platform, not from sensors.
"""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MetraCoordinator
from .const import DOMAIN
from .gtfs import slug

_LOGGER = logging.getLogger(__name__)

# Keeps the identifier of the old "Metra Network" device, so the device is
# renamed in place rather than replaced.
METRA_DEVICE = DeviceInfo(identifiers={(DOMAIN, "network")}, name="Metra",
                          manufacturer="Metra GTFS-RT", model="All lines",
                          entry_type=DeviceEntryType.SERVICE)


class MetraBase(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:train"


class ActiveTrainsSensor(MetraBase):
    _attr_name = "Active trains"

    def __init__(self, coordinator: MetraCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_active_trains"
        self._attr_device_info = METRA_DEVICE
        self.entity_id = "sensor.metra_active_trains"

    @property
    def native_value(self) -> int:
        return sum(len(trains) for trains in self.coordinator.data["active"].values())

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        return {"lines": {line: data["active"].get(line, []) for line in data["lines"]},
                "updated": data["updated"]}


class ScheduleSensor(MetraBase):
    """Every line's timetable outlook.

    Deliberately has no `updated` attribute: the data only changes at midnight
    (or when Metra publishes a new schedule — the `published` attribute), so
    the ~1.2 MB of attributes stays identical between refreshes and HA does
    not rewrite the state (or push it to every open browser) every 2 minutes.
    """

    _attr_name = "Schedule"
    _attr_icon = "mdi:timetable"

    def __init__(self, coordinator: MetraCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_schedule"
        self._attr_device_info = METRA_DEVICE
        self.entity_id = "sensor.metra_schedule"

    @property
    def native_value(self) -> int:
        return sum(s.get("count", 0) for s in self.coordinator.data["schedule"].values())

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        return {"published": data.get("version"),
                "lines": {
            line: {"days": data["schedule"].get(line, {}).get("days", []),
                   "patterns": data["schedule"].get(line, {}).get("patterns", {})}
            for line in data["lines"]}}


FAV_KINDS = [
    ("next_inbound", "Next inbound", "up_in", True),
    ("upcoming_inbound", "Upcoming inbound", "up_in", False),
    ("enroute_inbound", "En route inbound", "en_in", False),
    ("next_outbound", "Next outbound", "up_out", True),
    ("upcoming_outbound", "Upcoming outbound", "up_out", False),
    ("enroute_outbound", "En route outbound", "en_out", False),
]

FAV_ENTITY_IDS = {
    "next_inbound": "next_inbound", "upcoming_inbound": "upcoming_inbound",
    "enroute_inbound": "en_route_inbound", "next_outbound": "next_outbound",
    "upcoming_outbound": "upcoming_outbound", "enroute_outbound": "en_route_outbound",
}


class FavoriteSensor(MetraBase):
    def __init__(self, coordinator: MetraCoordinator, subentry_id: str, line: str,
                 kind: str, label: str, data_key: str, is_next: bool,
                 via_device_id: str | None) -> None:
        super().__init__(coordinator)
        self.subentry_id, self.line = subentry_id, line
        self.kind, self.data_key, self.is_next = kind, data_key, is_next
        self._attr_name = label
        self._attr_unique_id = f"{DOMAIN}_fav_{subentry_id}_{kind}"
        # own device: subentry entities must not share the main entry's device
        # (a subentry-associated device silently rejects main-entry entities)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"fav_{subentry_id}")},
            name=f"Metra {line} commute",
            manufacturer="Metra GTFS-RT", model="Commute pair")
        if via_device_id:
            self._attr_device_info["via_device_id"] = via_device_id
        self.entity_id = f"sensor.metra_{slug(line)}_{FAV_ENTITY_IDS[kind]}"

    def _lst(self):
        fav = self.coordinator.data["favorites"].get(self.subentry_id)
        return fav[self.data_key] if fav else None

    @property
    def available(self) -> bool:
        return super().available and self._lst() is not None

    @property
    def native_value(self):
        lst = self._lst() or []
        if self.is_next:
            return lst[0]["display"] if lst else "none"
        return len(lst)

    @property
    def extra_state_attributes(self):
        lst = self._lst() or []
        attrs = {"updated": self.coordinator.data["updated"]}
        if self.is_next:
            if lst:
                t = lst[0]
                attrs.update({k: t[k] for k in ("train", "departure", "arrival", "is_live", "delay_min")})
        else:
            attrs["trains"] = lst
        return attrs


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: MetraCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [ActiveTrainsSensor(coordinator), ScheduleSensor(coordinator)]
    async_add_entities(entities)

    # Drop registry rows for entities this entry no longer provides: the per-line
    # and map-slot sensors retired by the consolidation, select.metra_line (line
    # choice now lives in dashboard pop-ups), or any later retiree.
    # Without this they linger as restored "unavailable" entities. Favorite
    # sensors belong to subentries and are never touched.
    ent_reg = er.async_get(hass)
    wanted = {e.unique_id for e in entities}
    stale = [r.entity_id for r in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
             if r.domain in ("sensor", "select") and r.config_subentry_id is None
             and not r.unique_id.startswith(f"{DOMAIN}_fav_")
             and r.unique_id not in wanted]
    for entity_id in stale:
        ent_reg.async_remove(entity_id)
    if stale:
        _LOGGER.info("removed %d stale metra sensor(s): %s", len(stale), ", ".join(stale[:10]))

    # via_device_id wants the parent's REGISTRY id; get_or_create it on the MAIN
    # entry (never the subentry, which would hijack the shared device).
    dev_reg = dr.async_get(hass)
    metra_device_id = dev_reg.async_get_or_create(config_entry_id=entry.entry_id, **METRA_DEVICE).id

    for sub in entry.subentries.values():
        if sub.subentry_type != "favorite":
            continue
        fav_entities = [FavoriteSensor(coordinator, sub.subentry_id, sub.data["line"],
                                       kind, label, data_key, is_next, metra_device_id)
                        for kind, label, data_key, is_next in FAV_KINDS]
        async_add_entities(fav_entities, config_subentry_id=sub.subentry_id)

    # Drop this entry's devices that no longer carry anything -- the per-line
    # devices retired by the consolidation. Keep the Metra device and every
    # favorite's own device.
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        ours = {ident for domain, ident in device.identifiers if domain == DOMAIN}
        if "network" in ours or any(ident.startswith("fav_") for ident in ours):
            continue
        dev_reg.async_remove_device(device.id)
        _LOGGER.info("removed retired metra device %s", device.name)
