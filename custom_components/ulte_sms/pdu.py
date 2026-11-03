"""SMS PDU codec (3GPP TS 23.040 / 23.038) for the modem's PDU mode.

Pure Python, no Home Assistant imports, so it can be tested on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import re

# GSM 03.38 basic alphabet (0x1B = escape to the extension table)
GSM_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM_EXT = {0x0A: "\f", 0x14: "^", 0x28: "{", 0x29: "}", 0x2F: "\\",
           0x3C: "[", 0x3D: "~", 0x3E: "]", 0x40: "|", 0x65: "€"}
GSM_BASIC_REV = {c: i for i, c in enumerate(GSM_BASIC) if c != "\x1b"}
GSM_EXT_REV = {c: i for i, c in GSM_EXT.items()}

# Single-part limits; multipart parts lose space to the 6-byte UDH.
GSM7_SINGLE, GSM7_MULTI = 160, 153
UCS2_SINGLE, UCS2_MULTI = 140, 134  # bytes
MAX_PARTS = 10
# SCTS is the only occurrence hint in SMS-DELIVER beyond a reusable reference.
# A short bounded window favors incomplete delivery over mixing delayed/reused
# references. It is a heuristic, not a protocol-level message identifier.
MULTIPART_SCTS_WINDOW = timedelta(seconds=60)


class PduError(ValueError):
    """Invalid input for encoding or an undecodable PDU."""


@dataclass(slots=True)
class SmsPart:
    """One SMS-DELIVER PDU as stored on the SIM."""

    index: int
    pdu: str
    sender: str
    timestamp: datetime
    text: str
    ref: int | None = None
    total: int | None = None
    seq: int | None = None


@dataclass(slots=True)
class SmsMessage:
    """An incoming SMS, multipart messages already joined."""

    sender: str
    timestamp: datetime
    text: str
    indices: list[int]
    parts: int = 1
    complete: bool = True
    key: str = field(default="")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def septets_to_text(septets: list[int]) -> str:
    out: list[str] = []
    esc = False
    for s in septets:
        if esc:
            out.append(GSM_EXT.get(s, " "))
            esc = False
        elif s == 0x1B:
            esc = True
        else:
            out.append(GSM_BASIC[s])
    return "".join(out)


def unpack_septets(data: bytes, count: int) -> list[int]:
    septets: list[int] = []
    bits = acc = 0
    for byte in data:
        acc |= byte << bits
        bits += 8
        while bits >= 7 and len(septets) < count:
            septets.append(acc & 0x7F)
            acc >>= 7
            bits -= 7
    return septets[:count]


def pack_septets(septets: list[int], fill_bits: int = 0) -> bytes:
    acc, bits = 0, fill_bits
    out = bytearray()
    for s in septets:
        acc |= (s & 0x7F) << bits
        bits += 7
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    if bits:
        out.append(acc & 0xFF)
    return bytes(out)


def _swap_nibbles(data: str) -> str:
    return "".join(b + a for a, b in zip(data[::2], data[1::2]))


def _decode_address(digits_hex: str, toa: int) -> str:
    if (toa & 0x70) == 0x50:  # alphanumeric sender, GSM7 packed
        raw = bytes.fromhex(digits_hex)
        return septets_to_text(unpack_septets(raw, len(raw) * 8 // 7)).rstrip("@")
    num = _swap_nibbles(digits_hex).rstrip("Ff")
    return ("+" + num) if (toa & 0x70) == 0x10 else num


def _decode_scts(b: bytes) -> datetime:
    def bcd(x: int) -> int:
        return (x & 0x0F) * 10 + (x >> 4)

    yy, mo, dd, hh, mi, ss = (bcd(x) for x in b[:6])
    quarters = bcd(b[6] & 0xF7)
    offset = timedelta(minutes=15 * quarters)
    if b[6] & 0x08:
        offset = -offset
    return datetime(2000 + yy, mo, dd, hh, mi, ss, tzinfo=timezone(offset))


def normalize_number(number: str) -> str:
    """Strip separators, turn 00 into +. Raises PduError if not a number."""
    num = re.sub(r"[\s/().-]", "", str(number))
    if num.startswith("00"):
        num = "+" + num[2:]
    if not re.fullmatch(r"\+?\d{2,20}", num):
        raise PduError(f"Invalid phone number: {number!r}")
    return num


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #


def decode_deliver(index: int, pdu_hex: str) -> SmsPart | None:
    """Decode an SMS-DELIVER PDU (with leading SMSC field).

    Returns None for other PDU types (e.g. status reports). Raises PduError
    if the PDU is malformed.
    """
    try:
        b = bytes.fromhex(pdu_hex)
        p = 0
        smsc_len = b[p]
        p += 1 + smsc_len
        fo = b[p]
        p += 1
        if fo & 0x03 != 0x00:
            return None
        udhi = bool(fo & 0x40)
        addr_len = b[p]
        toa = b[p + 1]
        p += 2
        addr_bytes = (addr_len + 1) // 2
        sender = _decode_address(b[p:p + addr_bytes].hex().upper(), toa)
        p += addr_bytes
        p += 1  # PID
        dcs = b[p]
        p += 1
        timestamp = _decode_scts(b[p:p + 7])
        p += 7
        udl = b[p]
        p += 1
        ud = b[p:]
    except (IndexError, ValueError) as err:
        raise PduError(f"Malformed PDU at index {index}: {err}") from err

    # Character set from the DCS (simplified, 3GPP TS 23.038 section 4)
    group = dcs >> 4
    if group <= 0x7:
        alphabet = (dcs >> 2) & 3
    elif group == 0xE:
        alphabet = 2
    elif group == 0xF:
        alphabet = (dcs >> 2) & 1
    else:  # 0xC/0xD message waiting groups use GSM7
        alphabet = 0

    # UDL counts septets for GSM7 and octets for the other alphabets. Slice to
    # the declared payload so trailing bytes cannot satisfy a truncated UDH.
    ud_bytes = (udl * 7 + 7) // 8 if alphabet not in (1, 2) else udl
    if len(ud) < ud_bytes:
        raise PduError(f"Truncated user data at index {index}")
    ud = ud[:ud_bytes]

    ref = total = seq = None
    udh_len = 0
    if udhi:
        if not ud:
            raise PduError(f"Missing UDH at index {index}")
        udh_len = ud[0] + 1
        if udh_len > len(ud) or (
            alphabet not in (1, 2) and (udh_len * 8 + 6) // 7 > udl
        ):
            raise PduError(f"Truncated UDH at index {index}")
        i = 1
        while i < udh_len:
            if i + 2 > udh_len:
                raise PduError(f"Truncated UDH information element at index {index}")
            iei, ielen = ud[i], ud[i + 1]
            if i + 2 + ielen > udh_len:
                raise PduError(f"Truncated UDH information element at index {index}")
            data = ud[i + 2:i + 2 + ielen]
            if (iei == 0x00 and ielen != 3) or (iei == 0x08 and ielen != 4):
                raise PduError(f"Invalid concatenation UDH at index {index}")
            if iei == 0x00 and ielen == 3:
                ref, total, seq = data[0], data[1], data[2]
            elif iei == 0x08 and ielen == 4:
                ref = (data[0] << 8) | data[1]
                total, seq = data[2], data[3]
            i += 2 + ielen

    if alphabet == 2:
        text = ud[udh_len:].decode("utf-16-be", "replace")
    elif alphabet == 1:
        text = ud[udh_len:].decode("latin-1", "replace")
    else:
        septets = unpack_septets(ud, udl)
        skip = (udh_len * 8 + 6) // 7
        text = septets_to_text(septets[skip:])

    if total is not None and (total < 2 or not seq or seq > total):
        ref = total = seq = None  # broken concatenation header: treat as single

    return SmsPart(index, pdu_hex.upper(), sender, timestamp, text, ref, total, seq)


def _key(parts: list[SmsPart]) -> str:
    return hashlib.sha1("|".join(p.pdu for p in parts).encode()).hexdigest()[:20]


def assemble(
    parts: list[SmsPart], now: datetime, incomplete_after: timedelta
) -> tuple[list[SmsMessage], list[SmsPart]]:
    """Join multipart messages.

    Returns (messages, waiting parts). Incomplete multipart messages are kept
    back until their oldest part is older than ``incomplete_after``; then
    they are delivered with ``complete=False``.
    """
    messages: list[SmsMessage] = []
    waiting: list[SmsPart] = []
    groups: dict[tuple[str, int, int], list[SmsPart]] = {}
    for part in parts:
        if part.ref is None:
            messages.append(SmsMessage(part.sender, part.timestamp, part.text,
                                       [part.index], key=_key([part])))
        else:
            groups.setdefault((part.sender, part.ref, part.total or 0), []).append(part)

    for (sender, _ref, total), group in groups.items():
        # SIM slots are reused and need not be in arrival order. Bound each
        # occurrence by its oldest SCTS rather than chaining adjacent times.
        windows: list[list[SmsPart]] = []
        for part in sorted(group, key=lambda p: (p.timestamp, p.index)):
            if not windows or part.timestamp - windows[-1][0].timestamp > MULTIPART_SCTS_WINDOW:
                windows.append([])
            windows[-1].append(part)

        for window in windows:
            # Only byte-identical PDUs are duplicates. Keep every SIM index,
            # including while waiting, so no unconsumed fragment is lost.
            duplicates: dict[str, list[SmsPart]] = {}
            for part in window:
                duplicates.setdefault(part.pdu, []).append(part)
            unique = list(duplicates.values())
            sequences = [copies[0].seq for copies in unique]
            ambiguous = len(set(sequences)) != len(sequences)
            # Conflicting sequence slots cannot be assigned safely to their
            # occurrences. Deliver each distinct fragment incomplete instead.
            candidates = [[copies] for copies in unique] if ambiguous else [unique]
            for candidate in candidates:
                ordered_copies = sorted(candidate, key=lambda copies: copies[0].seq or 0)
                ordered = [copies[0] for copies in ordered_copies]
                consumed = [part for copies in ordered_copies for part in copies]
                complete = not ambiguous and {p.seq for p in ordered} == set(range(1, total + 1))
                oldest = min(p.timestamp for p in ordered)
                if not complete and now - oldest < incomplete_after:
                    waiting.extend(consumed)
                    continue
                messages.append(SmsMessage(
                    sender=sender,
                    timestamp=ordered[0].timestamp,
                    text="".join(p.text for p in ordered),
                    indices=sorted(p.index for p in consumed),
                    parts=total,
                    complete=complete,
                    key=_key(ordered),
                ))

    messages.sort(key=lambda m: m.indices[0])
    return messages, waiting


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


def _text_to_septet_units(text: str) -> list[list[int]] | None:
    units: list[list[int]] = []
    for c in text:
        if c in GSM_BASIC_REV:
            units.append([GSM_BASIC_REV[c]])
        elif c in GSM_EXT_REV:
            units.append([0x1B, GSM_EXT_REV[c]])
        else:
            return None
    return units


def _ucs2_units(text: str) -> list[bytes]:
    # Characters outside the BMP become surrogate pairs and must not be split.
    return [c.encode("utf-16-be") for c in text]


def _split(units: list, limit: int) -> list[list]:
    chunks: list[list] = []
    cur: list = []
    cur_len = 0
    for u in units:
        if cur_len + len(u) > limit:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(u)
        cur_len += len(u)
    if cur:
        chunks.append(cur)
    return chunks


def _encode_address(number: str) -> str:
    num = normalize_number(number)
    toa = 0x91 if num.startswith("+") else 0x81
    digits = num.lstrip("+")
    padded = digits + "F" if len(digits) % 2 else digits
    return "%02X%02X%s" % (len(digits), toa, _swap_nibbles(padded))


def count_parts(text: str) -> tuple[int, str]:
    """Number of SMS parts and the encoding ("gsm7" or "ucs2") for a text."""
    units = _text_to_septet_units(text)
    if units is not None:
        n = sum(len(u) for u in units)
        return (1 if n <= GSM7_SINGLE else len(_split(units, GSM7_MULTI))), "gsm7"
    ucs = _ucs2_units(text)
    n = sum(len(u) for u in ucs)
    return (1 if n <= UCS2_SINGLE else len(_split(ucs, UCS2_MULTI))), "ucs2"


def encode_submit(number: str, text: str, ref: int) -> list[tuple[int, str]]:
    """Encode an SMS-SUBMIT. Returns [(tpdu_length, hex incl. empty SMSC)].

    ``ref`` is the concatenation reference (0-255) for multipart messages.
    """
    if not text:
        raise PduError("Empty message")
    da = _encode_address(number)
    septet_units = _text_to_septet_units(text)
    if septet_units is not None:
        total = sum(len(u) for u in septet_units)
        chunks = [septet_units] if total <= GSM7_SINGLE else _split(septet_units, GSM7_MULTI)
    else:
        ucs = _ucs2_units(text)
        total = sum(len(u) for u in ucs)
        chunks = [ucs] if total <= UCS2_SINGLE else _split(ucs, UCS2_MULTI)
    if len(chunks) > MAX_PARTS:
        raise PduError(f"Message too long ({len(chunks)} parts, max. {MAX_PARTS})")

    multi = len(chunks) > 1
    result: list[tuple[int, str]] = []
    for n, chunk in enumerate(chunks, 1):
        udh = bytes([0x05, 0x00, 0x03, ref & 0xFF, len(chunks), n]) if multi else b""
        fo = 0x01 | (0x40 if multi else 0x00)  # SMS-SUBMIT, no validity period
        if septet_units is not None:
            septets = [s for u in chunk for s in u]
            if multi:
                udh_septets = (len(udh) * 8 + 6) // 7
                fill = udh_septets * 7 - len(udh) * 8
                ud = udh + pack_septets(septets, fill)
                udl = udh_septets + len(septets)
            else:
                ud = pack_septets(septets)
                udl = len(septets)
            dcs = 0x00
        else:
            ud = udh + b"".join(chunk)
            udl = len(ud)
            dcs = 0x08
        tpdu = "%02X00%s00%02X%02X%s" % (fo, da, dcs, udl, ud.hex().upper())
        # The modem expects the SMSC length byte (00 = default SMSC) in front;
        # AT+CMGS counts only the TPDU.
        result.append((len(tpdu) // 2, "00" + tpdu))
    return result
