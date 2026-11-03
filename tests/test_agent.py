"""Exercise the shipped shell command dispatch with a simulated AT modem."""
from pathlib import Path
import subprocess

import pytest

AGENT = Path(__file__).parents[1] / "custom_components/ulte_sms/agent.sh"


@pytest.mark.parametrize("reply, expected_exit", [
    ("OK", 0), ("+CMS ERROR: 321", 1), ("+CME ERROR: 10", 1), ("ERROR", 1), ("", 1),
])
def test_delete_dispatch_checks_modem_result(tmp_path, reply, expected_exit):
    # Execute the original dispatch, replacing only the serial I/O functions.
    dispatch = AGENT.read_text().split('case "$MODE" in\n', 1)[1]
    script = '''MODE=delete
OUT=$1
REPLY=$2
shift 2
quiet() { :; }
at() {
    echo "### $1"
    case "$1" in
        AT+CMGD=3) printf '%s\\n' "$REPLY" > "$OUT";;
        *) printf 'OK\\n' > "$OUT";;
    esac
    cat "$OUT"
}
case "$MODE" in
''' + dispatch
    result = subprocess.run(
        ["sh", "-s", "--", str(tmp_path / "response"), reply, "3", "4"],
        input=script, text=True, capture_output=True, check=False,
    )
    assert result.returncode == expected_exit, result.stderr
    # Continue after a failed deletion so later successes can be accounted for.
    assert "### AT+CMGD=3" in result.stdout
    assert "### AT+CMGD=4\nOK" in result.stdout
