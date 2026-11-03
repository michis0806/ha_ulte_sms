"""Setup, polling, events, deletion, host key handling and sending."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntryState, SOURCE_REAUTH
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.ulte_sms.const import (
    CONF_HOST_KEY,
    DOMAIN,
    EVENT_RECEIVED,
    OPT_ACCEPT_HOST_KEY,
    OPT_DELETE,
    OPT_RECIPIENTS,
)
from custom_components.ulte_sms.modem import (
    UlteDeleteError, UlteError, UlteHostKeyError, UlteModemError,
)
from custom_components.ulte_sms.parser import parse_info, parse_status
from custom_components.ulte_sms.pdu import assemble, decode_deliver

from .helpers import INFO_OUTPUT, STATUS_OUTPUT, deliver_pdus, host_key

KEY = host_key()
MAC = "78:45:58:39:e4:84"
DATA = {CONF_HOST: "192.168.33.239", CONF_PORT: 22, CONF_USERNAME: "admin",
        CONF_PASSWORD: "secret", CONF_HOST_KEY: KEY}


class FakeModem:
    """Stands in for UlteModem; SIM content in ``self.sim`` {index: pdu}."""

    def __init__(self, *args, **kwargs) -> None:
        self.host = DATA[CONF_HOST]
        self.port = 22
        self.sim: dict[int, str] = {}
        self.deleted: list[list[int]] = []
        self.sent: list[tuple[str, str]] = []
        self.host_key_error = 0
        self.fail_send: set[str] = set()
        self.closed = False
        self.new_key: str | None = None

    async def async_info(self):
        if self.host_key_error:
            self.host_key_error -= 1
            raise UlteHostKeyError("changed")
        return parse_info(INFO_OUTPUT)

    async def async_poll(self):
        if self.host_key_error:
            self.host_key_error -= 1
            raise UlteHostKeyError("changed")
        parts = [decode_deliver(i, p) for i, p in sorted(self.sim.items())]
        return parse_status(STATUS_OUTPUT), [p for p in parts if p], []

    async def async_delete(self, indices):
        self.deleted.append(list(indices))
        for i in indices:
            self.sim.pop(i, None)

    async def async_send(self, number, text):
        if number in self.fail_send:
            raise UlteModemError("+CMS ERROR: 500")
        self.sent.append((number, text))
        return [1]

    def set_host_key(self, key):
        self.new_key = key

    async def async_close(self):
        self.closed = True

    async def async_persist_host_key(self):
        self.persist_calls = getattr(self, "persist_calls", 0) + 1
        return {"dropbear_rsa_host_key": self.persist_result}

    persist_result = "persistent"


@pytest.fixture
def modem():
    fake = FakeModem()
    with patch("custom_components.ulte_sms.UlteModem", return_value=fake):
        yield fake


async def _setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="U-LTE-Pro-EU",
                            data=DATA, options=options or {})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _poll(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()


async def test_setup_and_sensors(hass: HomeAssistant, modem: FakeModem) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.u_lte_pro_eu_signal_strength_rsrp").state == "-95"
    assert hass.states.get("sensor.u_lte_pro_eu_operator").state == "Telekom.de"
    assert hass.states.get("sensor.u_lte_pro_eu_registration").state == "home"
    sim = hass.states.get("sensor.u_lte_pro_eu_sms_on_sim")
    assert sim.state == "2" and sim.attributes["capacity"] == 20
    assert hass.states.get("event.u_lte_pro_eu_incoming_sms") is not None
    assert hass.states.get("notify.u_lte_pro_eu_sms") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert modem.closed


async def test_receive_fires_once_and_deletes(hass: HomeAssistant, modem: FakeModem) -> None:
    events = async_capture_events(hass, EVENT_RECEIVED)
    entry = await _setup(hass)
    long_text = "Teil " * 50
    pdus = deliver_pdus("+491701234567", "Hallo") + deliver_pdus("+491709999999", long_text)
    modem.sim = dict(enumerate(pdus, start=3))
    await _poll(hass)

    assert [(e.data["sender"], e.data["text"]) for e in events] == [
        ("+491701234567", "Hallo"), ("+491709999999", long_text)]
    assert events[1].data["parts"] == 2 and events[1].data["complete"] is True
    assert events[0].data["config_entry_id"] == entry.entry_id
    assert modem.deleted == [[3, 4, 5]]
    state = hass.states.get("event.u_lte_pro_eu_incoming_sms")
    assert state.attributes["sender"] == "+491709999999"
    assert state.attributes["event_type"] == "received"

    await _poll(hass)
    assert len(events) == 2


async def test_receive_without_delete(hass: HomeAssistant, modem: FakeModem) -> None:
    events = async_capture_events(hass, EVENT_RECEIVED)
    await _setup(hass, {OPT_DELETE: False})
    modem.sim = {0: deliver_pdus("+491701234567", "Hallo")[0]}
    await _poll(hass)
    await _poll(hass)
    assert len(events) == 1
    assert modem.deleted == []


async def test_partial_delete_survives_reload_and_retries(
    hass: HomeAssistant, modem: FakeModem,
) -> None:
    """Delivered multipart remnants are deletion work, even before their expiry."""
    events = async_capture_events(hass, EVENT_RECEIVED)
    entry = await _setup(hass)
    pdus = deliver_pdus("+491701234567", "x" * 300)
    modem.sim = dict(enumerate(pdus))
    original_poll = modem.async_poll

    async def poll():
        status, parts, other = await original_poll()
        status.sms_used = len(modem.sim)
        return status, parts, other

    async def delete(indices):
        modem.deleted.append(list(indices))
        if len(modem.deleted) == 1:
            modem.sim.pop(0)
            raise UlteDeleteError("+CMS ERROR: 500", [0])
        if len(modem.deleted) == 2:
            raise UlteError("Connection lost; deletion result unknown")
        for index in indices:
            modem.sim.pop(index, None)

    sent = decode_deliver(0, pdus[0]).timestamp
    with (
        patch.object(modem, "async_poll", side_effect=poll),
        patch.object(modem, "async_delete", side_effect=delete),
        patch("custom_components.ulte_sms.coordinator.dt_util.utcnow",
              return_value=sent + timedelta(minutes=1)),
    ):
        await _poll(hass)
        coordinator = entry.runtime_data.coordinator
        assert len(events) == 1 and events[0].data["complete"] is True
        assert coordinator.data.sms_used == 1
        assert coordinator.last_delete_error == "+CMS ERROR: 500"
        stored = await coordinator._store.async_load()
        assert len(stored["delivered_parts"]) == 2
        assert all(len(key) == 64 for key in stored["delivered_parts"])
        assert set(stored) == {"seen", "delivered_parts"}

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinator
        assert len(events) == 1
        assert modem.deleted == [[0, 1], [1]]
        assert coordinator.waiting_parts == 0
        assert coordinator.data.sms_used == 1
        assert coordinator.last_delete_error == "Connection lost; deletion result unknown"
        assert len(coordinator._delivered_parts) == 1

        await _poll(hass)
        assert modem.deleted[-1] == [1]
        assert coordinator.data.sms_used == 0
        assert coordinator.last_delete_error is None
        assert len(events) == 1
        # Keep just-deleted fingerprints until the next SIM snapshot confirms
        # their absence, then bound storage to the remaining SIM contents.
        assert len(coordinator._delivered_parts) == 1
        await _poll(hass)
        assert coordinator._delivered_parts == set()
        assert (await coordinator._store.async_load())["delivered_parts"] == []


async def test_delivered_part_index_reused_for_new_pdu(
    hass: HomeAssistant, modem: FakeModem,
) -> None:
    events = async_capture_events(hass, EVENT_RECEIVED)
    entry = await _setup(hass, {OPT_DELETE: False})
    modem.sim = {0: deliver_pdus("+491701234567", "First message")[0]}
    await _poll(hass)
    first_fingerprints = entry.runtime_data.coordinator._delivered_parts.copy()
    modem.sim[0] = deliver_pdus("+491701234567", "Second message")[0]
    await _poll(hass)
    assert [event.data["text"] for event in events] == ["First message", "Second message"]
    assert len(entry.runtime_data.coordinator._delivered_parts) == 1
    assert not first_fingerprints.intersection(entry.runtime_data.coordinator._delivered_parts)


async def test_legacy_seen_keys_seed_delivered_parts(
    hass: HomeAssistant, modem: FakeModem,
) -> None:
    """An upgrade keeps already-delivered complete messages quiet."""
    events = async_capture_events(hass, EVENT_RECEIVED)
    entry = await _setup(hass, {OPT_DELETE: False})
    pdus = deliver_pdus("+491701234567", "x" * 300)
    modem.sim = dict(enumerate(pdus))
    parts = [decode_deliver(index, pdu) for index, pdu in modem.sim.items()]
    messages, _ = assemble(parts, dt_util.utcnow(), timedelta(0))
    await entry.runtime_data.coordinator._store.async_save({"seen": [messages[0].key]})

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert events == []
    assert len(entry.runtime_data.coordinator._delivered_parts) == 2
    modem.sim.pop(0)
    sent = parts[0].timestamp
    with patch("custom_components.ulte_sms.coordinator.dt_util.utcnow",
               return_value=sent + timedelta(days=1)):
        await _poll(hass)
    assert events == []
    assert len(entry.runtime_data.coordinator._delivered_parts) == 1


async def test_incomplete_multipart_waits(hass: HomeAssistant, modem: FakeModem) -> None:
    events = async_capture_events(hass, EVENT_RECEIVED)
    await _setup(hass)
    parts = deliver_pdus("+491701234567", "x" * 300)
    modem.sim = {0: parts[0]}
    # Part timestamps are in 2026-11-20; pin "now" right after that.
    sent = decode_deliver(0, parts[0]).timestamp
    with patch("custom_components.ulte_sms.coordinator.dt_util.utcnow",
               return_value=sent + timedelta(minutes=1)):
        await _poll(hass)
        assert events == [] and modem.deleted == []
        modem.sim[1] = parts[1]
        await _poll(hass)
    assert len(events) == 1 and events[0].data["text"] == "x" * 300


async def test_host_key_change_starts_reauth(hass: HomeAssistant, modem: FakeModem) -> None:
    entry = await _setup(hass)
    modem.host_key_error = 1
    with patch("custom_components.ulte_sms.config_flow.fetch_host_key", return_value=host_key()):
        await _poll(hass)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    assert entry.data[CONF_HOST_KEY] == KEY


async def test_host_key_change_accepted(
    hass: HomeAssistant, modem: FakeModem, issue_registry: ir.IssueRegistry
) -> None:
    new_key = host_key()
    entry = await _setup(hass, {OPT_ACCEPT_HOST_KEY: True})
    modem.host_key_error = 1
    with patch("custom_components.ulte_sms.coordinator.fetch_host_key", return_value=new_key):
        await _poll(hass)
    assert entry.data[CONF_HOST_KEY] == new_key
    assert modem.new_key == new_key
    assert issue_registry.async_get_issue(DOMAIN, f"host_key_accepted_{entry.entry_id}")
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert hass.states.get("sensor.u_lte_pro_eu_operator").state == "Telekom.de"


async def test_host_key_change_at_setup_accepted(hass: HomeAssistant, modem: FakeModem) -> None:
    new_key = host_key()
    modem.host_key_error = 1
    with patch("custom_components.ulte_sms.coordinator.fetch_host_key", return_value=new_key):
        entry = await _setup(hass, {OPT_ACCEPT_HOST_KEY: True})
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_HOST_KEY] == new_key


async def test_send_action(hass: HomeAssistant, modem: FakeModem) -> None:
    await _setup(hass)
    response = await hass.services.async_call(
        DOMAIN, "send_sms",
        {"recipients": ["0170 1234567", "+491701234567", "01701234567"], "message": "Hi"},
        blocking=True, return_response=True)
    assert modem.sent == [("01701234567", "Hi"), ("+491701234567", "Hi")]
    assert [r["success"] for r in response["results"]] == [True, True]

    modem.fail_send = {"+491700000000"}
    with pytest.raises(HomeAssistantError, match="CMS ERROR: 500"):
        await hass.services.async_call(
            DOMAIN, "send_sms", {"recipients": ["+491700000000", "+491711111111"],
                                 "message": "Hi"}, blocking=True)
    assert modem.sent[-1] == ("+491711111111", "Hi")

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "send_sms", {"recipients": ["abc"], "message": "Hi"}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "send_sms", {"recipients": ["+491701234567"], "message": " "},
            blocking=True)


async def test_notify_entity(hass: HomeAssistant, modem: FakeModem) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "notify", "send_message",
            {"entity_id": "notify.u_lte_pro_eu_sms", "message": "x"}, blocking=True)


async def test_notify_entity_recipients(hass: HomeAssistant, modem: FakeModem) -> None:
    await _setup(hass, {OPT_RECIPIENTS: ["+491701234567"]})
    await hass.services.async_call(
        "notify", "send_message",
        {"entity_id": "notify.u_lte_pro_eu_sms", "message": "Strom weg", "title": "USV"},
        blocking=True)
    assert modem.sent == [("+491701234567", "USV\nStrom weg")]


async def test_host_key_repair(
    hass: HomeAssistant, modem: FakeModem, issue_registry: ir.IssueRegistry
) -> None:
    from custom_components.ulte_sms.repairs import async_create_fix_flow

    entry = await _setup(hass)
    issue_id = f"host_key_not_persistent_{entry.entry_id}"
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable
    assert issue.translation_placeholders["files"] == "dropbear_rsa_host_key"

    modem.persist_result = "ram_only"
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["step_id"] == "confirm"
    result = await flow.async_step_confirm({})
    assert result["type"] == "abort" and result["reason"] == "persist_failed"
    assert issue_registry.async_get_issue(DOMAIN, issue_id)

    modem.persist_result = "persistent"
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    await flow.async_step_init()
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    assert modem.persist_calls == 2
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    assert entry.runtime_data.info.host_keys == {"dropbear_rsa_host_key": "persistent"}


async def test_no_issue_when_persistent(
    hass: HomeAssistant, modem: FakeModem, issue_registry: ir.IssueRegistry
) -> None:
    info = parse_info(INFO_OUTPUT.replace("ram_only", "persistent"))
    with patch.object(FakeModem, "async_info", return_value=info):
        entry = await _setup(hass)
    assert issue_registry.async_get_issue(
        DOMAIN, f"host_key_not_persistent_{entry.entry_id}") is None
