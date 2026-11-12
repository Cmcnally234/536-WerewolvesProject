#!/usr/bin/env bash
# make_certs.sh -- build the game's private PKI: a CA, a moderator certificate
# and one certificate per player.
#
#   sudo ./make_certs.sh                          # player1..player5, /etc/werewolf/tls
#   sudo N_PLAYERS=4 ./make_certs.sh
#   sudo ./make_certs.sh -s DNS:mod.lan -s IP:192.168.1.10   # moderator's names
#   ./make_certs.sh -o /tmp/pki p1 p2 p3 p4       # dev: no root, nothing chowned
#
# Options:
#   -o DIR   output directory (default /etc/werewolf/tls)
#   -s SAN   a subjectAltName for the moderator certificate, e.g. DNS:host or
#            IP:10.0.0.5. Repeatable. If given, these REPLACE the defaults
#            (DNS:localhost, IP:127.0.0.1, DNS:$(hostname)). Players check
#            the address they connect to against this list, so it must
#            include every name or IP players will use for the moderator.
#   -f       make a new CA even if one exists (invalidates every old cert)
#   -U       don't chown (as root: for test PKIs whose names aren't users)
#   NAME...  player names (default player1..player$N_PLAYERS). Each becomes
#            the certificate's CN, which IS the seat: the moderator seats a
#            connection by the CN in its verified certificate.
#
# Re-running keeps the existing CA and reissues the named certificates, so
# certificates for other names stay valid.
#
# Layout (owners apply when run as root without -U):
#   DIR/                       root      0755
#   DIR/ca.crt                 root      0644  everyone trusts this
#   DIR/ca.key                 root      0600  signs certs; never copied anywhere
#   DIR/moderator/             modbot    0700
#   DIR/moderator/cert.pem               0644
#   DIR/moderator/key.pem      modbot    0600
#   DIR/playerN/               playerN   0700  (same pattern)
#
# commslib.tls_paths() knows this layout; keep the two in sync.

set -euo pipefail

# Every file this script creates starts out private (0600 / 0700). Keys are
# therefore never readable by anyone else, not even for the moment between
# openssl writing them and our chmod. Public files are opened up explicitly.
umask 077

OUT=/etc/werewolf/tls
SANS=()
FORCE=0
CHOWN=1
while getopts "o:s:fUh" opt; do
    case $opt in
        o) OUT=$OPTARG ;;
        s) SANS+=("$OPTARG") ;;
        f) FORCE=1 ;;
        U) CHOWN=0 ;;
        *) sed -n '2,25p' "$0"; exit 2 ;;
    esac
done
shift $((OPTIND - 1))

if [[ $# -gt 0 ]]; then
    NAMES=("$@")
else
    NAMES=()
    for i in $(seq 1 "${N_PLAYERS:-5}"); do NAMES+=("player$i"); done
fi
if [[ ${#SANS[@]} -eq 0 ]]; then
    SANS=("DNS:localhost" "IP:127.0.0.1" "DNS:$(hostname)")
fi

# Only root can give a file away. A non-root run (development, tests) leaves
# everything owned by the caller, still with the same private modes.
if [[ $EUID -ne 0 ]]; then
    CHOWN=0
fi

MOD_ID=moderator    # commslib.MODERATOR_ID
MOD_USER=modbot

for name in "$MOD_ID" "${NAMES[@]}"; do
    # The name becomes a directory and a certificate subject: keep it boring.
    if [[ ! $name =~ ^[A-Za-z0-9_.-]+$ ]]; then
        echo "error: bad name '$name' (letters, digits, _ . - only)" >&2
        exit 2
    fi
done
for name in "${NAMES[@]}"; do
    if [[ $name == "$MOD_ID" ]]; then
        echo "error: '$MOD_ID' is the moderator's identity, not a player name" >&2
        exit 2
    fi
done
if [[ $CHOWN -eq 1 ]]; then
    for user in "$MOD_USER" "${NAMES[@]}"; do
        if ! id -u "$user" >/dev/null 2>&1; then
            echo "error: no Linux user '$user' to own its key (run setup_users.sh, or -U)" >&2
            exit 1
        fi
    done
fi

# umask 022 just for this: under the script's 077, mkdir -p would also make
# any missing PARENT (e.g. /etc/werewolf) 0700 root -- and then nobody but
# root could reach the certificates inside, whatever their own modes.
(umask 022; mkdir -p "$OUT")
chmod 0755 "$OUT"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# --- the CA -----------------------------------------------------------------

if [[ $FORCE -eq 1 || ! -f $OUT/ca.key || ! -f $OUT/ca.crt ]]; then
    # EC P-256: small, fast keys that every TLS 1.3 stack supports.
    # pathlen:0 -- this CA may sign leaf certificates but not other CAs.
    openssl req -x509 -new -noenc -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
        -keyout "$OUT/ca.key" -out "$OUT/ca.crt" -days 3650 \
        -subj "/CN=Werewolf game CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" 2>/dev/null
    echo "created CA $OUT/ca.crt"
else
    echo "reusing CA $OUT/ca.crt"
fi
chmod 0644 "$OUT/ca.crt"
chmod 0600 "$OUT/ca.key"

# --- leaf certificates ------------------------------------------------------

# issue NAME USAGE [SAN_LIST]
#   USAGE is the extendedKeyUsage: serverAuth for the moderator, clientAuth
#   for players. OpenSSL enforces it during the handshake, so a player's
#   certificate can't be used to impersonate the moderator, and vice versa.
issue() {
    local name=$1 usage=$2 sans=${3:-}
    local dir=$OUT/$name
    mkdir -p "$dir"
    chmod 0700 "$dir"
    {
        echo "basicConstraints=critical,CA:FALSE"
        echo "keyUsage=critical,digitalSignature"
        echo "extendedKeyUsage=$usage"
        echo "subjectKeyIdentifier=hash"
        echo "authorityKeyIdentifier=keyid,issuer"
        if [[ -n $sans ]]; then echo "subjectAltName=$sans"; fi
    } >"$TMP/$name.ext"
    openssl req -new -noenc -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
        -keyout "$dir/key.pem" -out "$TMP/$name.csr" -subj "/CN=$name" 2>/dev/null
    # A random 128-bit serial: certificates from the same CA must never share one.
    openssl x509 -req -in "$TMP/$name.csr" -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" \
        -set_serial "0x$(openssl rand -hex 16)" -days 825 \
        -extfile "$TMP/$name.ext" -out "$dir/cert.pem" 2>/dev/null
    chmod 0644 "$dir/cert.pem"
    chmod 0600 "$dir/key.pem"
}

own() {   # own NAME USER -- hand the identity's directory to its Linux user
    if [[ $CHOWN -eq 1 ]]; then
        chown -R "$2:$2" "$OUT/$1"
    fi
}

SAN_LIST=$(IFS=,; echo "${SANS[*]}")
issue "$MOD_ID" serverAuth "$SAN_LIST"
own "$MOD_ID" "$MOD_USER"
echo "issued $MOD_ID   SAN $SAN_LIST"

for name in "${NAMES[@]}"; do
    issue "$name" clientAuth
    own "$name" "$name"
    echo "issued $name"
done

if [[ $CHOWN -eq 1 ]] && command -v restorecon >/dev/null; then
    restorecon -R "$OUT" || true
fi
if [[ $CHOWN -eq 0 ]]; then
    echo "note: nothing chowned (not root, or -U); every key is owned by $(id -un)"
fi
