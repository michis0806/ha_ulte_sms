"""Diagnostics export redaction tests."""
from types import SimpleNamespace

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
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
from custom_components.ulte_sms.diagnostics import async_get_config_entry_diagnostics
from custom_components.ulte_sms.parser import ModemInfo


async def test_diagnostics_redact_credentials_and_default_recipients(
    hass: HomeAssistant,
) -> None:
    recipient = "+491701234567"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="78:45:58:39:e4:84",
        data={
            CONF_HOST: "192.168.33.239",
            CONF_PORT: 22,
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "device-password",
            CONF_PRIVATE_KEY: "private-key-material",
            CONF_HOST_KEY: "ssh-ed25519 AAAA",
        },
        options={
            OPT_SCAN_INTERVAL: 45,
            OPT_DELETE: False,
            OPT_RECIPIENTS: [recipient],
            OPT_ACCEPT_HOST_KEY: True,
        },
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        info=ModemInfo(imei="359779080335587", own_number="+4915111122233"),
        coordinator=SimpleNamespace(
            data=None,
            last_update_success=True,
            waiting_parts=0,
            received_total=2,
            last_delete_error=None,
        ),
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"][CONF_PASSWORD] == "**REDACTED**"
    assert diagnostics["entry"][CONF_PRIVATE_KEY] == "**REDACTED**"
    assert diagnostics["options"][OPT_RECIPIENTS] == "**REDACTED**"
    assert diagnostics["options"][OPT_SCAN_INTERVAL] == 45
    assert diagnostics["options"][OPT_DELETE] is False
    assert diagnostics["options"][OPT_ACCEPT_HOST_KEY] is True
    assert diagnostics["info"]["imei"] == "**REDACTED**"
    assert diagnostics["info"]["own_number"] == "**REDACTED**"
    assert recipient not in repr(diagnostics)
