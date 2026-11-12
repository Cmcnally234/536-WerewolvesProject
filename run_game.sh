#!/usr/bin/env bash
# run_game.sh -- launch one game: the moderator as modbot, every seat as its
# own Linux user.
#
#   sudo ./run_game.sh                        # 5 seats, all bots
#   sudo ./run_game.sh -H 1,2                 # player1 + player2 are humans, the rest bots
#   sudo ./run_game.sh -n 6 -S 4 -- --night-timeout 15 --discussion 20
#   sudo ./run_game.sh --host 192.168.1.10 -R 3,4,5 --open-firewall   # seats 3-5 elsewhere
#
# Every player talks to the moderator over TCP + mutual TLS, with the
# certificates from make_certs.sh -- on this machine as much as on others.
#
# Options:
#   -n N      number of seats, 4-6 (default 5; setup_users.sh must have created them)
#   -H LIST   comma-separated seat numbers played by humans (default: none)
#   -S LIST   seat numbers run as SILENT bots -- they never respond, which
#             shows the moderator's timeout path
#   --port N  the moderator's TCP port (default 5555)
#   --host A  the moderator's address (default 127.0.0.1): it listens there,
#             local seats connect there, and remote players must use it. It
#             must be a name or IP in the moderator's certificate.
#   --tls DIR certificates (default /etc/werewolf/tls)
#   -R LIST   seat numbers played on OTHER machines (deploy_remote.sh them
#             first); the command to run there is printed
#   --open-firewall  open the port in firewalld for this game only (runtime
#             rule, removed at exit)
#   -- ARGS   everything after -- goes to moderator.py (timers, --seed, ...)
#
# The moderator runs in the foreground in THIS terminal. It prints every role,
# so don't show this terminal to players. Bots run in the background and log
# to a temp directory. Human seats are started by you, one terminal each, with
# the commands printed below. Ctrl-C here ends the game cleanly for everyone.

set -uo pipefail

APP_DIR=/opt/werewolf
SRC_DIR=$(dirname "$(readlink -f "$0")")

usage() { sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "error: $*" >&2; exit 1; }

# --- arguments ----------------------------------------------------------------

N=5
HUMANS=""
SILENT=""
REMOTE=""
HOST=127.0.0.1
PORT=5555
TLS=/etc/werewolf/tls
OPEN_FW=0
# A plain loop rather than getopts, which can't parse long options.
while [[ $# -gt 0 ]]; do
    case $1 in
        -n|-H|-S|-R|--host|--port|--tls)
            [[ $# -ge 2 ]] || die "$1 needs a value"
            case $1 in
                -n)     N=$2 ;;
                -H)     HUMANS=${2//,/ } ;;
                -S)     SILENT=${2//,/ } ;;
                -R)     REMOTE=${2//,/ } ;;
                --host) HOST=$2 ;;
                --port) PORT=$2 ;;
                --tls)  TLS=$2 ;;
            esac
            shift 2 ;;
        --open-firewall) OPEN_FW=1; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; break ;;
        *) usage >&2; exit 2 ;;
    esac
done
MOD_ARGS=("$@")

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ $N =~ ^[4-6]$ ]] || die "-n must be 4, 5 or 6"
for s in $HUMANS $SILENT $REMOTE; do
    [[ $s =~ ^[0-9]+$ ]] && ((s >= 1 && s <= N)) || die "seat '$s' is not between 1 and $N"
done
for s in $HUMANS; do
    [[ " $SILENT " == *" $s "* ]] && die "seat $s can't be both human and silent"
done
for s in $REMOTE; do
    [[ " $HUMANS $SILENT " == *" $s "* ]] && die "seat $s is remote: it can't also be -H or -S here"
done
[[ $PORT =~ ^[0-9]+$ ]] || die "--port must be a number"
if [[ -n $REMOTE && ($HOST == 127.* || $HOST == localhost) ]]; then
    die "remote seats can't reach $HOST -- give --host this machine's address (one in the moderator's certificate)"
fi

# --- is the machine ready? -----------------------------------------------------

kind_of() {    # kind_of <seat number> -> human | silent | remote | bot
    if [[ " $HUMANS " == *" $1 "* ]]; then echo human
    elif [[ " $SILENT " == *" $1 "* ]]; then echo silent
    elif [[ " $REMOTE " == *" $1 "* ]]; then echo remote
    else echo bot
    fi
}

id -u modbot >/dev/null 2>&1 || die "no modbot user -- run: sudo ./setup_users.sh"
[[ -f $TLS/moderator/key.pem ]] || die "no moderator certificate in $TLS -- run: sudo ./make_certs.sh"
# Catch the most common cross-machine mistake here rather than as a
# certificate error on every player: the address players will dial must be
# in the moderator's certificate.
if [[ $HOST =~ ^[0-9.]+$ ]]; then check=(-checkip "$HOST"); else check=(-checkhost "$HOST"); fi
if ! openssl x509 -in "$TLS/moderator/cert.pem" -noout "${check[@]}" | grep -q "does match"; then
    die "the moderator's certificate doesn't list '$HOST' -- reissue it: sudo ./make_certs.sh -s IP:<addr> -s DNS:<name> ..."
fi
for i in $(seq 1 "$N"); do
    [[ $(kind_of "$i") == remote ]] && continue
    [[ -f $TLS/player$i/key.pem ]] || die "no certificate for player$i in $TLS -- run: sudo N_PLAYERS=$N ./make_certs.sh"
done

# Players run the copy in /opt/werewolf, not this directory. Refuse to start
# a game on stale code -- the classic "I fixed it but nothing changed" trap.
stale=()
for f in "$SRC_DIR"/*.py; do
    cmp -s "$f" "$APP_DIR/$(basename "$f")" || stale+=("$(basename "$f")")
done
((${#stale[@]} == 0)) || die "$APP_DIR is out of date (${stale[*]}) -- run: sudo ./setup_users.sh"

# --- plan ----------------------------------------------------------------------

LOG_DIR=$(mktemp -d /tmp/werewolf-run.XXXXXX)
chmod 0755 "$LOG_DIR"

SEATS=()
for i in $(seq 1 "$N"); do SEATS+=("player$i"); done

# Where the moderator listens = where every player connects.
NET=(--host "$HOST" --port "$PORT" --tls "$TLS")

echo "== werewolf: $N seats over TCP+TLS $HOST:$PORT, logs in $LOG_DIR =="
for i in $(seq 1 "$N"); do
    case $(kind_of "$i") in
        human)  echo "  player$i  HUMAN   open a terminal and run:  sudo -u player$i python3 $APP_DIR/player.py player$i ${NET[*]}" ;;
        silent) echo "  player$i  silent bot" ;;
        remote) echo "  player$i  REMOTE  played on another machine" ;;
        bot)    echo "  player$i  bot" ;;
    esac
done
if [[ -n $REMOTE ]]; then
    remote_seats=$(for i in $REMOTE; do printf 'player%s ' "$i"; done)
    echo "  on each remote machine, for the seats deployed there (deploy_remote.sh):"
    echo "      sudo /opt/werewolf/run_players.sh --host $HOST --port $PORT <seats>      # of: $remote_seats"
    echo "  (add -H <seat> there for a human)"
fi
[[ -n $HUMANS$REMOTE ]] && echo "  (the moderator waits for every seat; add '-- --join-timeout 300' for more than 120s)"
echo

# --- bots (background) -----------------------------------------------------------

declare -A BOT_PID
FW_OPENED=0
cleanup() {
    for pid in "${BOT_PID[@]}"; do
        kill "$pid" 2>/dev/null
    done
    if ((FW_OPENED)); then
        firewall-cmd --quiet --remove-port="$PORT/tcp"
        echo "  firewalld: closed $PORT/tcp again"
    fi
}
trap cleanup EXIT

# --- firewall (opt-in) ------------------------------------------------------------

if ((OPEN_FW)); then
    command -v firewall-cmd >/dev/null && firewall-cmd --quiet --state 2>/dev/null ||
        die "--open-firewall: firewalld isn't running"
    if firewall-cmd --quiet --query-port="$PORT/tcp"; then
        echo "firewalld: $PORT/tcp is already open (leaving it as it is)"
    else
        # Runtime only (no --permanent): gone at the next reload/reboot even
        # if this script were killed before cleanup could close it.
        firewall-cmd --quiet --add-port="$PORT/tcp"
        FW_OPENED=1
        echo "firewalld: opened $PORT/tcp for this game (zone $(firewall-cmd --get-default-zone))"
    fi
    echo
fi

for i in $(seq 1 "$N"); do
    kind=$(kind_of "$i")
    [[ $kind == human || $kind == remote ]] && continue
    mode=act
    [[ $kind == silent ]] && mode=silent
    # The bot opens --log itself, as player$i, so create the file with that
    # owner first (LOG_DIR belongs to root).
    install -o "player$i" -m 0644 /dev/null "$LOG_DIR/player$i.jsonl"
    # setsid: own session, so Ctrl-C in this terminal reaches only the
    # moderator. The bots then hear about the shutdown the proper way, via
    # the moderator's GAME_OVER. Redirecting every fd keeps sudo from
    # attaching the background job to this terminal.
    setsid sudo -u "player$i" python3 "$APP_DIR/player.py" "player$i" "${NET[@]}" \
        --bot "$mode" --log "$LOG_DIR/player$i.jsonl" \
        > "$LOG_DIR/player$i.out" 2>&1 < /dev/null &
    BOT_PID[$i]=$!
done

# --- moderator (foreground) -------------------------------------------------------

# tee -i: tee ignores Ctrl-C, so it keeps copying the moderator's shutdown
# messages instead of dying and breaking the pipe under it.
sudo -u modbot python3 "$APP_DIR/moderator.py" "${NET[@]}" "${MOD_ARGS[@]}" "${SEATS[@]}" 2>&1 \
    | tee -i "$LOG_DIR/moderator.out"
MOD_STATUS=${PIPESTATUS[0]}

# --- wrap up ----------------------------------------------------------------------

# Bots exit on their own after GAME_OVER; give stragglers 10s.
deadline=$((SECONDS + 10))
for pid in "${BOT_PID[@]}"; do
    while kill -0 "$pid" 2>/dev/null && ((SECONDS < deadline)); do sleep 0.2; done
done

echo
echo "== summary =="
echo "  moderator exit status: $MOD_STATUS (0 = game finished, 1 = aborted or nobody joined)"
for i in $(seq 1 "$N"); do
    [[ -n ${BOT_PID[$i]:-} ]] || continue
    if kill -0 "${BOT_PID[$i]}" 2>/dev/null; then
        echo "  player$i: still running after 10s -- killing it"
        kill "${BOT_PID[$i]}"
    else
        wait "${BOT_PID[$i]}"
        # Saved at once: inside the echo, $(kind_of ...) would run first
        # and replace $? with its own (always 0) status.
        code=$?
        echo "  player$i ($(kind_of "$i")): exit status $code"
    fi
done
# The TCP analogue of "no socket files left behind": nothing still listens.
if ss -Hltn "sport = :$PORT" 2>/dev/null | grep -q .; then
    echo "  port $PORT: something is still listening on it"
else
    echo "  port $PORT: released"
fi

# Hand the logs to whoever ran sudo, so they can read them without root.
[[ -n ${SUDO_USER:-} ]] && chown -R "$SUDO_USER" "$LOG_DIR"
echo "  logs: $LOG_DIR  (moderator.out, playerN.out, playerN.jsonl = raw messages received)"

BOT_PID=()     # all finished (or killed) above: nothing left for cleanup to kill
cleanup        # ...but it still closes the firewall port if we opened it
trap - EXIT
exit "$MOD_STATUS"
