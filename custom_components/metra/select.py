"""Metra selects: the line picker, plus origin/destination pickers that follow it.

Replaces input_select.metra_line and the two input_text station boxes. Options
come from the integration itself, so the line list can never drift out of sync
(that is what automation.metra_line_dropdown_sync used to do by hand) and a
station can only ever be one that exists on the selected line.
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


class MetraSelectBase(CoordinatorEntity, SelectEntity, RestoreEntity):
    _attr_has_entity_name = True

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


class MetraLineSelect(MetraSelectBase):
    def __init__(self, coordinator: MetraCoordinator) -> None:
        super().__init__(coordinator, "line", "Line", "mdi:train")

    @property
    def options(self) -> list[str]:
        return list(self.coordinator.data["lines"])

    def _selected(self, option: str | None) -> None:
        self.coordinator.selection.set_line(option)


class MetraStationSelect(MetraSelectBase):
    """Stops on whatever line is currently selected."""

    def __init__(self, coordinator: MetraCoordinator, key: str, name: str, icon: str) -> None:
        super().__init__(coordinator, key, name, icon)

    @property
    def options(self) -> list[str]:
        line = self.coordinator.selection.line
        return self.coordinator.stops(line) if line else []

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.selection.subscribe(self._line_changed))

    @callback
    def _line_changed(self) -> None:
        # a stop from the old line is meaningless on the new one; fall back to
        # the first stop in route order rather than leaving an invalid state
        self._current = None
        self.async_write_ha_state()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: MetraCoordinator = hass.data[DOMAIN][entry.entry_id]
    # line first: its restored value has to be published before the station
    # selects work out what their options are
    async_add_entities([
        MetraLineSelect(coordinator),
        MetraStationSelect(coordinator, "station", "Origin", "mdi:map-marker"),
        MetraStationSelect(coordinator, "destination", "Destination", "mdi:map-marker-check"),
    ])
