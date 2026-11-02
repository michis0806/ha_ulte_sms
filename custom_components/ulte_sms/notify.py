"""Notify entity: sends to the default recipients from the options."""
from __future__ import annotations

from homeassistant.components.notify import NotifyEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, OPT_RECIPIENTS
from .coordinator import UlteConfigEntry
from .entity import UlteEntity
from .services import async_send


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UlteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([UlteNotify(entry)])


class UlteNotify(UlteEntity, NotifyEntity):
    _attr_translation_key = "sms"

    def __init__(self, entry: UlteConfigEntry) -> None:
        super().__init__(entry, "notify")
        self._entry = entry

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        recipients = self._entry.options.get(OPT_RECIPIENTS) or []
        if not recipients:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_default_recipients"
            )
        text = f"{title}\n{message}" if title else message
        await async_send(self._entry, list(recipients), text)
