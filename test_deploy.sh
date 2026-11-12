#!/usr/bin/env bash
# test_deploy.sh -- multi-host deployment, end to end, on a podman "LAN".
#
#   ./test_deploy.sh          # needs podman (rootless is fine), no sudo
#   KEEP=1 ./test_deploy.sh   # leave the containers running afterwards
#
# Three containers stand in for three machines (see cluster.sh):
#   ww-mod  runs setup_users.sh and make_certs.sh, then deploy_remote.sh (as
#           an ordinary admin user, over real ssh/scp/sudo) to the others
#   ww-pa   gets player3 + player4      ww-pb   gets player5
# Then one game runs across all three: the moderator and player1-2 on ww-mod,
# the rest on their own hosts, over TCP + mutual TLS between the containers.

set -uo pipefail
HERE=$(dirname "$(readlink -f "$0")")
# shellcheck source=cluster.sh
source "$HERE/cluster.sh"
command -v podman >/dev/null || { echo "error: needs podman" >&2; exit 1; }

PASS=0
FAIL=0
# expect <exit code> <phrase the output must contain> <description> <command...>
expect() {
    local want=$1 phrase=$2 desc=$3
    shift 3
    local out code
    out=$("$@" 2>&1)
    code=$?
    if [[ $code -eq $want && $out == *"$phrase"* ]]; then
        PASS=$((PASS + 1))
        printf 'PASS  %-58s | %s\n' "$desc" "$(grep -F -- "$phrase" <<<"$out" | head -n1 | cut -c1-90)"
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL  %-58s | wanted exit %s with "%s", got %s\n' "$desc" "$want" "$phrase" "$code"
        sed 's/^/        /' <<<"$out" | tail -n 25
    fi
}

WORK=$(mktemp -d)
trap 'cluster_down; rm -rf "$WORK"' EXIT
echo "== starting the cluster: ${CLUSTER_HOSTS[*]} =="
cluster_up || exit 1

echo "== ww-mod: setup_users.sh + make_certs.sh =="
cx ww-mod bash -c 'cd /src && ./setup_users.sh >/dev/null' || exit 1
# The moderator's certificate names the address players will dial: ww-mod.
cx ww-mod bash -c 'cd /src && ./make_certs.sh -s DNS:ww-mod -s DNS:localhost -s IP:127.0.0.1' | sed 's/^/  /'
echo

echo "== deploy_remote.sh, run by admin on ww-mod =="
expect 0 "player3" "deploy player3 + player4 -> ww-pa" \
    cx_admin ww-mod /src/deploy_remote.sh admin@ww-pa player3 player4
expect 0 "player5" "deploy player5 -> ww-pb" \
    cx_admin ww-mod /src/deploy_remote.sh admin@ww-pb player5
expect 0 "player4" "re-deploy to ww-pa (idempotent)" \
    cx_admin ww-mod /src/deploy_remote.sh admin@ww-pa player3 player4
expect 1 "no certificate for 'player9'" "deploy a seat that has no certificate" \
    cx_admin ww-mod /src/deploy_remote.sh admin@ww-pa player9
echo

echo "== what landed on ww-pa =="
cx ww-pa bash -c 'ls -l /opt/werewolf; find /etc/werewolf | sort | xargs stat -c "%U:%G %a %n"' | sed 's/^/  /'
expect 0 "player3 600" "player3's key: owner player3, mode 0600" \
    cx ww-pa stat -c '%U %a' /etc/werewolf/tls/player3/key.pem
expect 0 "player3 700" "player3's directory: owner player3, mode 0700" \
    cx ww-pa stat -c '%U %a' /etc/werewolf/tls/player3
expect 0 "ca.crt player3 player4" "only ca.crt + this host's seats were copied" \
    cx ww-pa bash -c 'echo $(ls /etc/werewolf/tls)'
expect 1 "" "no ca.key, moderator key or player5 key on ww-pa" \
    cx ww-pa bash -c 'find / -xdev \( -name ca.key -o -path "*moderator/key.pem" -o -path "*player5*" \) 2>/dev/null | grep .'
expect 1 "Permission denied" "player4 can't read player3's key" \
    cx ww-pa sudo -u player4 cat /etc/werewolf/tls/player3/key.pem
expect 1 "" "no key bundle left behind in /tmp" \
    cx ww-pa bash -c 'ls -d /tmp/werewolf-deploy.* 2>/dev/null | grep .'
expect 0 "$(cx ww-mod sha256sum /src/commslib.py | cut -c1-64)" "same commslib.py on ww-pa as on ww-mod" \
    cx ww-pa sha256sum /opt/werewolf/commslib.py
echo

echo "== run_game.sh refuses obvious mistakes =="
expect 1 "doesn't list '10.9.9.9'" "--host that isn't in the moderator's certificate" \
    cx ww-mod /src/run_game.sh --host 10.9.9.9 --port 5555 -R 3
expect 1 "can't reach 127.0.0.1" "remote seats with a loopback --host" \
    cx ww-mod /src/run_game.sh --port 5555 -R 3
expect 1 "must be a number" "a --port that isn't a number" \
    cx ww-mod /src/run_game.sh --port 55x5
echo

echo "== one game across three hosts =="
TIMERS=(--join-timeout 60 --night-timeout 5 --discussion 1 --vote-timeout 3 --seed 7)
cx ww-mod /src/run_game.sh --host ww-mod --port 5555 -R 3,4,5 -- "${TIMERS[@]}" \
    > $WORK/mod.out 2>&1 &
MOD=$!
sleep 1
cx ww-pa /opt/werewolf/run_players.sh --host ww-mod --port 5555 player3 player4 \
    > $WORK/pa.out 2>&1 &
PA=$!
cx ww-pb /opt/werewolf/run_players.sh --host ww-mod --port 5555 player5 \
    > $WORK/pb.out 2>&1 &
PB=$!
# Snapshot the processes as soon as every seat has joined (a game can be
# over in a couple of seconds).
for _ in $(seq 100); do
    grep -q "joined (5/5)" "$WORK/mod.out" 2>/dev/null && break
    sleep 0.1
done
echo "-- processes, one Linux user per player, on each host (mid-game) --"
for h in "${CLUSTER_HOSTS[@]}"; do
    cx "$h" ps -o user=,pid=,args= -C python3 | grep -v sudo | sed "s/^/  $h  /" | cut -c1-110
done
wait $MOD; mod_status=$?
wait $PA; pa_status=$?
wait $PB; pb_status=$?
echo "-- moderator (ww-mod) --"
grep -E "listening|joined|GAME OVER|exit status|player[0-9]" $WORK/mod.out | grep -v " is " | sed 's/^/  /'
echo "-- ww-pa / ww-pb --"
grep -E "exit status" $WORK/pa.out $WORK/pb.out | sed 's/^/  /'

expect 0 "GAME OVER" "the game finished (moderator exit 0)" \
    bash -c "grep 'GAME OVER' $WORK/mod.out; exit $mod_status"
expect 0 "joined (5/5)" "all five seats joined, three of them from other hosts" \
    grep "joined (5/5)" $WORK/mod.out
expect 0 "player4 (bot): exit status 0" "ww-pa's players finished cleanly" \
    bash -c "cat $WORK/pa.out; exit $pa_status"
expect 0 "player5 (bot): exit status 0" "ww-pb's player finished cleanly" \
    bash -c "cat $WORK/pb.out; exit $pb_status"
expect 0 '"GAME_OVER"' "player5 on ww-pb received GAME_OVER" \
    cx ww-pb bash -c 'tail -n 1 /tmp/werewolf-players.*/player5.jsonl'

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
