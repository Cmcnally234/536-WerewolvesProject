#!/usr/bin/env bash
# run_game.sh -- launch one game: the moderator as modbot, every seat as its
# own Linux user.
#
#   sudo ./run_game.sh                        # 5 seats, all bots
#   sudo ./run_game.sh -H 1,2                 # player1 + player2 are humans, the rest bots
#   sudo ./run_game.sh -n 6 -S 4 -- --night-timeout 15 --discussion 20
#
# Options:
#   -n N      number of seats, 4-6 (default 5; setup_users.sh must have created them)
#   -H LIST   comma-separated seat numbers played by humans (default: none)
#   -S LIST   seat numbers run as SILENT bots -- they never respond, which
#             shows the moderator's timeout path
#   -- ARGS   everything after -- goes to moderator.py (timers, --seed, ...)
#
# The moderator runs in the foreground in THIS terminal. It prints every role,
# so don't show this terminal to players. Bots run in the background and log
# to a temp directory. Human seats are started by you, one terminal each, with
# the commands printed below. Ctrl-C here ends the game cleanly for everyone.

set -uo pipefail

APP_DIR=/opt/werewolf
RUN_DIR=/run/werewolf
SRC_DIR=$(dirname "$(readlink -f "$0")")

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

# --- arguments ----------------------------------------------------------------

N=5
HUMANS=""
SILENT=""
while getopts ":n:H:S:h" opt; do
    case $opt in
        n) N=$OPTARG ;;
        H) HUMANS=${OPTARG//,/ } ;;
        S) SILENT=${OPTARG//,/ } ;;
        h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
shift $((OPTIND - 1))       # getopts already consumed the "--"
MOD_ARGS=("$@")

die() { echo "error: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ $N =~ ^[4-6]$ ]] || die "-n must be 4, 5 or 6"
for s in $HUMANS $SILENT; do
    [[ $s =~ ^[0-9]+$ ]] && ((s >= 1 && s <= N)) || die "seat '$s' is not between 1 and $N"
done
for s in $HUMANS; do
    [[ " $SILENT " == *" $s "* ]] && die "seat $s can't be both human and silent"
done

# --- is the machine ready? -----------------------------------------------------

id -u modbot >/dev/null 2>&1 || die "no modbot user -- run: sudo ./setup_users.sh"
for i in $(seq 1 "$N"); do
    [[ -d $RUN_DIR/player$i ]] || die "$RUN_DIR/player$i missing (/run is wiped on reboot) -- run: sudo N_PLAYERS=$N ./setup_users.sh"
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

kind_of() {    # kind_of <seat number> -> human | silent | bot
    if [[ " $HUMANS " == *" $1 "* ]]; then echo human
    elif [[ " $SILENT " == *" $1 "* ]]; then echo silent
    else echo bot
    fi
}

echo "== werewolf: $N seats, logs in $LOG_DIR =="
for i in $(seq 1 "$N"); do
    case $(kind_of "$i") in
        human)  echo "  player$i  HUMAN   open a terminal and run:  sudo -u player$i python3 $APP_DIR/player.py player$i" ;;
        silent) echo "  player$i  silent bot" ;;
        bot)    echo "  player$i  bot" ;;
    esac
done
[[ -n $HUMANS ]] && echo "  (the moderator waits for human seats; add '-- --join-timeout 300' for more than 120s)"
echo

# --- bots (background) -----------------------------------------------------------

declare -A BOT_PID
cleanup() {
    for pid in "${BOT_PID[@]}"; do
        kill "$pid" 2>/dev/null
    done
}
trap cleanup EXIT

for i in $(seq 1 "$N"); do
    kind=$(kind_of "$i")
    [[ $kind == human ]] && continue
    mode=act
    [[ $kind == silent ]] && mode=silent
    # The bot opens --log itself, as player$i, so create the file with that
    # owner first (LOG_DIR belongs to root).
    install -o "player$i" -m 0644 /dev/null "$LOG_DIR/player$i.jsonl"
    # setsid: own session, so Ctrl-C in this terminal reaches only the
    # moderator. The bots then hear about the shutdown the proper way, via
    # the moderator's GAME_OVER. Redirecting every fd keeps sudo from
    # attaching the background job to this terminal.
    setsid sudo -u "player$i" python3 "$APP_DIR/player.py" "player$i" \
        --bot "$mode" --log "$LOG_DIR/player$i.jsonl" \
        > "$LOG_DIR/player$i.out" 2>&1 < /dev/null &
    BOT_PID[$i]=$!
done

# --- moderator (foreground) -------------------------------------------------------

# tee -i: tee ignores Ctrl-C, so it keeps copying the moderator's shutdown
# messages instead of dying and breaking the pipe under it.
sudo -u modbot python3 "$APP_DIR/moderator.py" --peercred "${MOD_ARGS[@]}" "${SEATS[@]}" 2>&1 \
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
        echo "  player$i ($(kind_of "$i")): exit status $?"
    fi
done
leftover=$(find "$RUN_DIR" -type s 2>/dev/null)
if [[ -z $leftover ]]; then
    echo "  socket files left behind: none"
else
    echo "  socket files left behind: $leftover"
fi

# Hand the logs to whoever ran sudo, so they can read them without root.
[[ -n ${SUDO_USER:-} ]] && chown -R "$SUDO_USER" "$LOG_DIR"
echo "  logs: $LOG_DIR  (moderator.out, playerN.out, playerN.jsonl = raw messages received)"

trap - EXIT
exit "$MOD_STATUS"
