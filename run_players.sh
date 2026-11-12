#!/usr/bin/env bash
# run_players.sh -- start THIS machine's seats for a game whose moderator
# runs on another machine. deploy_remote.sh installs it as
# /opt/werewolf/run_players.sh, next to the code and the seats' certificates.
#
#   sudo /opt/werewolf/run_players.sh --host 192.168.1.10 --port 5555 player3 player4
#   sudo /opt/werewolf/run_players.sh --host mod.lan --port 5555 -H player3 player3 player4
#
# Options:
#   --host ADDR   the moderator's address -- a name or IP its certificate lists
#   --port N      the moderator's port (default 5555)
#   --tls DIR     certificates (default /etc/werewolf/tls)
#   -H LIST       comma-separated seats played by humans: their commands are
#                 printed, and you start each in its own terminal
#   -S LIST       seats run as SILENT bots (they never respond)
#   -- ARGS       extra player.py flags for every bot (e.g. --heartbeat 1)
#
# Bots run in the background, each as its own Linux user, and log to a temp
# directory. This script waits until they've all finished (the game is over),
# then prints a summary. Ctrl-C makes every bot leave the game (BYE).

set -uo pipefail

APP_DIR=/opt/werewolf
TLS=/etc/werewolf/tls
HOST=""
PORT=5555
HUMANS=""
SILENT=""

usage() { sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --host|--port|--tls|-H|-S)
            [[ $# -ge 2 ]] || die "$1 needs a value"
            case $1 in
                --host) HOST=$2 ;;
                --port) PORT=$2 ;;
                --tls)  TLS=$2 ;;
                -H)     HUMANS=" ${2//,/ } " ;;
                -S)     SILENT=" ${2//,/ } " ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        --) shift; break ;;
        -*) usage >&2; exit 2 ;;
        *) break ;;
    esac
done
SEATS=()
while [[ $# -gt 0 && $1 != -- ]]; do SEATS+=("$1"); shift; done
[[ ${1:-} == -- ]] && shift
BOT_ARGS=("$@")

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -n $HOST ]] || die "--host (the moderator's address) is required"
[[ $PORT =~ ^[0-9]+$ ]] || die "--port must be a number"
((${#SEATS[@]} > 0)) || die "name at least one seat"
for s in $HUMANS $SILENT; do
    [[ " ${SEATS[*]} " == *" $s "* ]] || die "-H/-S seat '$s' isn't in the seat list"
done
for s in "${SEATS[@]}"; do
    id -u "$s" >/dev/null 2>&1 || die "no Linux user '$s' here -- run deploy_remote.sh for it"
    [[ -f $TLS/$s/key.pem ]] || die "no certificate for '$s' in $TLS -- run deploy_remote.sh for it"
done
[[ -f $APP_DIR/player.py ]] || die "no $APP_DIR/player.py -- run deploy_remote.sh"

kind_of() {
    if [[ $HUMANS == *" $1 "* ]]; then echo human
    elif [[ $SILENT == *" $1 "* ]]; then echo silent
    else echo bot
    fi
}

LOG_DIR=$(mktemp -d /tmp/werewolf-players.XXXXXX)
chmod 0755 "$LOG_DIR"
NET=(--host "$HOST" --port "$PORT" --tls "$TLS")

echo "== werewolf on $(hostname): seats ${SEATS[*]} -> moderator $HOST:$PORT, logs in $LOG_DIR =="
for s in "${SEATS[@]}"; do
    case $(kind_of "$s") in
        human)  echo "  $s  HUMAN   open a terminal and run:  sudo -u $s python3 $APP_DIR/player.py $s ${NET[*]}" ;;
        silent) echo "  $s  silent bot" ;;
        bot)    echo "  $s  bot" ;;
    esac
done

declare -A BOT_PID
cleanup() {
    # SIGTERM: each bot closes its session properly (BYE), so the moderator
    # sees "left the game" at once instead of holding the seat.
    for pid in "${BOT_PID[@]}"; do kill "$pid" 2>/dev/null; done
}
trap cleanup EXIT
trap 'echo; echo "interrupted: telling the bots to leave"; exit 130' INT TERM

for s in "${SEATS[@]}"; do
    kind=$(kind_of "$s")
    [[ $kind == human ]] && continue
    mode=act
    [[ $kind == silent ]] && mode=silent
    # The bot opens --log itself, as $s, so create the file with that owner.
    install -o "$s" -m 0644 /dev/null "$LOG_DIR/$s.jsonl"
    # setsid + all fds redirected, as in run_game.sh: Ctrl-C here reaches
    # only this script, which then ends the bots properly (cleanup).
    setsid sudo -u "$s" python3 "$APP_DIR/player.py" "$s" "${NET[@]}" \
        --bot "$mode" --log "$LOG_DIR/$s.jsonl" "${BOT_ARGS[@]}" \
        > "$LOG_DIR/$s.out" 2>&1 < /dev/null &
    BOT_PID[$s]=$!
done
((${#BOT_PID[@]} > 0)) || { trap - EXIT; echo "(no bots to run here)"; exit 0; }

echo
echo "bots started; waiting for the game to end (Ctrl-C to leave early)..."
STATUS=0
echo
echo "== summary =="
for s in "${!BOT_PID[@]}"; do
    wait "${BOT_PID[$s]}"
    code=$?
    ((code == 0)) || STATUS=1
    last=$(tail -n 1 "$LOG_DIR/$s.out" 2>/dev/null)
    echo "  $s ($(kind_of "$s")): exit status $code   last line: ${last:-<none>}"
done
trap - EXIT
[[ -n ${SUDO_USER:-} ]] && chown -R "$SUDO_USER" "$LOG_DIR"
echo "  logs: $LOG_DIR  ($(printf '%s.out ' "${SEATS[@]}")and .jsonl = raw messages received)"
exit "$STATUS"
