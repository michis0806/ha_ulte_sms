#!/bin/sh
# Runs ON the UniFi LTE device. Home Assistant pipes this script over SSH
# ("sh -s -- <mode> [args]"), nothing is installed on the device.
#
# Modes:
#   info                      board/firmware data, ATI, own number
#   status                    signal, operator, registration, SIM storage
#   poll                      status + all SMS on the SIM in PDU mode
#   send <len>:<pduhex> ...   send PDUs (AT+CMGS), one argument per part
#   delete <idx> ...          delete SMS by index (AT+CMGD)
#   persist_host_key          store dropbear's host key(s) in /etc/persistent
#                             and save to flash the way the firmware does
#
# Output: every AT command starts a section "### <command>", followed by the
# modem's response lines without CR and empty lines.
AGENT_VERSION=2
P=/dev/ttyUSB2
MODE=$1
[ $# -gt 0 ] && shift

echo "### AGENT $AGENT_VERSION"

# Host key files dropbear is started with (-r <file> in /etc/inittab).
host_key_files() {
    awk '/dropbear/ && !/^#/ { for (i = 1; i < NF; i++) if ($i == "-r") print $(i + 1) }' \
        /etc/inittab 2>/dev/null
}

# "### HOSTKEYS" section: "<file name> <state>" per key.
#   persistent   stored in the flash config, survives a reboot
#   not_in_flash in /etc/persistent, but not (yet) written to flash
#   ram_only     only in RAM, a new key is generated on the next boot
#   missing      file does not exist
host_keys() {
    echo "### HOSTKEYS"
    D=/tmp/ulte_sms_flash.$$
    rm -rf $D; mkdir -p $D
    flash=0
    if command -v cfgmtd >/dev/null 2>&1 &&
            cfgmtd -r -p $D -f $D/read.cfg >/dev/null 2>&1; then
        flash=1
    fi
    for f in $(host_key_files); do
        n=$(basename "$f")
        if [ ! -f "$f" ]; then
            echo "$n missing"
        elif [ $flash = 1 ] && cmp -s "$f" "$D/persistent/$n"; then
            echo "$n persistent"
        elif cmp -s "$f" "/etc/persistent/$n"; then
            [ $flash = 1 ] && echo "$n not_in_flash" || echo "$n persistent"
        else
            echo "$n ram_only"
        fi
    done
    rm -rf $D
}

if [ "$MODE" = persist_host_key ]; then
    for f in $(host_key_files); do
        n=$(basename "$f")
        [ -f "$f" ] || continue
        if ! cmp -s "$f" "/etc/persistent/$n"; then
            cp "$f" "/etc/persistent/$n.new" && chmod 600 "/etc/persistent/$n.new" &&
                mv "/etc/persistent/$n.new" "/etc/persistent/$n" ||
                { echo "ERROR: could not copy $n"; exit 1; }
        fi
    done
    # Same as the firmware's config monitor: mark and save (locks, cfgmtd -w).
    touch /var/run/need_cfg_save
    if [ -x /usr/etc/syswrapper.sh ]; then
        /usr/etc/syswrapper.sh cfg_save_check >/dev/null 2>&1
    else
        cfgmtd -w -p /etc /tmp/system.cfg >/dev/null 2>&1
    fi
    host_keys
    exit 0
fi

[ -c $P ] || { echo "ERROR: AT port $P missing"; exit 3; }

# Lock against parallel access to the AT port (stale after 30 s).
n=0
while ! mkdir /tmp/ulte_sms.lock 2>/dev/null; do
    n=$((n+1))
    if [ $n -gt 30 ]; then rm -rf /tmp/ulte_sms.lock; n=0; else sleep 1; fi
done
OUT=/tmp/ulte_sms_out.$$
trap 'rm -rf /tmp/ulte_sms.lock $OUT' EXIT INT TERM

stty raw -echo < $P 2>/dev/null || { echo "ERROR: AT port not usable"; exit 3; }

done_re='^(OK|ERROR|\+CM[ES] ERROR.*)$'

# at <command> [max half-seconds] [end pattern]
at() {
    rm -f $OUT
    cat $P > $OUT & CP=$!
    usleep 200000
    printf '%s\r' "$1" > $P
    lim=${2:-16}; re=${3:-$done_re}; i=0
    while [ $i -lt $lim ]; do
        tr -d '\r' < $OUT 2>/dev/null | grep -q -E "$re" && break
        usleep 500000; i=$((i+1))
    done
    kill $CP 2>/dev/null; wait $CP 2>/dev/null
    echo "### $1"
    tr -d '\r' < $OUT | grep -v '^$'
}

quiet() { at "$@" > /dev/null; }

status() {
    at 'AT+CSQ'
    at 'AT+COPS?'
    at 'AT+CEREG?'
    at 'AT+CREG?'
    at 'AT+CPMS="SM","SM","SM"'
    at 'AT!GSTATUS?' 10
}

case "$MODE" in
info)
    echo "### SYSTEM"
    grep -E '^board\.(name|shortname|hwaddr)=' /etc/board.info 2>/dev/null
    printf 'firmware=%s\n' "$(cat /etc/version 2>/dev/null)"
    host_keys
    quiet ATE0
    at ATI
    at 'AT+CNUM'
    ;;
status)
    quiet ATE0
    status
    ;;
poll)
    quiet ATE0
    status
    quiet 'AT+CMGF=0'
    at 'AT+CMGL=4' 40
    ;;
send)
    quiet ATE0
    quiet 'AT+CMGF=0'
    rc=0
    for part in "$@"; do
        case "$part" in *[!0-9A-Fa-f:]*) echo "ERROR: invalid PDU"; rc=2; break;; esac
        len=${part%%:*}; hex=${part#*:}
        rm -f $OUT
        cat $P > $OUT & CP=$!
        usleep 200000
        printf 'AT+CMGS=%s\r' "$len" > $P
        i=0
        while [ $i -lt 20 ]; do
            grep -q '>' $OUT 2>/dev/null && break
            usleep 500000; i=$((i+1))
        done
        if ! grep -q '>' $OUT 2>/dev/null; then
            printf '\033' > $P
            kill $CP 2>/dev/null; wait $CP 2>/dev/null
            echo "### AT+CMGS=$len"
            tr -d '\r' < $OUT | grep -v '^$'
            echo "ERROR: no prompt from modem"
            rc=1; break
        fi
        printf '%s\032' "$hex" > $P
        i=0
        while [ $i -lt 120 ]; do
            tr -d '\r' < $OUT | grep -q -E "^\+CMGS|$done_re" && break
            usleep 500000; i=$((i+1))
        done
        kill $CP 2>/dev/null; wait $CP 2>/dev/null
        echo "### AT+CMGS=$len"
        tr -d '\r' < $OUT | grep -v '^$' | grep -v '^> *$'
        tr -d '\r' < $OUT | grep -q '^+CMGS' || { rc=1; break; }
    done
    exit $rc
    ;;
delete)
    quiet ATE0
    for i in "$@"; do
        case "$i" in ''|*[!0-9]*) continue;; esac
        at "AT+CMGD=$i"
    done
    ;;
*)
    echo "ERROR: unknown mode $MODE"
    exit 2
    ;;
esac
exit 0
