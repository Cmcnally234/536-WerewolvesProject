#!/usr/bin/env bash
# deploy_remote.sh -- install the game code and the given seats' certificates
# on another machine, over ssh.
#
#   ./deploy_remote.sh alice@192.168.1.21 player3 player4
#   ./deploy_remote.sh -p 2222 bob@lab5 player5
#   ./deploy_remote.sh carol@mod2 moderator        # a separate moderator host
#
# Run it as your normal user on the machine that holds the certificates
# (make_certs.sh's output; reading the keys there may ask for your sudo
# password). On HOST you need an account that can sudo: you'll be asked for
# that password once, by the install step.
#
# Options:
#   -p PORT   ssh port (default 22)
#   -i FILE   ssh identity (private key) file
#   -t DIR    local certificate directory (default /etc/werewolf/tls)
#
# What ends up on HOST:
#   Linux users for the seats (moderator -> modbot), group werewolf, if missing
#   /opt/werewolf/                   root:werewolf 0750  the game code + run_players.sh
#   /etc/werewolf/tls/ca.crt         root          0644  to check the moderator
#   /etc/werewolf/tls/<seat>/        <seat>        0700
#   /etc/werewolf/tls/<seat>/cert.pem              0644
#   /etc/werewolf/tls/<seat>/key.pem <seat>        0600  only these seats' keys
#
# Never copied: ca.key, other seats' keys. Re-running updates the code and
# re-installs the certificates (e.g. after make_certs.sh reissued them).
# Re-run it after EVERY code change: hosts with different commslib.py
# versions don't speak the same protocol.

set -euo pipefail

SSH_PORT=22
SSH_ID=()
TLS=/etc/werewolf/tls
usage() { sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; }
while getopts ":p:i:t:h" opt; do
    case $opt in
        p) SSH_PORT=$OPTARG ;;
        i) SSH_ID=(-i "$OPTARG") ;;
        t) TLS=$OPTARG ;;
        h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
shift $((OPTIND - 1))
die() { echo "error: $*" >&2; exit 1; }
[[ $# -ge 2 ]] || { usage >&2; exit 2; }
TARGET=$1
shift
IDS=("$@")

SRC_DIR=$(dirname "$(readlink -f "$0")")
# What a remote host runs. No tests: those stay on the development machine.
CODE=(commslib.py roles.py player.py moderator.py run_players.sh)

for id in "${IDS[@]}"; do
    [[ $id =~ ^[A-Za-z0-9_.-]+$ ]] || die "bad seat name '$id'"
done

# --- 1. build the bundle locally -----------------------------------------------

# umask 077: the bundle holds private keys, so everything in it starts out
# readable by us alone (the same reason make_certs.sh does this).
umask 077
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/code" "$STAGE/tls"
for f in "${CODE[@]}"; do
    cp "$SRC_DIR/$f" "$STAGE/code/"
done

# The identity directories belong to their own users (mode 0700), so
# reading them usually takes root. tar keeps each file's mode; extracting
# as ourselves makes us the owner of the copies (inside our 0700 bundle).
files=(ca.crt "${IDS[@]}")
# The certificate directory itself is world-listable (0755), so a missing
# seat can be reported clearly before the tar below, which may need root.
for id in "${IDS[@]}"; do
    [[ -d $TLS/$id ]] || die "no certificate for '$id' in $TLS -- run make_certs.sh"
done
all_readable() {
    [[ -r $TLS/ca.crt ]] || return 1
    for id in "${IDS[@]}"; do [[ -r $TLS/$id/key.pem ]] || return 1; done
}
if all_readable; then
    tar -C "$TLS" -cf - "${files[@]}" | tar -C "$STAGE/tls" -xf -
else
    echo "reading the keys in $TLS needs root:"
    sudo tar -C "$TLS" -cf - "${files[@]}" | tar -C "$STAGE/tls" -xf -
fi
for id in "${IDS[@]}"; do
    [[ -f $STAGE/tls/$id/cert.pem && -f $STAGE/tls/$id/key.pem ]] ||
        die "no certificate for '$id' in $TLS -- run make_certs.sh"
done

# The install step, run on HOST as root. Arguments: the seats.
cat > "$STAGE/install.sh" <<'INSTALL'
#!/usr/bin/env bash
set -euo pipefail
BUNDLE=$(dirname "$(readlink -f "$0")")
# The bundle holds private keys: remove it whatever happens.
trap 'rm -rf "$BUNDLE"' EXIT
APP=/opt/werewolf
TLS=/etc/werewolf/tls
GROUP=werewolf
NOLOGIN=$(command -v nologin || echo /sbin/nologin)

# commslib needs 3.11+ (asyncio.wait_for -> TimeoutError, start_tls).
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    echo "error: $(hostname) needs python3 3.11 or newer (has: $(python3 --version 2>&1))" >&2
    exit 1
fi

user_for() { if [[ $1 == moderator ]]; then echo modbot; else echo "$1"; fi; }

getent group "$GROUP" >/dev/null || groupadd --system "$GROUP"
for id in "$@"; do
    user=$(user_for "$id")
    if ! id -u "$user" >/dev/null 2>&1; then
        # Same kind of account as setup_users.sh makes: a system user that
        # exists only to run one process via sudo -u, never to log in.
        useradd --system --user-group --no-create-home --home-dir "$APP" \
                --shell "$NOLOGIN" "$user"
        echo "created user $user"
    fi
    usermod -aG "$GROUP" "$user"
done

install -d -o root -g "$GROUP" -m 0750 "$APP"
install -o root -g "$GROUP" -m 0640 "$BUNDLE"/code/*.py "$APP"/
install -o root -g "$GROUP" -m 0750 "$BUNDLE"/code/run_players.sh "$APP"/

install -d -o root -g root -m 0755 /etc/werewolf "$TLS"
install -o root -g root -m 0644 "$BUNDLE/tls/ca.crt" "$TLS/ca.crt"
for id in "$@"; do
    user=$(user_for "$id")
    install -d -o "$user" -g "$user" -m 0700 "$TLS/$id"
    install -o "$user" -g "$user" -m 0644 "$BUNDLE/tls/$id/cert.pem" "$TLS/$id/cert.pem"
    install -o "$user" -g "$user" -m 0600 "$BUNDLE/tls/$id/key.pem" "$TLS/$id/key.pem"
done

if command -v restorecon >/dev/null; then
    restorecon -R "$APP" /etc/werewolf || true
fi

echo "== installed on $(hostname) =="
ls -l "$APP"
for id in "$@"; do
    stat -c '  %U:%G %a %n' "$TLS/$id" "$TLS/$id/key.pem"
done
echo "  commslib.py $(sha256sum "$APP/commslib.py" | cut -c1-16)"
INSTALL

# --- 2. copy it over and install --------------------------------------------------

SSH=(ssh -p "$SSH_PORT" "${SSH_ID[@]}")
SCP=(scp -q -P "$SSH_PORT" "${SSH_ID[@]}")
echo "== deploying ${IDS[*]} to $TARGET =="
# A private directory on HOST to receive the bundle (mktemp makes it 0700).
REMOTE=$("${SSH[@]}" "$TARGET" 'mktemp -d /tmp/werewolf-deploy.XXXXXX')
# -r copies the bundle; -p keeps the modes, so the keys stay 0600 in transit
# (scp itself encrypts them on the wire, like everything over ssh).
"${SCP[@]}" -rp "$STAGE/." "$TARGET:$REMOTE/"
# -t: a terminal, so sudo on HOST can ask for a password if it needs one.
status=0
"${SSH[@]}" -t "$TARGET" "sudo bash $REMOTE/install.sh ${IDS[*]}" || status=$?
# install.sh deletes the bundle itself; this covers it never having run
# (e.g. a wrong sudo password), so no copy of the keys is left in /tmp.
"${SSH[@]}" "$TARGET" "rm -rf $REMOTE" || true
((status == 0)) || die "install on $TARGET failed (exit $status)"
echo "  local commslib.py $(sha256sum "$SRC_DIR/commslib.py" | cut -c1-16)  (must match the line above)"
