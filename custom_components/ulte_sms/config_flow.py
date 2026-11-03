"""Config flow: SSH credentials, host key confirmation (trust on first use),
DHCP discovery (also follows IP changes), reauth on host key change,
reconfigure and options."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .const import (
    CONF_HOST_KEY,
    CONF_PRIVATE_KEY,
    DEFAULT_ACCEPT_HOST_KEY,
    DEFAULT_DELETE,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    OPT_ACCEPT_HOST_KEY,
    OPT_DELETE,
    OPT_RECIPIENTS,
    OPT_SCAN_INTERVAL,
)
from .modem import (
    UlteAuthError,
    UlteConnectionError,
    UlteError,
    UlteHostKeyError,
    UlteModem,
    fetch_host_key,
    fingerprint,
)
from .parser import ModemInfo
from .pdu import PduError, normalize_number

_LOGGER = logging.getLogger(__name__)

SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
MULTILINE = TextSelector(TextSelectorConfig(multiline=True))


def _credentials_schema(defaults: dict[str, Any], *, with_host: bool = True) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if with_host:
        fields[vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, ""))] = str
        fields[vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT))] = (
            vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
        )
        fields[vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, ""))] = str
    fields[vol.Optional(CONF_PASSWORD)] = SECRET
    fields[vol.Optional(CONF_PRIVATE_KEY)] = MULTILINE
    return vol.Schema(fields)


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    if CONF_HOST in data:
        data[CONF_HOST] = data[CONF_HOST].strip()
    for key in (CONF_PASSWORD, CONF_PRIVATE_KEY):
        value = (data.get(key) or "").strip()
        if value:
            data[key] = value + ("\n" if key == CONF_PRIVATE_KEY else "")
        else:
            data.pop(key, None)
    return data


async def _test(data: dict[str, Any]) -> ModemInfo:
    modem = UlteModem(
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_HOST_KEY],
        port=data.get(CONF_PORT, DEFAULT_PORT),
        password=data.get(CONF_PASSWORD),
        private_key=data.get(CONF_PRIVATE_KEY),
    )
    try:
        return await modem.async_info()
    finally:
        await modem.async_close()


def _error_key(err: Exception) -> str:
    if isinstance(err, UlteHostKeyError):
        return "host_key_changed"
    if isinstance(err, UlteAuthError):
        return "invalid_auth"
    if isinstance(err, UlteConnectionError):
        return "cannot_connect"
    if isinstance(err, UlteError):
        return "modem_error"
    return "unknown"


class UlteSmsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._discovered_host: str | None = None
        self._reauth_displayed_key: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> UlteSmsOptionsFlow:
        return UlteSmsOptionsFlow()

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        mac = format_mac(discovery_info.macaddress)
        await self.async_set_unique_id(mac)
        # Configured device with a new IP: update the host. The host key is
        # still verified, so this cannot redirect the connection elsewhere.
        self._abort_if_unique_id_configured(updates={CONF_HOST: discovery_info.ip})
        self._discovered_host = discovery_info.ip
        self.context["title_placeholders"] = {"host": discovery_info.ip}
        return await self.async_step_user()

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = {CONF_HOST: self._discovered_host or ""}
        if user_input is not None:
            data = _clean(user_input)
            defaults = data
            if not data.get(CONF_PASSWORD) and not data.get(CONF_PRIVATE_KEY):
                errors["base"] = "no_credentials"
            else:
                try:
                    data[CONF_HOST_KEY] = await fetch_host_key(data[CONF_HOST], data[CONF_PORT])
                except UlteConnectionError as err:
                    _LOGGER.debug("Host key fetch failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    self._data = data
                    return await self.async_step_host_key()
        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(defaults),
            errors=errors,
        )

    async def async_step_host_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the host key fingerprint; on confirmation log in and test."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await _test(self._data)
            except Exception as err:  # noqa: BLE001 - mapped to form errors
                errors["base"] = _error_key(err)
                if errors["base"] == "unknown":
                    _LOGGER.exception("Unexpected error during setup")
            else:
                return await self._async_finish(info)
            if self.source == SOURCE_RECONFIGURE:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=self.add_suggested_values_to_schema(
                        _credentials_schema({}), self._data
                    ),
                    errors=errors,
                )
            return self.async_show_form(
                step_id="user", data_schema=_credentials_schema(self._data), errors=errors
            )
        return self.async_show_form(
            step_id="host_key",
            data_schema=vol.Schema({}),
            description_placeholders={
                "host": self._data[CONF_HOST],
                "fingerprint": fingerprint(self._data[CONF_HOST_KEY]),
                "key_type": self._data[CONF_HOST_KEY].split()[0],
            },
        )

    async def _async_finish(self, info: ModemInfo) -> ConfigFlowResult:
        if not info.mac:
            return self.async_abort(reason="no_mac")
        mac = format_mac(info.mac)
        await self.async_set_unique_id(mac)
        if self.source == SOURCE_RECONFIGURE:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data=self._data
            )
        self._abort_if_unique_id_configured(updates=self._data)
        return self.async_create_entry(title=info.board or "UniFi LTE", data=self._data)

    # ------------------------------------------------------------------ #
    # Reauth: host key changed (firmware update, reset) or login rejected
    # ------------------------------------------------------------------ #

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        placeholders = {
            "host": entry.data[CONF_HOST],
            "old_fingerprint": fingerprint(entry.data[CONF_HOST_KEY]),
            "new_fingerprint": "-",
        }
        try:
            new_key = await fetch_host_key(entry.data[CONF_HOST], entry.data.get(CONF_PORT, DEFAULT_PORT))
        except UlteConnectionError:
            new_key = None
            self._reauth_displayed_key = None
            errors["base"] = "cannot_connect"
        else:
            placeholders["new_fingerprint"] = fingerprint(new_key)

        # The key a user approved must be the key used for authentication.
        # If it changed since the form was shown (or the first fetch failed),
        # show the newly observed key and require a fresh submission.
        key_matches_displayed = new_key is not None and new_key == self._reauth_displayed_key
        if new_key is not None and not key_matches_displayed:
            self._reauth_displayed_key = new_key
            if user_input is not None:
                errors["base"] = "host_key_changed"

        if user_input is not None and key_matches_displayed:
            data = {**entry.data, CONF_HOST_KEY: self._reauth_displayed_key}
            new = _clean(user_input)
            if new.get(CONF_PASSWORD) or new.get(CONF_PRIVATE_KEY):
                data.pop(CONF_PASSWORD, None)
                data.pop(CONF_PRIVATE_KEY, None)
                data.update(new)
            try:
                info = await _test(data)
            except Exception as err:  # noqa: BLE001
                errors["base"] = _error_key(err)
            else:
                if info.mac and format_mac(info.mac) != entry.unique_id:
                    return self.async_abort(reason="unique_id_mismatch")
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_credentials_schema({}, with_host=False),
            description_placeholders=placeholders,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # Reconfigure: host, port, credentials
    # ------------------------------------------------------------------ #

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data}
            new = _clean(user_input)
            if new.get(CONF_PASSWORD) or new.get(CONF_PRIVATE_KEY):
                data.pop(CONF_PASSWORD, None)
                data.pop(CONF_PRIVATE_KEY, None)
            data.update(new)
            try:
                key = await fetch_host_key(data[CONF_HOST], data[CONF_PORT])
            except UlteConnectionError:
                errors["base"] = "cannot_connect"
            else:
                self._data = data
                if key == entry.data[CONF_HOST_KEY]:
                    return await self.async_step_host_key({})
                data[CONF_HOST_KEY] = key
                return await self.async_step_host_key()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _credentials_schema({}),
                {k: v for k, v in entry.data.items() if k in (CONF_HOST, CONF_PORT, CONF_USERNAME)},
            ),
            errors=errors,
        )


class UlteSmsOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            recipients: list[str] = []
            for raw in user_input.get(OPT_RECIPIENTS) or []:
                if not str(raw).strip():
                    continue
                try:
                    recipients.append(normalize_number(raw))
                except PduError:
                    errors[OPT_RECIPIENTS] = "invalid_number"
            if not errors:
                return self.async_create_entry(
                    data={
                        OPT_SCAN_INTERVAL: int(user_input[OPT_SCAN_INTERVAL]),
                        OPT_DELETE: user_input[OPT_DELETE],
                        OPT_RECIPIENTS: recipients,
                        OPT_ACCEPT_HOST_KEY: user_input[OPT_ACCEPT_HOST_KEY],
                    }
                )
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_SCAN_INTERVAL,
                    default=options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, step=1,
                        unit_of_measurement="s", mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    OPT_DELETE, default=options.get(OPT_DELETE, DEFAULT_DELETE)
                ): BooleanSelector(),
                vol.Optional(
                    OPT_RECIPIENTS, default=list(options.get(OPT_RECIPIENTS, []))
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEL, multiple=True)),
                vol.Required(
                    OPT_ACCEPT_HOST_KEY,
                    default=options.get(OPT_ACCEPT_HOST_KEY, DEFAULT_ACCEPT_HOST_KEY),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
