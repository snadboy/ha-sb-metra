"""Diagnostics for SB Metra.

Settings -> Devices & services -> SB Metra -> Download diagnostics. Includes the
configured lines, what the last refresh produced per line, and the state of the
GTFS cache. The API token is redacted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import MetraCoordinator, gtfs
from .const import DOMAIN

TO_REDACT = {"api_token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: MetraCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    data = (coordinator.data if coordinator else None) or {}

    def _cache() -> dict[str, Any]:
        cache = Path(gtfs.CACHE)
        files = sorted(cache.glob("*")) if cache.is_dir() else []
        return {
            "path": str(cache),
            "exists": cache.is_dir(),
            "files": {f.name: f.stat().st_size for f in files if f.is_file()},
        }

    lines = data.get("lines", [])
    return {
        "entry": {
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "commute_pairs": len(entry.subentries),
        },
        "coordinator": {
            "loaded": coordinator is not None,
            "last_update_success": coordinator.last_update_success if coordinator else None,
            "updated": data.get("updated"),
            "span_days": coordinator.span_days if coordinator else None,
            "schedule_version": (coordinator.idx or {}).get("version") if coordinator else None,
            "routes_in_feed": len((coordinator.idx or {}).get("routes", [])) if coordinator else None,
        },
        "lines": {
            line: {
                "active_trains": len(data.get("active", {}).get(line, [])),
                "with_position": sum(
                    1 for t in data.get("active", {}).get(line, [])
                    if t.get("latitude") is not None
                ),
                "scheduled_today": data.get("schedule", {}).get(line, {}).get("count"),
                "service_days": len(data.get("schedule", {}).get(line, {}).get("days", [])),
                "patterns": list(data.get("schedule", {}).get(line, {}).get("patterns", {})),
            }
            for line in lines
        },
        "cache": await hass.async_add_executor_job(_cache),
    }
