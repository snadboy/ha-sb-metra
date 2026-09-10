"""Metra selects: line, service date, origin, destination and train.

Every picker the query console needs, owned by the integration so its options
come from the GTFS data instead of being maintained beside it. Only valid
choices are offerable: stations exist on the selected line, dates fall inside
the feed horizon, trains actually run on that line that day. That retires
input_select.metra_line, the two input_text station boxes, input_text.
metra_train_number, input_datetime.metra_date, and the two automations that
existed only to keep a helper in sync (line dropdown sync, console date reset).

Selects whose value the console must pass on to a service publish a
machine-readable form as an attribute -- select.metra_date has `date`
(ISO), select.metra_train has `train` (bare number) -- so the automation
reads state_attr() instead of unpicking a display label with string surgery.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MetraCoordinator
from .const import DOMAIN
from .sensor import NETWORK_DEVICE


def _date_label(day: dict) -> str:
    return f"{day['day'][:3]}, {day['date'].replace('-', '.')}"


def _train_label(t: dict) -> str:
    return f"{t['train']} · {t['direction']} · {t['departs']} → {t['arrives']}"


class MetraSelectBase(CoordinatorEntity, SelectEntity, RestoreEntity):
    _attr_has_entity_name = True
    _reset_on_change = True

    def __init__(self, coordinator: MetraCoordinator, key: str, name: str, icon: str) -> None:
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{DOMAIN}_network_{key}"
        self._attr_device_info = NETWORK_DEVICE
        self.entity_id = f"select.metra_{key}"
        self._current: str | None = None

    @property
    def current_option(self) -> str | None:
        opts = self.options
        if self._current in opts:
            return self._current
        return opts[0] if opts else None

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ServiceValidationError(f"{option!r} is not available on {self.entity_id}")
        self._current = option
        self._selected(option)
        self.async_write_ha_state()

    def _selected(self, option: str | None) -> None:
        """Hook for subclasses that publish their choice."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self.options:
            self._current = last.state
        self._selected(self.current_option)


class MetraDependentSelect(MetraSelectBase):
    """Re-renders when the line (or date) behind it changes."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.selection.subscribe(self._upstream_changed))

    @callback
    def _upstream_changed(self) -> None:
        if self._reset_on_change:
            # a stop or train from the old line/date is meaningless now; fall
            # back to the first option rather than holding an invalid state
            self._current = None
        self._selected(self.current_option)
        self.async_write_ha_state()


class MetraLineSelect(MetraSelectBase):
    def __init__(self, coordinator: MetraCoordinator) -> None:
        super().__init__(coordinator, "line", "Line", "mdi:train")

    @property
    def options(self) -> list[str]:
        return list(self.coordinator.data["lines"])

    def _selected(self, option: str | None) -> None:
        self.coordinator.selection.set_line(option)


class MetraDateSelect(MetraDependentSelect):
    """Service dates for the selected line, bounded by the GTFS feed horizon.

    input_datetime has no min/max, so a picker cannot be constrained -- an
    out-of-horizon date just returns an empty schedule. Offering the valid
    dates as options is what actually makes an invalid one unchoosable.
    """

    _reset_on_change = False   # a date stays meaningful when the line changes

    def __init__(self, coordinator: MetraCoordinator) -> None:
        super().__init__(coordinator, "date", "Date", "mdi:calendar")

    def _days(self) -> list[dict]:
        line = self.coordinator.selection.line
        if not line:
            return []
        return self.coordinator.data["schedule"].get(line, {}).get("days", [])

    @property
    def options(self) -> list[str]:
        return [_date_label(d) for d in self._days()]

    @property
    def extra_state_attributes(self) -> dict:
        return {"date": self._iso()}

    def _iso(self) -> str | None:
        cur = self.current_option
        return next((d["date"] for d in self._days() if _date_label(d) == cur), None)

    def _selected(self, option: str | None) -> None:
        self.coordinator.selection.set_date(self._iso())


class MetraStationSelect(MetraDependentSelect):
    """Stops on whatever line is currently selected."""

    @property
    def options(self) -> list[str]:
        line = self.coordinator.selection.line
        return self.coordinator.stops(line) if line else []


class MetraTrainSelect(MetraDependentSelect):
    """Trains running on the selected line on the selected date."""

    def __init__(self, coordinator: MetraCoordinator) -> None:
        super().__init__(coordinator, "train", "Train", "mdi:train-car")

    def _trains(self) -> list[dict]:
        sel = self.coordinator.selection
        return self.coordinator.day_trains(sel.line, sel.date)

    @property
    def options(self) -> list[str]:
        return [_train_label(t) for t in self._trains()]

    @property
    def extra_state_attributes(self) -> dict:
        cur = self.current_option
        return {"train": next((t["train"] for t in self._trains()
                               if _train_label(t) == cur), None)}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: MetraCoordinator = hass.data[DOMAIN][entry.entry_id]
    # order matters: each entity publishes its restored choice as it is added,
    # and the ones after it work out their options from what is already set
    async_add_entities([
        MetraLineSelect(coordinator),
        MetraDateSelect(coordinator),
        MetraStationSelect(coordinator, "station", "Origin", "mdi:map-marker"),
        MetraStationSelect(coordinator, "destination", "Destination", "mdi:map-marker-check"),
        MetraTrainSelect(coordinator),
    ])
