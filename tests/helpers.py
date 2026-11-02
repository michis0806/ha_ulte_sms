"""Test helpers: build SMS-DELIVER PDUs and agent output like the device."""
from __future__ import annotations

import asyncssh

from custom_components.ulte_sms.pdu import encode_submit

SCTS = "62110232105540"  # 2026-11-20 23:01:55 +01:00 (BCD, nibble-swapped)


def host_key() -> str:
    key = asyncssh.generate_private_key("ssh-ed25519")
    return key.export_public_key("openssh").decode().strip()


def deliver_pdus(sender: str, text: str, ref: int = 7, scts: str = SCTS) -> list[str]:
    """Turn our own SMS-SUBMIT encoding into SMS-DELIVER PDUs from ``sender``."""
    result = []
    sender_da = encode_submit(sender, "x", 0)[0][1][6:]  # "00" fo mr | DA...
    sender_da = sender_da[: 4 + (int(sender_da[:2], 16) + 1) // 2 * 2]
    for _len, hexdata in encode_submit("+4900", text, ref):
        tpdu = hexdata[2:]
        fo = int(tpdu[:2], 16)
        da_digits = int(tpdu[4:6], 16)
        p = 8 + (da_digits + 1) // 2 * 2
        pid_dcs = tpdu[p:p + 4]
        rest = tpdu[p + 4:]
        deliver_fo = 0x04 | (fo & 0x40)
        result.append("00%02X%s%s%s%s" % (deliver_fo, sender_da, pid_dcs, scts, rest))
    return result


def cmgl(pdus: list[str], start: int = 0) -> str:
    lines = ["### AT+CMGL=4"]
    for i, pdu in enumerate(pdus, start):
        lines.append(f"+CMGL: {i},1,,{len(pdu) // 2 - 1}")
        lines.append(pdu)
    lines.append("OK")
    return "\n".join(lines)


STATUS_OUTPUT = """### AGENT 1
### AT+CSQ
+CSQ: 22,99
OK
### AT+COPS?
+COPS: 0,0,"Telekom.de",7
OK
### AT+CEREG?
+CEREG: 0,1
OK
### AT+CREG?
+CREG: 0,1
OK
### AT+CPMS="SM","SM","SM"
+CPMS: 2,20,2,20,2,20
OK
### AT!GSTATUS?
!GSTATUS:
Current Time:  1247566\t\tTemperature: 40
Modem Mitigate Level: 0\t\tModemProc Mitigate Level: 0\t
Reset Counter: 1\t\tMode:        ONLINE
System mode:   LTE        \tPS state:    Attached
IMS reg state: NOT REGISTERED  \tIMS mode:    Normal
IMS Srv State: NO SMS,NO VoIP
LTE band:      B8     \t\tLTE bw:      10 MHz
LTE Rx chan:   3749\t\tLTE Tx chan: 21749
LTE CA state:  INACTIVE
EMM state:     Registered     \tNormal Service
RRC state:     RRC Connected
PCC RxM RSSI:  -71\t\tRSRP (dBm):  -95
PCC RxD RSSI:  -72\t\tRSRP (dBm):  -98
Tx Power:      --\t\tTAC:         9503 (38147)
RSRQ (dB):     -8\t\tCell ID:     01969905 (26646789)
SINR (dB):     19.0
OK
"""

INFO_OUTPUT = """### AGENT 1
### SYSTEM
board.name=U-LTE-Pro-EU
board.shortname=ULTEPEU
board.hwaddr=78455839E484
firmware=IL.7.0.110
### ATI
Manufacturer: Sierra Wireless, Incorporated
Model: WP7607
Revision: SWI9X07Y_02.37.03.05 52ddef jenkins 2021/12/22 04:24:24
IMEI: 359779080335587
IMEI SV: 16
FSN: V3019686781610
+GCAP: +CGSM
OK
### AT+CNUM
+CNUM: "Eigene Rufnummer","+4915100000000",145
OK
"""
