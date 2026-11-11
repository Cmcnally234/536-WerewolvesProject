#!/usr/bin/env bash
# test_tls.sh -- prove the mutual-TLS rules: who gets a seat, and who doesn't.
#
#   ./test_tls.sh           # protocol checks, as your own user (no sudo needed)
#   sudo ./test_tls.sh      # ...plus key-ownership checks with the real game
#                           #    users (needs setup_users.sh first)
#
# Builds throwaway PKIs with make_certs.sh, starts tls_server.py, and runs
# tls_probe.py against it. Probe exit codes:
#   ACCEPTED (0)  REFUSED by the moderator (3)  OTHER (4)
#   UNTRUSTED (5) -- the PLAYER refused the moderator's certificate
# Each check also requires a phrase in the probe's output, so a probe that
# fails for the wrong reason (e.g. the client rejecting the server when the
# point was the server rejecting the client) doesn't count as a pass.

set -uo pipefail    # no -e: several probes are *supposed* to fail

HERE=$(dirname "$(readlink -f "$0")")
WORK=$(mktemp -d)
# 0755 so that, under sudo, the game users can reach their own files in here.
chmod 0755 "$WORK"
SERVER_PIDS=()
cleanup() {
    for pid in "${SERVER_PIDS[@]}"; do kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; done
    rm -rf "$WORK"
}
trap cleanup EXIT

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
        printf 'PASS  %-52s | %s\n' "$desc" "$(tail -n1 <<<"$out")"
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL  %-52s | wanted exit %s with "%s", got %s\n' "$desc" "$want" "$phrase" "$code"
        sed 's/^/        /' <<<"$out"
    fi
}

# start_server <name> <tls dir> [runner...] -- sets $PORT, logs to $WORK/<name>.log
start_server() {
    local name=$1 dir=$2
    shift 2
    local log=$WORK/$name.log
    "$@" python3 "$CODE/tls_server.py" player1 player2 --tls "$dir" --duration 60 >"$log" 2>&1 &
    SERVER_PIDS+=($!)
    PORT=
    for _ in $(seq 50); do
        PORT=$(sed -n 's/^PORT //p' "$log")
        [[ -n $PORT ]] && return 0
        sleep 0.1
    done
    echo "error: $name didn't start:" >&2
    cat "$log" >&2
    exit 1
}

# server_said <phrase> <description> -- the real moderator's newest _rejected
# event must contain <phrase>. Used for rejections inside the TLS handshake:
# the player only sees a hang-up there (asyncio doesn't flush the TLS alert),
# so the moderator's log is the place that says why.
server_said() {
    local phrase=$1 desc=$2 last
    sleep 0.2
    last=$(grep _rejected "$WORK/real.log" | tail -n1)
    if [[ $last == *"$phrase"* ]]; then
        PASS=$((PASS + 1))
        printf 'PASS  %-52s | %s\n' "  ...moderator: $desc" "${last#*\'reason\': }"
    else
        FAIL=$((FAIL + 1))
        printf 'FAIL  %-52s | wanted "%s", last rejection: %s\n' "  ...moderator: $desc" "$phrase" "$last"
    fi
}

probe() {   # probe <seat> <tls dir> [extra args...]
    local seat=$1 dir=$2
    shift 2
    python3 "$CODE/tls_probe.py" "$seat" --port "$PORT" --tls "$dir" "$@"
}

# The code, in a place the game users can read (the home dir is mode 700).
CODE=$WORK/code
install -d -m 0755 "$CODE"
install -m 0644 "$HERE/commslib.py" "$HERE/tls_server.py" "$HERE/tls_probe.py" "$CODE/"

# --- the PKIs ---------------------------------------------------------------
# GOOD  the game's real CA: player1, player2, and "mallory" (signed by the
#       real CA, but not a seat in this game).
# EVIL  an unrelated CA that also issued a "player1" and a "moderator".
mk() { "$HERE/make_certs.sh" -U "$@" >/dev/null; }
GOOD=$WORK/good
EVIL=$WORK/evil
mk -o "$GOOD" player1 player2 mallory
mk -o "$EVIL" player1

# A "player config dir" is just ca.crt + <seat>/{cert,key}.pem, so a stolen or
# foreign certificate is simulated by building such a dir from mixed parts.
# hybrid <dir> <ca.crt to trust> <seat> <identity dir to put in that seat>
hybrid() {
    install -d -m 0755 "$1"
    cp "$2" "$1/ca.crt"
    install -d -m 0700 "$1/$3"
    cp "$4"/cert.pem "$4"/key.pem "$1/$3/"
}
hybrid "$WORK/stolen"  "$GOOD/ca.crt" player1 "$GOOD/player2"   # player2's cert, in seat player1
hybrid "$WORK/foreign" "$GOOD/ca.crt" player1 "$EVIL/player1"   # trusts our CA, cert from EVIL
hybrid "$WORK/mallory" "$GOOD/ca.crt" mallory "$GOOD/mallory"   # real CA, not a seat
# Moderator impostors (their own dir, used by tls_server.py):
hybrid "$WORK/mod-evil"  "$EVIL/ca.crt" moderator "$EVIL/moderator"   # cert from another CA
hybrid "$WORK/mod-thief" "$GOOD/ca.crt" moderator "$GOOD/player2"     # a PLAYER's cert, real CA
# A real moderator cert, but for some other host: reissue with the wrong SAN.
cp -a "$GOOD" "$WORK/mod-wrongsan"
mk -o "$WORK/mod-wrongsan" -s DNS:elsewhere.invalid player1

echo "== the moderator's certificate =="
openssl x509 -in "$GOOD/moderator/cert.pem" -noout -subject -ext subjectAltName,extendedKeyUsage
echo

# --- the real moderator -------------------------------------------------------
start_server real "$GOOD"
echo "== players -> the real moderator (port $PORT) =="
expect 0 ACCEPTED  "player1, valid cert"                        probe player1 "$GOOD"
expect 0 ACCEPTED  "player2, valid cert"                        probe player2 "$GOOD"
expect 3 REFUSED   "no client cert"                             probe player1 "$GOOD" --no-cert
server_said PEER_DID_NOT_RETURN_A_CERTIFICATE "no certificate"
expect 3 "rejected our certificate" "cert from a different CA"  probe player1 "$WORK/foreign"
server_said CERTIFICATE_VERIFY_FAILED "certificate not from our CA"
expect 3 "is for 'player2', not 'player1'" "player2's cert used for seat player1" \
                                                                probe player1 "$WORK/stolen"
expect 3 "not a seat"  "real-CA cert for a name with no seat"   probe mallory "$WORK/mallory"
expect 3 REFUSED   "plain TCP, no TLS at all"                   probe player1 "$GOOD" --plain
server_said WRONG_VERSION_NUMBER "not TLS"
# Take seat player1 and hold it, then try to take it again.
probe player1 "$GOOD" --hold 3 >/dev/null 2>&1 &
HOLDER=$!
sleep 0.5
expect 3 "seat already taken" "second connection for a taken seat"  probe player1 "$GOOD"
wait $HOLDER
echo

# --- impostor moderators: the PLAYER must refuse them -------------------------
echo "== player1 -> impostor moderators =="
start_server mod-evil "$WORK/mod-evil"
expect 5 UNTRUSTED "moderator cert from a different CA"         probe player1 "$GOOD"
start_server mod-thief "$WORK/mod-thief"
expect 5 UNTRUSTED "moderator using a player's cert"            probe player1 "$GOOD"
start_server mod-wrongsan "$WORK/mod-wrongsan"
expect 5 "mismatch" "moderator cert for another hostname"       probe player1 "$GOOD"
echo

# --- key ownership (root only) --------------------------------------------------
if [[ $EUID -eq 0 ]]; then
    if id -u modbot >/dev/null 2>&1 && id -u player1 >/dev/null 2>&1 && id -u player2 >/dev/null 2>&1; then
        echo "== key ownership, as the real game users =="
        OWNED=$WORK/owned
        "$HERE/make_certs.sh" -o "$OWNED" player1 player2 >/dev/null
        stat -c '%U:%G %a %n' "$OWNED"/ca.crt "$OWNED"/ca.key "$OWNED"/moderator \
            "$OWNED"/moderator/key.pem "$OWNED"/player1 "$OWNED"/player1/key.pem \
            "$OWNED"/player1/cert.pem "$OWNED"/player2/key.pem
        expect 0 "player1 600" "player1's key: owner player1, mode 0600" \
            stat -c '%U %a' "$OWNED/player1/key.pem"
        expect 0 "player2 600" "player2's key: owner player2, mode 0600" \
            stat -c '%U %a' "$OWNED/player2/key.pem"
        expect 0 "modbot 600"  "moderator's key: owner modbot, mode 0600" \
            stat -c '%U %a' "$OWNED/moderator/key.pem"
        expect 1 "Permission denied" "player2 can't read player1's key" \
            sudo -u player2 cat "$OWNED/player1/key.pem"
        expect 1 "Permission denied" "player1 can't read the CA key" \
            sudo -u player1 cat "$OWNED/ca.key"
        start_server owned "$OWNED" sudo -u modbot
        expect 0 ACCEPTED "player1 as Linux user player1, own key" \
            sudo -u player1 python3 "$CODE/tls_probe.py" player1 --port "$PORT" --tls "$OWNED"
        expect 4 "Permission denied" "Linux user player2 posing as player1" \
            sudo -u player2 python3 "$CODE/tls_probe.py" player1 --port "$PORT" --tls "$OWNED"
        echo
    else
        echo "(skipping key-ownership checks: run setup_users.sh first)"
        echo
    fi
else
    echo "(key-ownership checks need root: sudo ./test_tls.sh)"
    echo
fi

echo "== the real moderator's log =="
sleep 0.3
cat "$WORK/real.log"

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
