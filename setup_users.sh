#!/usr/bin/env bash
# setup_users.sh -- create game users, install code, build the socket tree.
#
#   sudo ./setup_users.sh            # 5 players (default)
#   sudo N_PLAYERS=4 ./setup_users.sh
#
# Idempotent: safe to re-run. Existing users are kept, and modes/owners are
# re-applied every time, which also repairs anything that drifted.
#
# /run is a tmpfs (wiped at every boot), so re-run this after rebooting.
#
# Resulting layout:
#   /opt/werewolf/            root:werewolf     0750   code (*.py, 0640)
#   /run/werewolf/            modbot:werewolf   0750
#   /run/werewolf/playerN/    modbot:playerN    2750   (setgid)
#   /run/werewolf/playerN/sock                         created by moderator,
#                             modbot:playerN    0660   group inherited via setgid

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "error: run with sudo" >&2
    exit 1
fi

N_PLAYERS=${N_PLAYERS:-5}
GAME_GROUP=werewolf
MOD_USER=modbot
APP_DIR=/opt/werewolf
RUN_DIR=/run/werewolf
NOLOGIN=$(command -v nologin || echo /sbin/nologin)
# Where this script lives = where the source .py files are.
SRC_DIR=$(dirname "$(readlink -f "$0")")

PLAYERS=()
for i in $(seq 1 "$N_PLAYERS"); do
    PLAYERS+=("player$i")
done

# --- 1. groups and users ----------------------------------------------------

# Shared group for everyone in the game: grants access to /opt/werewolf
# and lets members *traverse* /run/werewolf. Outsiders get neither.
if ! getent group "$GAME_GROUP" >/dev/null; then
    groupadd --system "$GAME_GROUP"
    echo "created group $GAME_GROUP"
fi

for user in "$MOD_USER" "${PLAYERS[@]}"; do
    if id -u "$user" >/dev/null 2>&1; then
        echo "user $user exists, skipping"
    else
        # --system:     low uid, no aging, hidden from login screens
        # --user-group: also create a private group with the same name;
        #               group playerN is what the socket ends up owned by
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

# --- 3. socket directory tree ----------------------------------------------

# 0750: modbot full access; game members may enter (x) and list (r);
# everyone else can't even look inside.
mkdir -p "$RUN_DIR"
chown "$MOD_USER:$GAME_GROUP" "$RUN_DIR"
chmod 0750 "$RUN_DIR"

for p in "${PLAYERS[@]}"; do
    d="$RUN_DIR/$p"
    mkdir -p "$d"
    chown "$MOD_USER:$p" "$d"
    # 2 = setgid: files created inside get group $p instead of the creator's
    # primary group. 7 = modbot rwx, 5 = playerN r-x, 0 = others nothing.
    chmod 2750 "$d"
done

# Fedora runs SELinux: give the new files the default labels for their paths
# (normally they already have them; this just makes sure).
if command -v restorecon >/dev/null; then
    restorecon -R "$APP_DIR" "$RUN_DIR" || true
fi

echo
echo "socket tree:"
ls -la "$RUN_DIR"
