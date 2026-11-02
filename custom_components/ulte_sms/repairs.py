"""Repair: store the device's SSH host key permanently.

Some UniFi LTE firmware starts dropbear with an RSA host key that exists only
in RAM, so the key changes on every reboot. The fix copies the running key to
/etc/persistent and lets the firmware write its config to flash.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .modem import UlteError

ISSUE_HOST_KEY_VOLATILE = "host_key_not_persistent_{entry_id}"


@callback
def async_update_host_key_issue(hass: HomeAssistant, entry, host_keys: dict[str, str]) -> None:
    issue_id = ISSUE_HOST_KEY_VOLATILE.format(entry_id=entry.entry_id)
    volatile = [n for n, s in host_keys.items() if s in ("ram_only", "not_in_flash")]
    if not volatile:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="host_key_not_persistent",
        translation_placeholders={"title": entry.title, "files": ", ".join(volatile)},
        data={"entry_id": entry.entry_id},
    )


class PersistHostKeyFlow(RepairsFlow):
    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={"title": entry.title},
            )
        runtime = entry.runtime_data
        try:
            host_keys = await runtime.modem.async_persist_host_key()
        except UlteError as err:
            return self.async_abort(reason="persist_failed", description_placeholders={"error": str(err)})
        runtime.info.host_keys = host_keys
        async_update_host_key_issue(self.hass, entry, host_keys)
        if any(s != "persistent" for s in host_keys.values()):
            return self.async_abort(
                reason="persist_failed",
                description_placeholders={
                    "error": ", ".join(f"{n}: {s}" for n, s in host_keys.items())
                },
            )
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    return PersistHostKeyFlow((data or {})["entry_id"])
