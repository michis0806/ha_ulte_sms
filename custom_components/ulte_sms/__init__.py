"""SMS over the UniFi LTE Backup Pro (U-LTE-Pro).

Home Assistant logs in to the device over SSH and talks to the LTE modem's AT
port. Nothing is installed on the device.
"""
from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_HOST_KEY, CONF_PRIVATE_KEY, DEFAULT_PORT, DOMAIN
from .coordinator import (
    UlteConfigEntry,
    UlteCoordinator,
    UlteRuntime,
    async_accept_host_key_change,
)
from .modem import UlteAuthError, UlteError, UlteHostKeyError, UlteModem
from .repairs import async_update_host_key_issue
from .services import async_setup_services

PLATFORMS = [Platform.EVENT, Platform.NOTIFY, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def modem_from_data(data: dict) -> UlteModem:
    return UlteModem(
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_HOST_KEY],
        port=data.get(CONF_PORT, DEFAULT_PORT),
        password=data.get(CONF_PASSWORD),
        private_key=data.get(CONF_PRIVATE_KEY),
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: UlteConfigEntry) -> bool:
    modem = modem_from_data(dict(entry.data))
    try:
        try:
            info = await modem.async_info()
        except UlteHostKeyError:
            if not await async_accept_host_key_change(hass, entry, modem):
                raise
            info = await modem.async_info()
        coordinator = UlteCoordinator(hass, entry, modem, info)
        await coordinator.async_config_entry_first_refresh()
    except (UlteHostKeyError, UlteAuthError) as err:
        await modem.async_close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except UlteError as err:
        await modem.async_close()
        raise ConfigEntryNotReady(str(err)) from err
    except Exception:
        await modem.async_close()
        raise

    entry.runtime_data = UlteRuntime(modem, info, coordinator)
    async_update_host_key_issue(hass, entry, info.host_keys)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UlteConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.modem.async_close()
    return unloaded
