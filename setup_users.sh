#!/usr/bin/env bash
# setup_users.sh -- create the game's Linux users and install the code.
#
#   sudo ./setup_users.sh            # 5 players (default)
#   sudo N_PLAYERS=4 ./setup_users.sh
#
# Idempotent: safe to re-run. Existing users are kept, and modes/owners are
# re-applied every time, which also repairs anything that drifted.
#
# Resulting layout:
#   users modbot, player1..N  (system accounts, no login), all in group werewolf
#   /opt/werewolf/            root:werewolf     0750   code (*.py, 0640)
#
# Next: sudo ./make_certs.sh -- each player's TLS key, owned by its own user,
# is what lets that user (and only that user) take its seat.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "error: run with sudo" >&2
    exit 1
fi

N_PLAYERS=${N_PLAYERS:-5}
GAME_GROUP=werewolf
MOD_USER=modbot
APP_DIR=/opt/werewolf
NOLOGIN=$(command -v nologin || echo /sbin/nologin)
# Where this script lives = where the source .py files are.
SRC_DIR=$(dirname "$(readlink -f "$0")")

PLAYERS=()
for i in $(seq 1 "$N_PLAYERS"); do
    PLAYERS+=("player$i")
done

# --- 1. groups and users ----------------------------------------------------

# Shared group for everyone in the game: grants access to /opt/werewolf.
# Outsiders can't even read the code.
if ! getent group "$GAME_GROUP" >/dev/null; then
    groupadd --system "$GAME_GROUP"
    echo "created group $GAME_GROUP"
fi

for user in "$MOD_USER" "${PLAYERS[@]}"; do
    if id -u "$user" >/dev/null 2>&1; then
        echo "user $user exists, skipping"
    else
        # --system:     low uid, no aging, hidden from login screens
        # --user-group: also create a private group with the same name
        #               (make_certs.sh gives each key directory to user:user)
        # --no-create-home + nologin: these accounts only exist to run a
        #               process via `sudo -u`, never to log in
        useradd --system --user-group --no-create-home \
                --home-dir "$APP_DIR" --shell "$NOLOGIN" "$user"
        echo "created user $user"
    fi
    usermod -aG "$GAME_GROUP" "$user"    # -a = append, so re-running is harmless
done

# --- 2. install the code ----------------------------------------------------

# The code is copied out of the home directory because home is mode 700: the game
# users couldn't even read the .py files there. Wipe and recopy so files
# deleted from the source tree don't linger here.
rm -rf "$APP_DIR"
install -d -o root -g "$GAME_GROUP" -m 0750 "$APP_DIR"
install -o root -g "$GAME_GROUP" -m 0640 "$SRC_DIR"/*.py "$APP_DIR"/
echo "installed $(ls "$APP_DIR" | wc -l) files to $APP_DIR"

# Fedora runs SELinux: give the new files the default labels for their paths
# (normally they already have them; this just makes sure).
if command -v restorecon >/dev/null; then
    restorecon -R "$APP_DIR" || true
fi

echo
echo "next, the certificates: sudo ./make_certs.sh   (sudo N_PLAYERS=$N_PLAYERS ./make_certs.sh)"
