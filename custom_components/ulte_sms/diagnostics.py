"""Diagnostics (secrets, IMEI and own number redacted)."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_PRIVATE_KEY
from .coordinator import UlteConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_PRIVATE_KEY, "imei", "own_number", "receiver"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UlteConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "info": async_redact_data(asdict(runtime.info), TO_REDACT),
        "status": asdict(coordinator.data) if coordinator.data else None,
        "last_update_success": coordinator.last_update_success,
        "waiting_multipart_parts": coordinator.waiting_parts,
        "received_since_start": coordinator.received_total,
        "last_delete_error": coordinator.last_delete_error,
    }
