"""Polling coordinator: status + incoming SMS in one SSH call."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_HOST_KEY,
    DEFAULT_ACCEPT_HOST_KEY,
    DEFAULT_DELETE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_RECEIVED,
    INCOMPLETE_AFTER,
    OPT_ACCEPT_HOST_KEY,
    OPT_DELETE,
    OPT_SCAN_INTERVAL,
    SIGNAL_RECEIVED,
)
from .modem import (
    UlteAuthError,
    UlteDeleteError,
    UlteError,
    UlteHostKeyError,
    UlteModem,
    fetch_host_key,
    fingerprint,
)
from .parser import ModemInfo, ModemStatus
from .pdu import SmsMessage, assemble

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

type UlteConfigEntry = ConfigEntry[UlteRuntime]


@dataclass
class UlteRuntime:
    modem: UlteModem
    info: ModemInfo
    coordinator: UlteCoordinator


async def async_accept_host_key_change(
    hass: HomeAssistant, entry: UlteConfigEntry, modem: UlteModem
) -> bool:
    """If enabled in the options: trust the device's new host key.

    Some UniFi LTE firmware keeps the RSA host key only in RAM, so it changes
    on every reboot. Returns True if a new key was stored.
    """
    if not entry.options.get(OPT_ACCEPT_HOST_KEY, DEFAULT_ACCEPT_HOST_KEY):
        return False
    try:
        new_key = await fetch_host_key(modem.host, modem.port)
    except UlteError:
        return False
    old_key = entry.data[CONF_HOST_KEY]
    if new_key == old_key:
        return False
    _LOGGER.warning(
        "Host key of %s changed from %s to %s; accepted because the option is enabled",
        modem.host, fingerprint(old_key), fingerprint(new_key),
    )
    modem.set_host_key(new_key)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST_KEY: new_key})
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"host_key_accepted_{entry.entry_id}",
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="host_key_accepted",
        translation_placeholders={
            "title": entry.title,
            "host": modem.host,
            "old": fingerprint(old_key),
            "new": fingerprint(new_key),
            "time": dt_util.now().strftime("%Y-%m-%d %H:%M"),
        },
    )
    return True


class UlteCoordinator(DataUpdateCoordinator[ModemStatus]):
    """Polls the modem; fires an event for every new SMS."""

    config_entry: UlteConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: UlteConfigEntry, modem: UlteModem, info: ModemInfo
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(
                seconds=entry.options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.modem = modem
        self.info = info
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._seen: set[str] = set()
        self._delivered_parts: set[str] = set()
        self.waiting_parts = 0
        self.received_total = 0
        self.last_delete_error: str | None = None

    async def _async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        self._seen = set(stored.get("seen", []))
        self._delivered_parts = set(stored.get("delivered_parts", []))

    async def _async_update_data(self) -> ModemStatus:
        try:
            try:
                status, parts, other = await self.modem.async_poll()
            except UlteHostKeyError:
                if not await async_accept_host_key_change(
                    self.hass, self.config_entry, self.modem
                ):
                    raise
                status, parts, other = await self.modem.async_poll()
        except (UlteHostKeyError, UlteAuthError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except UlteError as err:
            raise UpdateFailed(str(err)) from err

        # A multipart message's key changes when deletion removes only some of
        # its parts. Track delivered SIM contents individually, so the remnants
        # cannot become a second (incomplete) message, including after restart.
        # Only hashes are persisted; slots reused for a different PDU are new.
        fingerprints = {
            part.index: hashlib.sha256(f"{part.index}:{part.pdu}".encode()).hexdigest()
            for part in parts
        }
        delivered_parts = self._delivered_parts.intersection(fingerprints.values())
        messages, waiting = assemble(
            [part for part in parts if fingerprints[part.index] not in delivered_parts],
            dt_util.utcnow(), INCOMPLETE_AFTER,
        )
        self.waiting_parts = len(waiting)

        for message in messages:
            if message.key not in self._seen:
                self._fire(message)
            # Legacy installations only stored message keys. Seed their part
            # fingerprints too, without emitting the complete message again.
            delivered_parts.update(fingerprints[index] for index in message.indices)

        on_sim = {m.key for m in messages}
        delete = self.config_entry.options.get(OPT_DELETE, DEFAULT_DELETE)
        indices = sorted(
            {index for index, key in fingerprints.items() if key in delivered_parts}
            | set(other)
        ) if delete else []
        if indices:
            deleted: set[int] = set()
            try:
                await self.modem.async_delete(indices)
            except UlteError as err:
                if isinstance(err, UlteDeleteError):
                    deleted = set(err.deleted_indices).intersection(indices)
                self.last_delete_error = str(err)
                _LOGGER.warning("Could not delete SMS %s from the SIM: %s", indices, err)
            else:
                self.last_delete_error = None
                deleted = set(indices)
            if status.sms_used is not None:
                status.sms_used = max(0, status.sms_used - len(deleted))

        if on_sim != self._seen or delivered_parts != self._delivered_parts:
            self._seen = on_sim
            self._delivered_parts = delivered_parts
            await self._store.async_save({
                "seen": sorted(self._seen),
                "delivered_parts": sorted(self._delivered_parts),
            })
        return status

    def _fire(self, message: SmsMessage) -> None:
        self.received_total += 1
        data = {
            "config_entry_id": self.config_entry.entry_id,
            "sender": message.sender,
            "text": message.text,
            "timestamp": message.timestamp.isoformat(),
            "parts": message.parts,
            "complete": message.complete,
            "receiver": self.info.own_number,
        }
        _LOGGER.debug("SMS from %s (%s part(s))", message.sender, message.parts)
        self.hass.bus.async_fire(EVENT_RECEIVED, data)
        async_dispatcher_send(self.hass, SIGNAL_RECEIVED, self.config_entry.entry_id, data)
