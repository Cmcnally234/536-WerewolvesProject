#!/usr/bin/env bash
# teardown_users.sh -- undo setup_users.sh.
#
#   sudo ./teardown_users.sh
#   sudo N_PLAYERS=4 ./teardown_users.sh
#
# Kills any processes still running as the game users, then deletes the
# users, their groups, /opt/werewolf and /etc/werewolf (the TLS
# certificates from make_certs.sh). Safe to re-run.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "error: run with sudo" >&2
    exit 1
fi

N_PLAYERS=${N_PLAYERS:-5}
GAME_GROUP=werewolf
MOD_USER=modbot

USERS=("$MOD_USER")
for i in $(seq 1 "$N_PLAYERS"); do
    USERS+=("player$i")
done

for user in "${USERS[@]}"; do
    if id -u "$user" >/dev/null 2>&1; then
        # userdel refuses to remove a user that still has running processes.
        pkill -u "$user" 2>/dev/null || true
        sleep 0.2
        userdel "$user"    # also removes the user-private group of the same name
        echo "removed user $user"
    fi
    # In case the private group survived (e.g. made by hand), drop it too.
    if getent group "$user" >/dev/null; then
        groupdel "$user"
    fi
done

if getent group "$GAME_GROUP" >/dev/null; then
    groupdel "$GAME_GROUP"
    echo "removed group $GAME_GROUP"
fi

rm -rf /opt/werewolf /etc/werewolf
echo "removed /opt/werewolf and /etc/werewolf"
