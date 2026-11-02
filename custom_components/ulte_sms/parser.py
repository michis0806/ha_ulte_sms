"""Parse the agent's output (sections of AT responses). No HA imports."""
from __future__ import annotations

from dataclasses import dataclass, field
import re

# 3GPP TS 27.007 +CEREG/+CREG <stat>
REGISTRATION = {
    0: "not_registered",
    1: "home",
    2: "searching",
    3: "denied",
    4: "unknown",
    5: "roaming",
}
# +COPS <AcT>
ACCESS_TECH = {0: "GSM", 2: "UMTS", 3: "EDGE", 4: "HSDPA", 5: "HSUPA",
               6: "HSPA", 7: "LTE", 9: "NB-IoT", 13: "NR"}


@dataclass(slots=True)
class ModemInfo:
    model: str | None = None
    revision: str | None = None
    imei: str | None = None
    own_number: str | None = None
    board: str | None = None
    board_short: str | None = None
    mac: str | None = None
    firmware: str | None = None
    agent_version: int | None = None


@dataclass(slots=True)
class ModemStatus:
    rssi: int | None = None          # dBm
    rsrp: int | None = None          # dBm
    rsrq: float | None = None        # dB
    sinr: float | None = None        # dB
    operator: str | None = None
    access_technology: str | None = None
    registration: str | None = None
    system_mode: str | None = None
    band: str | None = None
    bandwidth: str | None = None
    temperature: int | None = None   # °C
    cell_id: str | None = None
    tac: str | None = None
    sms_used: int | None = None
    sms_total: int | None = None
    extra: dict[str, str] = field(default_factory=dict)


class AgentError(Exception):
    """The agent reported an error."""


def split_sections(output: str) -> list[tuple[str, list[str]]]:
    """[(command, response lines)] in order; lines before the first marker
    are returned under the key ''."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for raw in output.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("### "):
            sections.append((line[4:].strip(), []))
        elif line.strip():
            sections[-1][1].append(line)
    return sections


def section(sections: list[tuple[str, list[str]]], command: str) -> list[str] | None:
    for cmd, lines in sections:
        if cmd == command:
            return lines
    return None


def agent_version(sections: list[tuple[str, list[str]]]) -> int | None:
    for cmd, _lines in sections:
        if cmd.startswith("AGENT "):
            try:
                return int(cmd[6:])
            except ValueError:
                return None
    return None


def agent_errors(sections: list[tuple[str, list[str]]]) -> list[str]:
    """Lines starting with 'ERROR:' written by the agent itself."""
    return [line for _cmd, lines in sections for line in lines if line.startswith("ERROR:")]


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _response(lines: list[str] | None, prefix: str) -> str | None:
    for line in lines or []:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def parse_info(output: str) -> ModemInfo:
    sections = split_sections(output)
    info = ModemInfo(agent_version=agent_version(sections))
    for line in section(sections, "SYSTEM") or []:
        key, _, value = line.partition("=")
        value = value.strip() or None
        if key == "board.name":
            info.board = value
        elif key == "board.shortname":
            info.board_short = value
        elif key == "board.hwaddr" and value and re.fullmatch(r"[0-9A-Fa-f]{12}", value):
            info.mac = ":".join(value[i:i + 2] for i in range(0, 12, 2)).lower()
        elif key == "firmware":
            info.firmware = value
    ati = section(sections, "ATI") or []
    info.model = _response(ati, "Model:")
    info.revision = _response(ati, "Revision:")
    info.imei = _response(ati, "IMEI:")
    cnum = _response(section(sections, "AT+CNUM"), "+CNUM:")
    if cnum:
        m = re.search(r'"(\+?\d+)"', cnum)
        if m:
            info.own_number = m.group(1)
    return info


def _parse_gstatus(lines: list[str], status: ModemStatus) -> None:
    """Sierra Wireless AT!GSTATUS? — tab separated 'Key: value' pairs."""
    pairs: dict[str, str] = {}
    for line in lines:
        if line.startswith("!GSTATUS") or line in ("OK", "ERROR"):
            continue
        # Split at tabs; each chunk is "Key: value" (keys may contain spaces).
        for chunk in re.split(r"\t+", line):
            key, sep, value = chunk.partition(":")
            if sep and key.strip():
                # Two "RSRP (dBm)" keys exist (RxM and RxD) -> keep the first.
                pairs.setdefault(key.strip(), value.strip())
    status.extra = pairs
    status.temperature = _int(pairs.get("Temperature"))
    status.system_mode = pairs.get("System mode") or None
    band = pairs.get("LTE band")
    status.band = band if band and band != "--" else None
    bw = pairs.get("LTE bw")
    status.bandwidth = bw if bw and bw != "--" else None
    rssi = _int(pairs.get("PCC RxM RSSI"))
    if rssi is not None:
        status.rssi = rssi
    status.rsrp = _int(pairs.get("RSRP (dBm)"))
    status.rsrq = _float(pairs.get("RSRQ (dB)"))
    status.sinr = _float(pairs.get("SINR (dB)"))
    cell = pairs.get("Cell ID")
    status.cell_id = cell.split()[0] if cell else None
    tac = pairs.get("TAC")
    status.tac = tac.split()[0] if tac else None


def parse_status(output: str) -> ModemStatus:
    sections = split_sections(output)
    status = ModemStatus()

    csq = _response(section(sections, "AT+CSQ"), "+CSQ:")
    if csq:
        n = _int(csq.split(",")[0])
        if n is not None and 0 <= n <= 31:
            status.rssi = -113 + 2 * n

    cops = _response(section(sections, "AT+COPS?"), "+COPS:")
    if cops:
        m = re.match(r'\d+,\d+,"([^"]*)"(?:,(\d+))?', cops)
        if m:
            status.operator = m.group(1) or None
            act = _int(m.group(2))
            status.access_technology = ACCESS_TECH.get(act) if act is not None else None

    for cmd, prefix in (("AT+CEREG?", "+CEREG:"), ("AT+CREG?", "+CREG:")):
        reg = _response(section(sections, cmd), prefix)
        if reg:
            fields = reg.split(",")
            stat = _int(fields[1]) if len(fields) > 1 else None
            if stat in (1, 5) or status.registration is None:
                status.registration = REGISTRATION.get(stat) if stat is not None else None
            if status.registration in ("home", "roaming"):
                break

    cpms = _response(section(sections, 'AT+CPMS="SM","SM","SM"'), "+CPMS:")
    if cpms:
        nums = [x for x in cpms.split(",") if x.strip().isdigit()]
        if len(nums) >= 2:
            status.sms_used, status.sms_total = int(nums[0]), int(nums[1])

    gstatus = section(sections, "AT!GSTATUS?")
    if gstatus:
        _parse_gstatus(gstatus, status)
    return status


def parse_cmgl(output: str) -> list[tuple[int, str]]:
    """[(index, pdu hex)] from the AT+CMGL=4 section."""
    lines = section(split_sections(output), "AT+CMGL=4")
    if lines is None:
        raise AgentError("No AT+CMGL response from the modem")
    if lines and not any(line == "OK" for line in lines):
        raise AgentError("AT+CMGL failed: " + " / ".join(lines[-2:]))
    result: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        m = re.match(r"\+CMGL:\s*(\d+)", lines[i])
        if m and i + 1 < len(lines) and re.fullmatch(r"[0-9A-Fa-f]+", lines[i + 1].strip()):
            result.append((int(m.group(1)), lines[i + 1].strip()))
            i += 2
            continue
        i += 1
    return result


def parse_send(output: str) -> list[int]:
    """Message references (+CMGS) of all sent parts. Raises AgentError."""
    sections = split_sections(output)
    refs: list[int] = []
    for cmd, lines in sections:
        if not cmd.startswith("AT+CMGS="):
            continue
        ref = _response(lines, "+CMGS:")
        if ref is None:
            err = next((line for line in lines if "ERROR" in line), None)
            raise AgentError(err or "No confirmation from the modem")
        refs.append(int(ref.split(",")[0]) if ref.split(",")[0].isdigit() else -1)
    errors = agent_errors(sections)
    if errors:
        raise AgentError(errors[0][6:].strip())
    return refs
