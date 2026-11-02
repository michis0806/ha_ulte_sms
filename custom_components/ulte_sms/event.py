"""Event entity "Incoming SMS" — usable as a trigger in the automation editor."""
from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SIGNAL_RECEIVED
from .coordinator import UlteConfigEntry
from .entity import UlteEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UlteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([IncomingSmsEvent(entry)])


class IncomingSmsEvent(UlteEntity, EventEntity):
    _attr_translation_key = "incoming_sms"
    _attr_event_types = ["received"]

    def __init__(self, entry: UlteConfigEntry) -> None:
        super().__init__(entry, "incoming_sms")
        self._entry_id = entry.entry_id

    @property
    def available(self) -> bool:
        # An event entity keeps its last event even while the modem is offline.
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _received(entry_id: str, data: dict[str, Any]) -> None:
            if entry_id != self._entry_id:
                return
            self._trigger_event(
                "received", {k: v for k, v in data.items() if k != "config_entry_id"}
            )
            self.async_write_ha_state()

        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_RECEIVED, _received))

    @callback
    def _handle_coordinator_update(self) -> None:
        # State only changes on events, not on every poll.
        return
