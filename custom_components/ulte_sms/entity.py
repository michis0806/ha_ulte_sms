"""Shared device info.

The device carries the LTE device's MAC as a connection (Home Assistant keeps
it as a separate device next to the one of the UniFi Network integration).
Manufacturer and model use the same values as the UniFi integration.
"""
from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import UlteConfigEntry, UlteCoordinator


def device_info(entry: UlteConfigEntry) -> DeviceInfo:
    info = entry.runtime_data.info
    mac = entry.unique_id or info.mac or entry.entry_id
    hw = " ".join(x for x in ("Sierra Wireless", info.model) if x) if info.model else None
    return DeviceInfo(
        identifiers={(DOMAIN, mac)},
        connections={(CONNECTION_NETWORK_MAC, mac)} if ":" in mac else set(),
        name=entry.title,
        manufacturer="Ubiquiti Networks",
        model=info.board_short or info.board,
        hw_version=hw,
    )


class UlteEntity(CoordinatorEntity[UlteCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, entry: UlteConfigEntry, key: str) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{key}"
        self._attr_device_info = device_info(entry)
