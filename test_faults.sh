#!/usr/bin/env bash
# test_faults.sh -- fault injection on the podman LAN (see cluster.sh).
#
#   ./test_faults.sh          # needs podman (rootless is fine), no sudo
#   KEEP=1 ./test_faults.sh   # leave the containers running afterwards
#
# Three whole games across three "machines" (moderator + player1-2 on
# ww-mod, player3-4 on ww-pa, player5-6 on ww-pb), each with one fault:
#
#   A  latency + loss   tc netem on every host's eth0: 150ms +-50ms delay
#                       and 10% loss each way. The game must simply finish.
#   B  mid-night        iptables DROP between ww-pa and the moderator the
#                       moment night 1 starts (the seer, player3, is on ww-pa).
#   C  mid-vote         the same for a host with voters, the moment voting opens.
#
# For B and C: the moderator must notice within the heartbeat bound, the
# phase must time out (no-op / abstain for the cut-off seats), the players
# must resume once the partition heals, and the game must finish -- with
# every player, partitioned or not, holding exactly the same broadcasts.
#
# iptables DROP silently discards packets: no RST, no ICMP. Neither TCP
# stack hears anything is wrong; only the application heartbeats can tell.

set -uo pipefail
HERE=$(dirname "$(readlink -f "$0")")
# shellcheck source=cluster.sh
source "$HERE/cluster.sh"
command -v podman >/dev/null || { echo "error: needs podman" >&2; exit 1; }

HB=1                # heartbeat interval everywhere: dead after 3 x 1s of silence
LIMIT=3
SLACK=1.5           # polling + process scheduling on top of a bound
# Seed 19 deals: player1 wolf, player2 doctor, player3 SEER, player4 villager,
# player5 wolf, player6 villager -- so night 1 always waits for ww-pa.
MOD_ARGS=(--seed 19 --join-timeout 60 --night-timeout 8 --discussion 2 --vote-timeout 6
          --resume-timeout 60 --heartbeat "$HB" --max-rounds 6)
# --bot-think 2: remote bots answer 2s after a prompt, so a partition made
# when the phase starts is in place before they've acted.
BOT_ARGS=(--heartbeat "$HB" --bot-think 2)

WORK=$(mktemp -d)
trap 'cluster_down; rm -rf "$WORK"' EXIT
PASS=0
FAIL=0
check() {    # check <description> <command...>   (passes if the command succeeds)
    local desc=$1
    shift
    local out
    if out=$("$@" 2>&1); then
        PASS=$((PASS + 1))
        printf 'PASS  %-60s | %s\n' "$desc" "$(tail -n1 <<<"$out" | cut -c1-80)"
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL  %-60s\n' "$desc"
        sed 's/^/        /' <<<"$out" | tail -n 15
    fi
}
now() { date +%s.%N; }
elapsed() {     # elapsed <from> <to>; "n/a" (which then fails any check) if one is missing
    [[ -n ${1:-} && -n ${2:-} ]] || { echo n/a; return; }
    python3 -c "print(f'{$2 - $1:.2f}')"
}
within() { python3 -c "import sys; sys.exit(not $1 < $2)"; }  # within <x> <bound>

# wait_for <file> <text> <seconds> -- wait until <text> is in <file>; prints
# the moment it was seen (returns 1 if it never was).
wait_for() {
    local end=$(($(date +%s) + $3))
    while (($(date +%s) < end)); do
        grep -qF -- "$2" "$1" 2>/dev/null && { now; return 0; }
        sleep 0.05
    done
    echo "never saw '$2' in $(basename "$1")" >&2
    return 1
}

partition() {   # partition <host> -- silently drop everything to/from the moderator
    cx "$1" iptables -I INPUT -s "$MOD_IP" -j DROP
    cx "$1" iptables -I OUTPUT -d "$MOD_IP" -j DROP
}
heal() {
    cx "$1" iptables -D INPUT -s "$MOD_IP" -j DROP
    cx "$1" iptables -D OUTPUT -d "$MOD_IP" -j DROP
}

# start_game <name>: moderator + player1-2 on ww-mod, remote bots elsewhere.
start_game() {
    G=$WORK/$1
    mkdir -p "$G"
    cx ww-mod /src/run_game.sh -n 6 --host ww-mod --port 5555 -R 3,4,5,6 -- "${MOD_ARGS[@]}" \
        > "$G/mod.out" 2>&1 &
    MOD_PID=$!
    wait_for "$G/mod.out" "waiting for" 30 >/dev/null || return 1
    cx ww-pa /opt/werewolf/run_players.sh --host ww-mod --port 5555 player3 player4 \
        -- "${BOT_ARGS[@]}" > "$G/pa.out" 2>&1 &
    PA_PID=$!
    cx ww-pb /opt/werewolf/run_players.sh --host ww-mod --port 5555 player5 player6 \
        -- "${BOT_ARGS[@]}" > "$G/pb.out" 2>&1 &
    PB_PID=$!
}

# finish_game: wait for everyone, then copy every player's message log here.
finish_game() {
    wait "$MOD_PID"; MOD_STATUS=$?
    wait "$PA_PID"; PA_STATUS=$?
    wait "$PB_PID"; PB_STATUS=$?
    local d
    d=$(sed -n 's/.*logs in \([^ ]*\) ==.*/\1/p' "$G/mod.out" | head -n1)
    for p in player1 player2; do cx ww-mod cat "$d/$p.jsonl" > "$G/$p.jsonl"; done
    d=$(sed -n 's/.*logs in \([^ ]*\) ==.*/\1/p' "$G/pa.out" | head -n1)
    for p in player3 player4; do cx ww-pa cat "$d/$p.jsonl" > "$G/$p.jsonl"; done
    d=$(sed -n 's/.*logs in \([^ ]*\) ==.*/\1/p' "$G/pb.out" | head -n1)
    for p in player5 player6; do cx ww-pb cat "$d/$p.jsonl" > "$G/$p.jsonl"; done
}

# Every player -- local, remote, partitioned or not, dead or alive -- must
# end up with the same broadcasts, in the same order, each exactly once.
cat > "$WORK/same_broadcasts.py" <<'EOF'
import json, sys
game = sys.argv[1]
BROADCASTS = ("PHASE", "DAY_ANNOUNCE", "VOTE_TALLY", "GAME_OVER")
seq = {}
for i in range(1, 7):
    log = [json.loads(l) for l in open(f"{game}/player{i}.jsonl") if l.strip()]
    if not log or log[-1]["type"] != "GAME_OVER":
        sys.exit(f"player{i} never got GAME_OVER")
    seq[f"player{i}"] = [json.dumps(m, sort_keys=True) for m in log if m["type"] in BROADCASTS]
ref = seq["player1"]
for p, s in seq.items():
    if s != ref:
        sys.exit(f"{p}'s broadcasts differ from player1's ({len(s)} vs {len(ref)})")
print(f"all 6 players: the same {len(ref)} broadcasts, ending in GAME_OVER")
EOF
same_broadcasts() { python3 "$WORK/same_broadcasts.py" "$G"; }
game_finished() {
    grep "GAME OVER" "$G/mod.out" && ((MOD_STATUS == 0 && PA_STATUS == 0 && PB_STATUS == 0))
}
has_event() {   # has_event <player> <type> -- the player's log has a message of that type
    grep -q "\"type\": \"$2\"" "$G/$1.jsonl" && echo "$1 got $2"
}

echo "== cluster: ${CLUSTER_HOSTS[*]}, 6 seats =="
cluster_up || exit 1
cluster_provision 6 || { echo "error: provisioning failed" >&2; exit 1; }
# First line only: /etc/hosts and the network's DNS can both answer.
MOD_IP=$(cx ww-pa getent hosts ww-mod | awk 'NR == 1 {print $1}')
echo "moderator at ww-mod = $MOD_IP; player3-4 on ww-pa, player5-6 on ww-pb"
echo

# --- A: latency + loss -------------------------------------------------------------

echo "== A: latency + loss (tc netem: 150ms +-50ms, 10% loss, on every host) =="
for h in "${CLUSTER_HOSTS[@]}"; do
    cx "$h" tc qdisc replace dev eth0 root netem delay 150ms 50ms loss 10%
done
# Round trip ww-pa -> ww-mod: a TCP connect to sshd is one SYN / SYN-ACK.
rtt=$(cx ww-pa python3 -c '
import socket, statistics, time
samples = []
for _ in range(7):
    t = time.monotonic()
    socket.create_connection(("ww-mod", 22), 10).close()
    samples.append(time.monotonic() - t)
print(f"{statistics.median(samples) * 1000:.0f}")')
check "netem is in effect: median round trip ww-pa <-> ww-mod ${rtt}ms" within 200 "$rtt"
start_game A
finish_game
for h in "${CLUSTER_HOSTS[@]}"; do cx "$h" tc qdisc del dev eth0 root; done
check "the game finished; every process exited 0" game_finished
check "no session ended (nobody was dropped for good)" \
    bash -c "! grep -E 'player[3-6] disconnected' '$G/mod.out' && echo 'none'"
away=$(grep -c " is away" "$G/mod.out")
check "every player got the same broadcasts ($away link drops survived)" same_broadcasts
echo

# --- B: partition mid-night ------------------------------------------------------------

echo "== B: partition ww-pa (player3 = seer, player4) the moment night 1 starts =="
start_game B
t_night=$(wait_for "$G/mod.out" "--- night 1 ---" 60)
partition ww-pa
t_cut=$(now)
echo "  cut ww-pa off $(elapsed "$t_night" "$t_cut")s into night 1"
t_away=$(wait_for "$G/mod.out" "player3 is away" 15)
t_timeout=$(wait_for "$G/mod.out" "night 1: timed out" 20)
sleep 1
heal ww-pa
t_heal=$(now)
t_back=$(wait_for "$G/mod.out" "player3 is back" 30)
wait_for "$G/mod.out" "player4 is back" 30 >/dev/null
finish_game
noticed=$(elapsed "$t_cut" "$t_away")
resumed=$(elapsed "$t_heal" "$t_back")
check "moderator noticed in ${noticed}s (bound ${LIMIT}s + ${SLACK}s)" \
    within "$noticed" "$(python3 -c "print($LIMIT + $SLACK)")"
check "it was the heartbeat that noticed" grep "player3 is away (no heartbeat" "$G/mod.out"
check "night 1 timed out, with a no-op for the seer" \
    bash -c "grep 'night 1: timed out' '$G/mod.out' | grep player3"
check "player3 resumed ${resumed}s after the partition healed" \
    bash -c "grep 'player3 is back' '$G/mod.out'"
check "player3 saw itself reconnect" has_event player3 _resumed
check "player3 got its 'Time's up' notice (sent while it was cut off)" \
    grep -h "Time's up" "$G/player3.jsonl"
check "the game finished; every process exited 0" game_finished
check "every player got the same broadcasts" same_broadcasts
echo "  (the whole partition lasted $(elapsed "$t_cut" "$t_heal")s; night 1 ended $(elapsed "$t_night" "$t_timeout")s in)"
echo

# --- C: partition mid-vote ---------------------------------------------------------------

echo "== C: partition a host with voters the moment voting opens =="
start_game C
t_vote=$(wait_for "$G/mod.out" "--- vote 1 ---" 90)
voters=$(grep -m1 -- "--- vote 1 ---" "$G/mod.out")
if [[ $voters == *player5* || $voters == *player6* ]]; then
    host=ww-pb; seats=(player5 player6)
else
    host=ww-pa; seats=(player3 player4)
fi
partition "$host"
t_cut=$(now)
cut=()   # the cut-off seats that were supposed to vote
for s in "${seats[@]}"; do [[ $voters == *"'$s'"* ]] && cut+=("$s"); done
echo "  cut $host (${cut[*]} voting) off $(elapsed "$t_vote" "$t_cut")s into the vote"
t_away=$(wait_for "$G/mod.out" "${cut[0]} is away" 15)
t_timeout=$(wait_for "$G/mod.out" "day 1: vote timed out" 20)
sleep 1
heal "$host"
t_heal=$(now)
t_back=$(wait_for "$G/mod.out" "${cut[0]} is back" 30)
finish_game
noticed=$(elapsed "$t_cut" "$t_away")
resumed=$(elapsed "$t_heal" "$t_back")
check "moderator noticed in ${noticed}s (bound ${LIMIT}s + ${SLACK}s)" \
    within "$noticed" "$(python3 -c "print($LIMIT + $SLACK)")"
check "the vote timed out, ${cut[*]} counted as abstaining" \
    bash -c "line=\$(grep 'day 1: vote timed out' '$G/mod.out'); for s in ${cut[*]}; do [[ \$line == *\$s* ]] || exit 1; done; echo \"\$line\""
check "${cut[0]} resumed ${resumed}s after the partition healed" \
    bash -c "grep '${cut[0]} is back' '$G/mod.out'"
check "${cut[0]} saw itself reconnect" has_event "${cut[0]}" _reconnecting
check "the game finished; every process exited 0" game_finished
check "every player got the same broadcasts" same_broadcasts
echo

echo "== moderator log of game B (the mid-night partition) =="
grep -E "night 1|player3|player4|GAME OVER" "$WORK/B/mod.out" | grep -v " is \(seer\|villager\|werewolf\|doctor\)$" | sed 's/^/  /'
echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
