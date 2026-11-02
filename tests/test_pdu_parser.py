"""Codec and parser tests (no Home Assistant)."""
from datetime import timedelta

import pytest

from custom_components.ulte_sms.parser import (
    AgentError,
    parse_cmgl,
    parse_info,
    parse_send,
    parse_status,
)
from custom_components.ulte_sms.pdu import (
    GSM_BASIC,
    pack_septets,
    PduError,
    assemble,
    count_parts,
    decode_deliver,
    encode_submit,
    normalize_number,
)

from .helpers import INFO_OUTPUT, SCTS, STATUS_OUTPUT, cmgl, deliver_pdus


def _decode_all(pdus):
    return [decode_deliver(i, p) for i, p in enumerate(pdus)]


@pytest.mark.parametrize(
    ("text", "parts", "encoding"),
    [
        ("Hallo Welt, äöü ß €", 1, "gsm7"),
        ("x" * 160, 1, "gsm7"),
        ("x" * 161, 2, "gsm7"),
        ("[" * 80, 1, "gsm7"),  # extension chars count double
        ("[" * 81, 2, "gsm7"),
        ("Snow ❄️", 1, "ucs2"),
        ("é😀" * 40, 2, "ucs2"),
    ],
)
def test_count_parts(text, parts, encoding):
    assert count_parts(text) == (parts, encoding)


@pytest.mark.parametrize(
    "text",
    [
        "Hallo Welt, äöü ß € {[]}~|^\\",
        "Länger: " + "0123456789" * 40,
        "Emoji 😀 and ❄️ " * 20,
        "€" * 100,  # extension chars must not be split across parts
    ],
)
def test_roundtrip(text):
    parts = _decode_all(deliver_pdus("+491701234567", text))
    assert all(p.sender == "+491701234567" for p in parts)
    messages, waiting = assemble(parts, parts[0].timestamp, timedelta(hours=1))
    assert not waiting
    assert len(messages) == 1
    assert messages[0].text == text
    assert messages[0].complete
    assert messages[0].parts == len(parts)


def test_timestamp():
    part = decode_deliver(0, deliver_pdus("+491701234567", "x")[0])
    assert part.timestamp.isoformat() == "2026-11-20T23:01:55+01:00"


def test_alphanumeric_sender():
    name = pack_septets([GSM_BASIC.index(c) for c in "Telekom"]).hex().upper()
    text = pack_septets([GSM_BASIC.index(c) for c in "Test"]).hex().upper()
    # SMSC +4917..., fo 04, OA len 14 semi-octets, TOA D0 (alphanumeric)
    pdu = "07919471060040F304" + "0ED0" + name + "0000" + SCTS + "04" + text
    part = decode_deliver(3, pdu)
    assert part.sender == "Telekom"
    assert part.text == "Test"


def test_status_report_is_skipped():
    # SMS-STATUS-REPORT (fo & 3 == 2)
    assert decode_deliver(0, "000206" + "00" * 20) is None


def test_malformed():
    with pytest.raises(PduError):
        decode_deliver(0, "0004")


def test_incomplete_multipart_waits_then_delivers():
    parts = _decode_all(deliver_pdus("+491701234567", "y" * 400))
    assert len(parts) == 3
    missing = [parts[0], parts[2]]
    now = parts[0].timestamp + timedelta(minutes=5)
    messages, waiting = assemble(missing, now, timedelta(hours=1))
    assert messages == [] and len(waiting) == 2
    messages, waiting = assemble(missing, now + timedelta(hours=1), timedelta(hours=1))
    assert not waiting
    assert messages[0].complete is False
    assert messages[0].indices == [0, 2]


def test_message_keys_differ():
    a = _decode_all(deliver_pdus("+491701234567", "a"))
    b = _decode_all(deliver_pdus("+491701234567", "b"))
    ma, _ = assemble(a, a[0].timestamp, timedelta(hours=1))
    mb, _ = assemble(b, b[0].timestamp, timedelta(hours=1))
    assert ma[0].key != mb[0].key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("+49 170 1234567", "+491701234567"), ("0049-170/1234567", "+491701234567"),
     ("0170 1234567", "01701234567")],
)
def test_normalize_number(raw, expected):
    assert normalize_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "+49 170 12 34 56 78 90 12 34 56 78"])
def test_normalize_number_invalid(raw):
    with pytest.raises(PduError):
        normalize_number(raw)


def test_too_long():
    with pytest.raises(PduError):
        encode_submit("+491701234567", "x" * 2000, 1)


def test_parse_status():
    s = parse_status(STATUS_OUTPUT)
    assert s.rssi == -71
    assert s.rsrp == -95
    assert s.rsrq == -8.0
    assert s.sinr == 19.0
    assert s.operator == "Telekom.de"
    assert s.access_technology == "LTE"
    assert s.registration == "home"
    assert s.system_mode == "LTE"
    assert s.band == "B8"
    assert s.bandwidth == "10 MHz"
    assert s.temperature == 40
    assert s.cell_id == "01969905"
    assert s.tac == "9503"
    assert (s.sms_used, s.sms_total) == (2, 20)


def test_parse_status_without_gstatus():
    out = STATUS_OUTPUT.split("### AT!GSTATUS?")[0]
    s = parse_status(out)
    assert s.rssi == -69  # from +CSQ: 22
    assert s.rsrp is None and s.band is None


def test_parse_info():
    info = parse_info(INFO_OUTPUT)
    assert info.mac == "78:45:58:39:e4:84"
    assert info.model == "WP7607"
    assert info.board == "U-LTE-Pro-EU"
    assert info.board_short == "ULTEPEU"
    assert info.own_number == "+4915100000000"
    assert info.firmware == "IL.7.0.110"
    assert info.agent_version == 1


def test_parse_cmgl():
    pdus = deliver_pdus("+491701234567", "a") + deliver_pdus("+491701234567", "b")
    assert parse_cmgl(cmgl(pdus, start=4)) == [(4, pdus[0]), (5, pdus[1])]
    assert parse_cmgl("### AT+CMGL=4\nOK") == []
    with pytest.raises(AgentError):
        parse_cmgl("### AT+CMGL=4\n+CMS ERROR: 310")
    with pytest.raises(AgentError):
        parse_cmgl("### AGENT 1")


def test_parse_send():
    assert parse_send("### AT+CMGS=20\n+CMGS: 12\nOK\n### AT+CMGS=20\n+CMGS: 13\nOK") == [12, 13]
    with pytest.raises(AgentError, match="CMS ERROR: 500"):
        parse_send("### AT+CMGS=20\n+CMS ERROR: 500")
    with pytest.raises(AgentError, match="no prompt"):
        parse_send("### AT+CMGS=20\nERROR: no prompt from modem")
