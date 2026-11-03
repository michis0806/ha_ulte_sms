"""Transport regressions using simulated SSH results, without device access."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncssh
import pytest

from custom_components.ulte_sms.modem import (
    UlteConnectionError,
    UlteDeleteError,
    UlteModem,
    UlteModemError,
)

from .helpers import STATUS_OUTPUT, cmgl, deliver_pdus, host_key


def _modem(*results):
    modem = UlteModem("192.0.2.1", "test", host_key())
    connection = SimpleNamespace(
        run=AsyncMock(side_effect=results),
        close=Mock(),
        wait_closed=AsyncMock(),
    )
    modem._connect = AsyncMock(return_value=connection)
    return modem, connection


def _result(output, exit_status=0):
    return SimpleNamespace(stdout=output, stderr="", exit_status=exit_status)


@pytest.mark.parametrize("error", [OSError("response lost"), asyncssh.ConnectionLost("lost")])
async def test_send_is_not_retried_after_lost_response(error):
    # The device may already have sent the SMS when receiving the response fails.
    modem, connection = _modem(error, _result("### AT+CMGS=20\n+CMGS: 42\nOK\n"))
    with pytest.raises(UlteConnectionError, match="outcome unknown.*not retried"):
        await modem.async_send("+491700000000", "Hello")
    assert connection.run.await_count == 1
    assert modem._connect.await_count == 1
    connection.close.assert_called_once()


async def test_send_timeout_reports_unknown_outcome():
    error = asyncssh.TimeoutError({}, "sh -s -- send", None, None, None, None, "", "")
    modem, connection = _modem(error)
    with pytest.raises(UlteModemError, match="outcome unknown.*not retried"):
        await modem.async_send("+491700000000", "Hello")
    assert connection.run.await_count == 1


async def test_read_reconnects_once():
    modem, connection = _modem(OSError("stale connection"), _result(STATUS_OUTPUT))
    assert (await modem.async_status()).sms_used == 2
    assert connection.run.await_count == 2
    assert modem._connect.await_count == 2


async def test_read_stops_after_second_failure():
    modem, connection = _modem(OSError("lost"), OSError("lost again"))
    with pytest.raises(UlteConnectionError):
        await modem.async_status()
    assert connection.run.await_count == 2


async def test_delete_is_not_replayed_after_lost_response():
    # A deleted SIM index could be reused by a newly arriving SMS.
    modem, connection = _modem(OSError("response lost"))
    with pytest.raises(UlteConnectionError, match="outcome unknown"):
        await modem.async_delete([3])
    assert connection.run.await_count == 1


@pytest.mark.parametrize("exit_status", [0, 1])
@pytest.mark.parametrize("failure", ["+CMS ERROR: 321", "+CME ERROR: 10", "ERROR", ""])
async def test_delete_reports_partial_confirmation(exit_status, failure):
    modem, _ = _modem(_result(
        f"### AT+CMGD=3\nOK\n### AT+CMGD=4\n{failure}\n", exit_status))
    with pytest.raises(UlteDeleteError) as exc:
        await modem.async_delete([3, 4])
    assert exc.value.deleted_indices == [3]
    assert "AT+CMGD=4" in str(exc.value)


@pytest.mark.parametrize("output", [
    "", "### AT+CMGD=99\nOK\n", "### AT+CMGD=3\nOK\n### AT+CMGD=3\nOK\n",
    "### AT+CMGD=3\n+CMS ERROR: 321\nOK\n",
])
async def test_delete_requires_unambiguous_ok_for_requested_index(output):
    modem, _ = _modem(_result(output))
    with pytest.raises(UlteDeleteError) as exc:
        await modem.async_delete([3])
    assert exc.value.deleted_indices == []


async def test_delete_success_and_empty_request():
    modem, connection = _modem(_result("### AT+CMGD=3\nOK\n### AT+CMGD=4\nOK\n"))
    await modem.async_delete([])
    connection.run.assert_not_awaited()
    await modem.async_delete([4, 3, 3])
    assert connection.run.call_args.args[0] == "sh -s -- delete 3 4"


async def test_bad_udh_does_not_block_following_sms():
    malformed = "00440281210008621102321055400403000307"
    modem, _ = _modem(_result(STATUS_OUTPUT + cmgl(
        [malformed, *deliver_pdus("+491700000000", "Valid message")], start=3)))
    status, parts, other = await modem.async_poll()
    assert status.sms_used == 2
    assert [(p.index, p.text) for p in parts] == [(4, "Valid message")]
    assert other == [3]
