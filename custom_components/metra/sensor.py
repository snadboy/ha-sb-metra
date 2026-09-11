"""Metra sensors: roster, per-line active/schedule, map slots, favorite pairs.

Entity ids are pinned to the MQTT-publisher-era ids (set explicitly) so
dashboards, the delay-push automation, and the customize-free map keep working.
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MetraCoordinator
from .const import DOMAIN
from .gtfs import slug


def _line_device(line: str, routes: list[dict]) -> DeviceInfo:
    long_name = next((r["name"] for r in routes if r["id"] == line), line)
    return DeviceInfo(identifiers={(DOMAIN, f"line_{slug(line)}")},
                      name=f"Metra {line}", manufacturer="Metra GTFS-RT", model=long_name)


NETWORK_DEVICE = DeviceInfo(identifiers={(DOMAIN, "network")}, name="Metra Network",
                            manufacturer="Metra GTFS-RT", model="System roster")


class MetraBase(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:train"

    def __init__(self, coordinator: MetraCoordinator) -> None:
        super().__init__(coordinator)


class RosterSensor(MetraBase):
    _attr_name = "Lines"

    def __init__(self, coordinator: MetraCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_network_lines"
        self._attr_device_info = NETWORK_DEVICE
        self.entity_id = "sensor.metra_network_lines"

    @property
    def native_value(self):
        return len(self.coordinator.data["routes"])

    @property
    def extra_state_attributes(self):
        return {"lines": self.coordinator.data["routes"],
                "updated": self.coordinator.data["updated"]}


class ActiveTrainsSensor(MetraBase):
    _attr_name = "Active trains"

    def __init__(self, coordinator: MetraCoordinator, line: str) -> None:
        super().__init__(coordinator)
        self.line = line
        self._attr_unique_id = f"{DOMAIN}_line_{slug(line)}_active_trains"
        self._attr_device_info = _line_device(line, coordinator.data["routes"])
        self.entity_id = f"sensor.metra_{slug(line)}_active_trains"

    @property
    def available(self) -> bool:
        return super().available and self.line in self.coordinator.data["active"]

    @property
    def native_value(self):
        return len(self.coordinator.data["active"].get(self.line, []))

    @property
    def extra_state_attributes(self):
        return {"trains": self.coordinator.data["active"].get(self.line, []),
                "updated": self.coordinator.data["updated"]}


class TodayScheduleSensor(MetraBase):
    _attr_name = "Schedule"
    _attr_icon = "mdi:timetable"

    def __init__(self, coordinator: MetraCoordinator, line: str) -> None:
        super().__init__(coordinator)
        self.line = line
        self._attr_unique_id = f"{DOMAIN}_line_{slug(line)}_today_schedule"
        self._attr_device_info = _line_device(line, coordinator.data["routes"])
        self.entity_id = f"sensor.metra_{slug(line)}_schedule"

    @property
    def available(self) -> bool:
        return super().available and self.line in self.coordinator.data["schedule"]

    @property
    def native_value(self):
        return self.coordinator.data["schedule"].get(self.line, {}).get("count", 0)

    @property
    def extra_state_attributes(self):
        sched = self.coordinator.data["schedule"].get(self.line, {})
        return {"date": sched.get("date"), "trains": sched.get("trains", []),
                "days": sched.get("days", []), "patterns": sched.get("patterns", {}),
                "updated": self.coordinator.data["updated"]}


def _slot_attrs(t: dict | None, updated: str) -> dict:
    """Map-slot attributes; lat/lon present only while the slot holds a train."""
    if not t:
        return {"updated": updated}
    return {"latitude": t["latitude"], "longitude": t["longitude"],
            "heading_to": t["destination"], "next_station": t["next_station"],
            "next_eta": t["eta"],
            "dest_eta": (t["stops"][-1]["eta"] if t.get("stops") else "?"),
            "stops": t.get("stops", []),
            "updated": updated}


class MapSlotSensor(MetraBase):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MetraCoordinator, line: str, direction: str, i: int) -> None:
        super().__init__(coordinator)
        self.line, self.direction, self.i = line, direction, i
        self._attr_name = f"{direction.capitalize()} {line} {i}"
        self._attr_unique_id = f"{DOMAIN}_line_{slug(line)}_pos_{direction}_{i}"
        self._attr_device_info = _line_device(line, coordinator.data["routes"])
        self._attr_entity_picture = f"/local/metra/engine_{slug(line)}_{direction}.svg?v=2"
        self.entity_id = f"sensor.metra_{slug(line)}_map_{direction}_{i}"

    def _train(self):
        trains = [t for t in self.coordinator.data["active"].get(self.line, [])
                  if t["direction"] == self.direction and t.get("latitude") is not None]
        return trains[self.i - 1] if self.i <= len(trains) else None

    @property
    def native_value(self):
        t = self._train()
        return t["train"] if t else "none"

    @property
    def extra_state_attributes(self):
        return _slot_attrs(self._train(), self.coordinator.data["updated"])


class SelectedLineSlotSensor(MetraBase):
    """Active train N on whichever line select.metra_line points at.

    The per-line map slots pin a map to one line; these follow the selection,
    so a single map card can show any line's live trains. Live positions only
    exist for today, so the date select has no bearing on them.
    """
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MetraCoordinator, i: int) -> None:
        super().__init__(coordinator)
        self.i = i
        self._attr_name = f"Active train {i}"
        self._attr_unique_id = f"{DOMAIN}_network_active_train_{i}"
        self._attr_device_info = NETWORK_DEVICE
        self.entity_id = f"sensor.metra_active_train_{i}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # re-render on a line change, not just on the 2-minute refresh
        self.async_on_remove(self.coordinator.selection.subscribe(self.async_write_ha_state))

    def _train(self):
        line = self.coordinator.selection.line
        if not line:
            return None
        live = [t for t in self.coordinator.data["active"].get(line, [])
                if t.get("latitude") is not None]
        live.sort(key=lambda t: t["direction"] != "inbound")   # stable: inbound first
        return live[self.i - 1] if self.i <= len(live) else None

    @property
    def native_value(self):
        t = self._train()
        return t["train"] if t else "none"

    @property
    def entity_picture(self):
        t = self._train()
        if not t:
            return None
        return f"/local/metra/engine_{slug(self.coordinator.selection.line)}_{t['direction']}.svg?v=2"

    @property
    def extra_state_attributes(self):
        t = self._train()
        attrs = _slot_attrs(t, self.coordinator.data["updated"])
        attrs["line"] = self.coordinator.selection.line
        if t:
            attrs["direction"] = t["direction"]
        return attrs


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
                 kind: str, label: str, data_key: str, is_next: bool) -> None:
        super().__init__(coordinator)
        self.subentry_id, self.line = subentry_id, line
        self.kind, self.data_key, self.is_next = kind, data_key, is_next
        self._attr_name = label
        self._attr_unique_id = f"{DOMAIN}_fav_{subentry_id}_{kind}"
        # own device: subentry entities must not share the main entry's line
        # device (a subentry-associated device silently rejects main-entry
        # entities, wiping out the line's per-line sensors)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"fav_{subentry_id}")},
            name=f"Metra {line} commute",
            manufacturer="Metra GTFS-RT", model="Commute pair",
            via_device=(DOMAIN, f"line_{slug(line)}"))
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
    data = coordinator.data
    entities: list[SensorEntity] = [RosterSensor(coordinator)]
    for line in data["lines"]:
        entities.append(ActiveTrainsSensor(coordinator, line))
        entities.append(TodayScheduleSensor(coordinator, line))
    for line in data["map_lines"]:
        for direction in ("inbound", "outbound"):
            for i in range(1, coordinator.map_slots + 1):
                entities.append(MapSlotSensor(coordinator, line, direction, i))
    # both directions share one numbering here, so twice the per-direction cap
    for i in range(1, 2 * coordinator.map_slots + 1):
        entities.append(SelectedLineSlotSensor(coordinator, i))
    async_add_entities(entities)

    for sub in entry.subentries.values():
        if sub.subentry_type != "favorite":
            continue
        fav_entities = [FavoriteSensor(coordinator, sub.subentry_id, sub.data["line"],
                                       kind, label, data_key, is_next)
                        for kind, label, data_key, is_next in FAV_KINDS]
        async_add_entities(fav_entities, config_subentry_id=sub.subentry_id)
