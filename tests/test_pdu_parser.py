"""Codec and parser tests (no Home Assistant)."""
from dataclasses import replace
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


@pytest.mark.parametrize(
    ("udl", "payload"),
    [
        (4, "03000307"),  # reported crash: concat IE exceeds its UDH
        (0, ""),  # UDHI without a header
        (6, "05000307"),  # fewer octets than UDL declares
        (3, "050003"),  # UDHL exceeds the declared user data
        (2, "0100"),  # IE missing its length byte
        (5, "0400020702"),  # 8-bit concat IE with an invalid length
        (6, "050804000702"),  # truncated 16-bit concat IE
        (5, "0300030702"),  # IE extends into text outside the UDH
    ],
)
def test_malformed_udh_raises_pdu_error(udl, payload):
    pdu = "00440281210008" + SCTS + f"{udl:02X}" + payload
    with pytest.raises(PduError):
        decode_deliver(7, pdu)


def test_gsm7_udh_must_fit_declared_septets():
    pdu = "00440281210000" + SCTS + "06050003070201"
    with pytest.raises(PduError):
        decode_deliver(7, pdu)


def test_truncated_gsm7_user_data():
    pdu = deliver_pdus("+491701234567", "hello")[0]
    with pytest.raises(PduError):
        decode_deliver(7, pdu[:-2])


def test_unknown_udh_information_element_is_skipped():
    pdu = "00440281210008" + SCTS + "06037001FF0041"
    part = decode_deliver(7, pdu)
    assert part.text == "A"
    assert part.ref is None


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


def test_reused_reference_preserves_both_messages_and_all_indices():
    first = _decode_all(deliver_pdus("+491701234567", "a" * 161))
    second = _decode_all(deliver_pdus("+491701234567", "b" * 161, scts="62110232605540"))
    # SIM slots and input order are unrelated to sequence or message order.
    parts = [replace(first[1], index=1), replace(second[0], index=3),
             replace(first[0], index=20), replace(second[1], index=2)]
    messages, waiting = assemble(parts, second[0].timestamp, timedelta(hours=1))
    assert not waiting
    assert {m.text: m.indices for m in messages} == {"a" * 161: [1, 20], "b" * 161: [2, 3]}
    assert all(m.complete for m in messages)


def test_multipart_can_have_different_service_centre_timestamps():
    first = _decode_all(deliver_pdus("+491701234567", "a" * 161))
    later = _decode_all(deliver_pdus("+491701234567", "a" * 161, scts="62110232106540"))
    messages, waiting = assemble([first[0], later[1]], later[1].timestamp, timedelta(hours=1))
    assert not waiting
    assert len(messages) == 1
    assert messages[0].complete and messages[0].text == "a" * 161


def test_exact_duplicate_parts_keep_every_sim_index():
    parts = _decode_all(deliver_pdus("+491701234567", "a" * 161))
    duplicate = replace(parts[0], index=8)
    messages, waiting = assemble([duplicate, *parts], parts[0].timestamp, timedelta(hours=1))
    assert not waiting
    assert len(messages) == 1
    assert messages[0].text == "a" * 161
    assert messages[0].indices == [0, 1, 8]
    assert messages[0].complete


def test_ambiguous_reference_collision_preserves_fragments_until_timeout():
    first = _decode_all(deliver_pdus("+491701234567", "a" * 161))
    second = _decode_all(deliver_pdus("+491701234567", "b" * 161))
    parts = [first[0], first[1], replace(second[0], index=4), replace(first[0], index=8)]
    now = first[0].timestamp
    messages, waiting = assemble(parts, now, timedelta(hours=1))
    assert not messages
    assert sorted(p.index for p in waiting) == [0, 1, 4, 8]
    messages, waiting = assemble(waiting, now + timedelta(hours=1), timedelta(hours=1))
    assert not waiting
    assert {m.text: m.indices for m in messages} == {
        first[0].text: [0, 8], first[1].text: [1], second[0].text: [4],
    }
    assert len({m.key for m in messages}) == 3
    assert all(not m.complete for m in messages)


def test_separate_incomplete_occurrences_do_not_fill_each_others_gaps():
    first = _decode_all(deliver_pdus("+491701234567", "a" * 161))
    later = _decode_all(deliver_pdus("+491701234567", "b" * 161, scts="62110232605540"))
    parts = [first[0], replace(first[0], index=8), replace(later[1], index=4)]
    now = later[1].timestamp
    messages, waiting = assemble(parts, now, timedelta(hours=1))
    assert not messages
    assert sorted(p.index for p in waiting) == [0, 4, 8]
    messages, waiting = assemble(waiting, now + timedelta(hours=1), timedelta(hours=1))
    assert not waiting
    assert {m.text: m.indices for m in messages} == {first[0].text: [0, 8], later[1].text: [4]}
    assert all(not m.complete for m in messages)


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
    assert info.host_keys == {"dropbear_rsa_host_key": "ram_only"}
    assert info.host_key_volatile
    assert not parse_info(INFO_OUTPUT.replace("ram_only", "persistent")).host_key_volatile


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
