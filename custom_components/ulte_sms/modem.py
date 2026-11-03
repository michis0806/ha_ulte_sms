"""SSH client for the UniFi LTE device. No Home Assistant imports."""
from __future__ import annotations

import asyncio
from functools import partial
import logging
from pathlib import Path
import random

import asyncssh

from .parser import (
    AgentError,
    ModemInfo,
    ModemStatus,
    agent_errors,
    parse_cmgl,
    parse_host_keys,
    parse_info,
    parse_send,
    parse_status,
    split_sections,
)
from .pdu import SmsPart, decode_deliver, encode_submit, PduError

_LOGGER = logging.getLogger(__name__)

AGENT_SCRIPT = (Path(__file__).parent / "agent.sh").read_text(encoding="utf-8")

CONNECT_TIMEOUT = 10
COMMAND_TIMEOUT = 60
SEND_TIMEOUT_PER_PART = 70
CONNECT_ATTEMPTS = 3
RETRY_DELAY = 3


class UlteError(Exception):
    """Base error."""


class UlteConnectionError(UlteError):
    """Device not reachable or SSH connection failed."""


class UlteAuthError(UlteError):
    """Username, password or key rejected."""


class UlteHostKeyError(UlteError):
    """The device presented a different host key than the stored one."""


class UlteModemError(UlteError):
    """The modem returned an error (AT port missing, +CMS ERROR, ...)."""


class UlteDeleteError(UlteModemError):
    """Some SIM deletions failed; only these indices were confirmed deleted."""

    def __init__(self, message: str, deleted_indices: list[int]) -> None:
        super().__init__(message)
        self.deleted_indices = deleted_indices


def fingerprint(public_key: str) -> str:
    return asyncssh.import_public_key(public_key).get_fingerprint("sha256")


async def fetch_host_key(host: str, port: int) -> str:
    """Read the device's host key without authenticating (OpenSSH format)."""
    loop = asyncio.get_running_loop()
    options = await loop.run_in_executor(
        None, partial(asyncssh.SSHClientConnectionOptions, config=[])
    )
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            key = await asyncssh.get_server_host_key(host, port, options=options)
    except (OSError, TimeoutError, asyncssh.Error) as err:
        raise UlteConnectionError(f"{host}:{port}: {err}") from err
    if key is None:
        raise UlteConnectionError(f"{host}:{port}: no host key received")
    return key.export_public_key("openssh").decode().strip()


class UlteModem:
    """Runs the agent script on the device over a persistent SSH connection."""

    def __init__(
        self,
        host: str,
        username: str,
        host_key: str,
        *,
        port: int = 22,
        password: str | None = None,
        private_key: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._username = username
        self._password = password or None
        self._private_key = private_key or None
        self._host_key = host_key
        self._conn: asyncssh.SSHClientConnection | None = None
        self._options: asyncssh.SSHClientConnectionOptions | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def _build_options(self) -> asyncssh.SSHClientConnectionOptions:
        """Runs in an executor: asyncssh touches the file system here."""
        client_keys = None
        if self._private_key:
            try:
                client_keys = [asyncssh.import_private_key(self._private_key)]
            except (asyncssh.KeyImportError, ValueError) as err:
                raise UlteAuthError(f"Private key invalid: {err}") from err
        return asyncssh.SSHClientConnectionOptions(
            username=self._username,
            password=self._password,
            client_keys=client_keys,
            known_hosts=([asyncssh.import_public_key(self._host_key)], [], []),
            agent_path=None,
            config=[],
            connect_timeout=CONNECT_TIMEOUT,
            login_timeout=CONNECT_TIMEOUT,
            keepalive_interval=30,
            keepalive_count_max=3,
            # Password/keyboard-interactive only if a password is given.
            password_auth=self._password is not None,
            kbdint_auth=self._password is not None,
            public_key_auth=client_keys is not None,
        )

    async def _connect(self) -> asyncssh.SSHClientConnection:
        if self._options is None:
            loop = asyncio.get_running_loop()
            self._options = await loop.run_in_executor(None, self._build_options)
        last: Exception | None = None
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                return await asyncssh.connect(self.host, self.port, options=self._options)
            except asyncssh.HostKeyNotVerifiable as err:
                raise UlteHostKeyError(str(err)) from err
            except asyncssh.PermissionDenied as err:
                raise UlteAuthError(str(err)) from err
            except (OSError, TimeoutError, asyncssh.Error) as err:
                # Dropbear on the device occasionally drops connections
                # right after accept -> retry a few times.
                last = err
                _LOGGER.debug("Connect to %s failed (attempt %s): %s", self.host, attempt, err)
                if attempt < CONNECT_ATTEMPTS:
                    await asyncio.sleep(RETRY_DELAY)
        raise UlteConnectionError(f"{self.host}:{self.port}: {last}") from last

    def set_host_key(self, host_key: str) -> None:
        """Trust a different host key from now on (next connect)."""
        self._host_key = host_key
        self._options = None

    async def async_close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            try:
                await asyncio.wait_for(self._conn.wait_closed(), 5)
            except (TimeoutError, asyncssh.Error, OSError):
                pass
            self._conn = None

    async def _run(self, mode: str, *args: str, timeout: float = COMMAND_TIMEOUT) -> str:
        """Run the agent; retry only read operations after a lost connection."""
        command = " ".join(["sh", "-s", "--", mode, *args])
        can_retry = mode in {"info", "status", "poll"}
        async with self._lock:
            for attempt in (1, 2):
                if self._conn is None:
                    self._conn = await self._connect()
                try:
                    result = await self._conn.run(
                        command, input=AGENT_SCRIPT, check=False, timeout=timeout
                    )
                except asyncssh.TimeoutError as err:
                    await self.async_close()
                    detail = "" if can_retry else "; outcome unknown, command was not retried"
                    raise UlteModemError(f"Agent '{mode}' timed out{detail}") from err
                except (asyncssh.Error, OSError) as err:
                    await self.async_close()
                    if not can_retry:
                        raise UlteConnectionError(
                            f"Agent '{mode}' connection lost; outcome unknown, "
                            "command was not retried"
                        ) from err
                    if attempt == 2:
                        raise UlteConnectionError(f"{self.host}: {err}") from err
                    _LOGGER.debug("SSH session failed, reconnecting: %s", err)
                    continue
                break
        output = result.stdout if isinstance(result.stdout, str) else ""
        if result.exit_status not in (0, None):
            errors = agent_errors(split_sections(output))
            # Send/delete parse their individual AT responses, including partial
            # success. Other modes must not silently accept a failed agent.
            if result.exit_status == 3 or mode not in {"send", "delete"}:
                raise UlteModemError(
                    errors[0][6:].strip() if errors
                    else f"Agent '{mode}' failed (exit {result.exit_status}): {result.stderr}"
                )
        return output

    # ------------------------------------------------------------------ #
    # API
    # ------------------------------------------------------------------ #

    async def async_info(self) -> ModemInfo:
        return parse_info(await self._run("info"))

    async def async_status(self) -> ModemStatus:
        return parse_status(await self._run("status"))

    async def async_poll(self) -> tuple[ModemStatus, list[SmsPart], list[int]]:
        """Status, SMS-DELIVER parts and indices of other PDUs (reports,
        undecodable) on the SIM."""
        output = await self._run("poll")
        status = parse_status(output)
        try:
            raw = parse_cmgl(output)
        except AgentError as err:
            raise UlteModemError(str(err)) from err
        parts: list[SmsPart] = []
        other: list[int] = []
        for index, pdu_hex in raw:
            try:
                part = decode_deliver(index, pdu_hex)
            except PduError as err:
                _LOGGER.warning("Could not decode SMS %s: %s", index, err)
                part = None
            if part is None:
                other.append(index)
            else:
                parts.append(part)
        return status, parts, other

    async def async_delete(self, indices: list[int]) -> None:
        if not indices:
            return
        indices = sorted({int(i) for i in indices})
        if any(i < 0 for i in indices):
            raise UlteModemError("SIM indices must be non-negative")
        output = await self._run("delete", *(str(i) for i in indices))
        sections = split_sections(output)
        deleted: list[int] = []
        failures: list[str] = []
        for index in indices:
            command = f"AT+CMGD={index}"
            responses = [lines for cmd, lines in sections if cmd == command]
            if len(responses) != 1:
                failures.append(f"{command}: missing or ambiguous confirmation")
                continue
            lines = responses[0]
            error = next((line for line in lines if "ERROR" in line), None)
            if error or "OK" not in lines:
                failures.append(f"{command}: {error or 'no OK from modem'}")
            else:
                deleted.append(index)
        if failures:
            raise UlteDeleteError("; ".join(failures), deleted)

    async def async_persist_host_key(self) -> dict[str, str]:
        """Store dropbear's host key in the device's flash config.

        Returns the host key states afterwards (see agent.sh).
        """
        output = await self._run("persist_host_key", timeout=120)
        errors = agent_errors(split_sections(output))
        if errors:
            raise UlteModemError(errors[0][6:].strip())
        return parse_host_keys(output)

    async def async_send(self, number: str, text: str) -> list[int]:
        """Send an SMS; returns the message references of all parts."""
        pdus = encode_submit(number, text, random.randint(0, 255))
        output = await self._run(
            "send",
            *(f"{length}:{hexdata}" for length, hexdata in pdus),
            timeout=SEND_TIMEOUT_PER_PART * len(pdus) + 10,
        )
        try:
            refs = parse_send(output)
        except AgentError as err:
            raise UlteModemError(str(err)) from err
        if len(refs) != len(pdus):
            raise UlteModemError(
                f"Only {len(refs)} of {len(pdus)} parts confirmed by the modem"
            )
        return refs
