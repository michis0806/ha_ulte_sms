"""Config flow tests."""
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ulte_sms.const import (
    CONF_HOST_KEY,
    CONF_PRIVATE_KEY,
    DOMAIN,
    OPT_ACCEPT_HOST_KEY,
    OPT_DELETE,
    OPT_RECIPIENTS,
    OPT_SCAN_INTERVAL,
)
from custom_components.ulte_sms.modem import UlteAuthError, UlteConnectionError
from custom_components.ulte_sms.parser import parse_info

from .helpers import INFO_OUTPUT, host_key

KEY = host_key()
MAC = "78:45:58:39:e4:84"
INPUT = {CONF_HOST: "192.168.33.239", CONF_PORT: 22, CONF_USERNAME: "admin",
         CONF_PASSWORD: "secret"}


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.ulte_sms.async_setup_entry", return_value=True):
        yield


def _patch(fetch=None, test=None):
    return (
        patch("custom_components.ulte_sms.config_flow.fetch_host_key",
              AsyncMock(return_value=KEY, side_effect=fetch)),
        patch("custom_components.ulte_sms.config_flow._test",
              AsyncMock(return_value=parse_info(INFO_OUTPUT), side_effect=test)),
    )


async def test_user_flow(hass: HomeAssistant) -> None:
    p1, p2 = _patch()
    with p1, p2:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
        assert result["step_id"] == "host_key"
        assert result["description_placeholders"]["fingerprint"].startswith("SHA256:")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "U-LTE-Pro-EU"
    assert result["result"].unique_id == MAC
    assert result["data"][CONF_HOST_KEY] == KEY
    assert result["data"][CONF_PASSWORD] == "secret"
    assert CONF_PRIVATE_KEY not in result["data"]


async def test_user_flow_errors(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**INPUT, CONF_PASSWORD: "  "})
    assert result["errors"] == {"base": "no_credentials"}

    p1, p2 = _patch(fetch=UlteConnectionError("x"))
    with p1, p2:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    assert result["errors"] == {"base": "cannot_connect"}

    p1, p2 = _patch(test=UlteAuthError("x"))
    with p1, p2:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}

    p1, p2 = _patch()
    with p1, p2:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_dhcp_updates_host(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC,
                            data={**INPUT, CONF_HOST: "192.168.33.243", CONF_HOST_KEY: KEY})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP},
        data=DhcpServiceInfo(ip="192.168.33.239", hostname="u-lte-pro",
                             macaddress="78455839e484"))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.33.239"


async def test_dhcp_new_device(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP},
        data=DhcpServiceInfo(ip="192.168.33.50", hostname="u-lte-pro",
                             macaddress="78455839e484"))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_reauth_new_host_key(hass: HomeAssistant) -> None:
    old = host_key()
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data={**INPUT, CONF_HOST_KEY: old})
    entry.add_to_hass(hass)
    p1, p2 = _patch()
    with p1, p2:
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        assert result["description_placeholders"]["old_fingerprint"] != \
            result["description_placeholders"]["new_fingerprint"]
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_HOST_KEY] == KEY
    assert entry.data[CONF_PASSWORD] == "secret"


async def test_reconfigure_same_key(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data={**INPUT, CONF_HOST_KEY: KEY})
    entry.add_to_hass(hass)
    p1, p2 = _patch()
    with p1, p2:
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "192.168.33.240", CONF_PORT: 22, CONF_USERNAME: "admin"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "192.168.33.240"
    assert entry.data[CONF_PASSWORD] == "secret"


async def test_options(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data={**INPUT, CONF_HOST_KEY: KEY})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {OPT_SCAN_INTERVAL: 30, OPT_DELETE: False, OPT_RECIPIENTS: ["0049 170 1234567"],
         OPT_ACCEPT_HOST_KEY: True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {OPT_SCAN_INTERVAL: 30, OPT_DELETE: False,
                             OPT_RECIPIENTS: ["+491701234567"], OPT_ACCEPT_HOST_KEY: True}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {OPT_SCAN_INTERVAL: 30, OPT_DELETE: True, OPT_RECIPIENTS: ["abc"],
         OPT_ACCEPT_HOST_KEY: False})
    assert result["errors"] == {OPT_RECIPIENTS: "invalid_number"}
