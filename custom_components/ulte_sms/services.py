"""Action ulte_sms.send_sms and the shared send helper."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_MESSAGE,
    ATTR_RECIPIENTS,
    DOMAIN,
    SERVICE_SEND_SMS,
)
from .modem import UlteError
from .pdu import PduError, count_parts, normalize_number

_LOGGER = logging.getLogger(__name__)

SEND_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_RECIPIENTS): vol.All(cv.ensure_list, [cv.string], vol.Length(min=1)),
        vol.Required(ATTR_MESSAGE): cv.string,
    }
)


def _entry(hass: HomeAssistant, entry_id: str | None):
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_found",
                translation_placeholders={"entry_id": entry_id},
            )
        entries = [entry]
    else:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if len(entries) != 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_required" if entries else "no_entry",
            )
    entry = entries[0]
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    return entry


def validate_recipients(recipients: list[str]) -> list[str]:
    numbers: list[str] = []
    for raw in recipients:
        try:
            number = normalize_number(raw)
        except PduError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_number",
                translation_placeholders={"number": str(raw)},
            ) from err
        if number not in numbers:
            numbers.append(number)
    return numbers


async def async_send(entry, recipients: list[str], message: str) -> list[dict[str, Any]]:
    """Send ``message`` to every recipient. Raises if any of them failed —
    after trying all of them."""
    if not message.strip():
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="empty_message")
    numbers = validate_recipients(recipients)
    try:
        parts, _encoding = count_parts(message)
    except PduError as err:
        raise ServiceValidationError(str(err)) from err
    if parts > 10:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="message_too_long",
            translation_placeholders={"parts": str(parts)},
        )

    modem = entry.runtime_data.modem
    results: list[dict[str, Any]] = []
    failed: list[str] = []
    for number in numbers:
        try:
            refs = await modem.async_send(number, message)
        except (UlteError, PduError) as err:
            _LOGGER.error("SMS to %s failed: %s", number, err)
            failed.append(f"{number}: {err}")
            results.append({"recipient": number, "success": False, "error": str(err)})
        else:
            _LOGGER.debug("SMS to %s sent, references %s", number, refs)
            results.append({"recipient": number, "success": True, "parts": len(refs),
                            "references": refs})
    if failed:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="send_failed",
            translation_placeholders={"errors": "; ".join(failed)},
        )
    return results


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def _send(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        results = await async_send(entry, call.data[ATTR_RECIPIENTS], call.data[ATTR_MESSAGE])
        return {"results": results} if call.return_response else None

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_SMS, _send, schema=SEND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
