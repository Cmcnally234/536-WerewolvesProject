#!/usr/bin/env bash
# test_permissions.sh -- prove each seat's socket is reachable only by its owner.
#
#   sudo ./setup_users.sh && sudo ./test_permissions.sh
#
# Starts perm_server.py as modbot with seats for player1 and player2, then
# runs perm_probe.py as various users and checks the outcome of each:
#   ACCEPTED (0)  DENIED by filesystem (2)  REJECTED by SO_PEERCRED (3)

set -uo pipefail    # no -e: several probes are *supposed* to fail

if [[ $EUID -ne 0 ]]; then
    echo "error: run with sudo" >&2
    exit 1
fi

APP_DIR=/opt/werewolf
RUN_DIR=/run/werewolf
OUTSIDER=nobody     # exists on every Fedora system, not in any game group

if ! id -u modbot >/dev/null 2>&1 || [[ ! -d $RUN_DIR/player2 ]]; then
    echo "error: run setup_users.sh first" >&2
    exit 1
fi

# Run from / so no process starts with a cwd it isn't allowed to read.
cd /

LOG=$(mktemp)
# The outsider can't read /opt/werewolf (that's part of the point), so give
# it a world-readable copy of the probe. Having the code is not the same as
# being able to reach the socket.
OUTSIDER_DIR=$(mktemp -d)
install -m 0644 "$APP_DIR/commslib.py" "$APP_DIR/perm_probe.py" "$OUTSIDER_DIR/"
chmod 0755 "$OUTSIDER_DIR"

sudo -u modbot python3 "$APP_DIR/perm_server.py" player1 player2 \
    --dir "$RUN_DIR" --duration 30 >"$LOG" 2>&1 &
SERVER_PID=$!
cleanup() {
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
    rm -rf "$LOG" "$OUTSIDER_DIR"
}
trap cleanup EXIT

# Wait (up to 5s) for both sockets to appear.
for _ in $(seq 50); do
    [[ -S $RUN_DIR/player1/sock && -S $RUN_DIR/player2/sock ]] && break
    sleep 0.1
done
if [[ ! -S $RUN_DIR/player1/sock ]]; then
    echo "error: server didn't start:" >&2
    cat "$LOG" >&2
    exit 1
fi

echo "== layout =="
ls -ld "$RUN_DIR" "$RUN_DIR"/player1 "$RUN_DIR"/player2
ls -l "$RUN_DIR"/player1/sock "$RUN_DIR"/player2/sock
echo

PASS=0
FAIL=0
# expect <expected exit code> <description> <command...>
expect() {
    local want=$1 desc=$2
    shift 2
    local out code
    out=$("$@" 2>&1)
    code=$?
    if [[ $code -eq $want ]]; then
        PASS=$((PASS + 1))
        printf 'PASS  %-50s | %s\n' "$desc" "$(tail -n1 <<<"$out")"
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL  %-50s | wanted exit %s, got %s\n' "$desc" "$want" "$code"
        sed 's/^/        /' <<<"$out"
    fi
}

probe_as() {    # probe_as <user> <seat>
    sudo -u "$1" python3 "$APP_DIR/perm_probe.py" "$2" --dir "$RUN_DIR"
}
outsider_probe() {    # outsider_probe <seat>
    sudo -u "$OUTSIDER" python3 "$OUTSIDER_DIR/perm_probe.py" "$1" --dir "$RUN_DIR"
}

echo "== probes =="
expect 0 "player1 -> own socket"              probe_as player1 player1
expect 0 "player2 -> own socket"              probe_as player2 player2
expect 2 "player2 -> player1's socket"        probe_as player2 player1
expect 2 "player1 -> player2's socket"        probe_as player1 player2
expect 2 "$OUTSIDER (outsider) -> player1's socket" outsider_probe player1
expect 2 "$OUTSIDER (outsider) -> player2's socket" outsider_probe player2
expect 2 "$OUTSIDER (outsider) can't list $RUN_DIR" \
    sudo -u "$OUTSIDER" ls "$RUN_DIR"
# modbot owns every seat directory, so the filesystem lets it through --
# only the SO_PEERCRED check stops it from impersonating player1.
expect 3 "modbot -> player1's socket (SO_PEERCRED)"  probe_as modbot player1

echo
echo "== server log =="
sleep 0.3
cat "$LOG"

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
