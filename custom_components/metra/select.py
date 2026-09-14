"""Metra select: the line picker.

One select, owned by the integration so its options come from the GTFS roster
and never drift out of sync with it. Dashboards read it to decide which line a
card or map shows; nothing inside the integration depends on it.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MetraCoordinator
from .const import DOMAIN
from .sensor import METRA_DEVICE


class MetraLineSelect(CoordinatorEntity, SelectEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = "Line"
    _attr_icon = "mdi:train"

    def __init__(self, coordinator: MetraCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_network_line"
        self._attr_device_info = METRA_DEVICE
        self.entity_id = "select.metra_line"
        self._current: str | None = None

    @property
    def options(self) -> list[str]:
        return list(self.coordinator.data["lines"])

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
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self.options:
            self._current = last.state


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: MetraCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MetraLineSelect(coordinator)])
