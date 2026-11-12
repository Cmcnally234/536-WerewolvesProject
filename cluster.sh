#!/usr/bin/env bash
# cluster.sh -- a throwaway "LAN" of Fedora containers for the multi-host
# tests (test_deploy.sh, and the fault-injection tests). Sourced, not run.
#
# Each container is a separate host as far as the game can tell: its own
# network stack, hostname, /etc/passwd and filesystem, and an sshd. They
# share one podman network (DNS names = container names):
#
#   ww-mod   the moderator's host: setup_users.sh + make_certs.sh run here,
#            and deploy_remote.sh runs here as "admin" to reach the others
#   ww-pa    a player host
#   ww-pb    another player host
#
# Rootless podman: no sudo on the real machine. Inside each container we
# are root, so useradd, chown, sudo -u, tc and iptables all work for real
# (NET_ADMIN applies to the container's own network namespace only).
#
#   source cluster.sh; cluster_up; ...; cluster_down     (KEEP=1: leave it running)

CLUSTER_IMAGE=localhost/werewolf-host:4
CLUSTER_NET=wwnet
CLUSTER_HOSTS=(ww-mod ww-pa ww-pb)
CLUSTER_SRC=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")

# The files a real user would have in their checkout (git ls-files), so the
# containers get exactly the project and nothing from a local scratch area.
cluster_files() { (cd "$CLUSTER_SRC" && git ls-files -co --exclude-standard -- '*.py' '*.sh'); }

cluster_image() {
    podman image exists "$CLUSTER_IMAGE" && return 0
    echo "building $CLUSTER_IMAGE (once; needs network for dnf)..."
    # iproute-tc + iptables-nft: for the fault-injection tests.
    # pam_loginuid -> optional: it can't set the audit login uid in an
    #   unprivileged container, and as "required" it would fail every login.
    # usermod -p '*': no usable password, but not "locked" ('!'), which sshd
    #   refuses even for key logins.
    # PerSourcePenalties no: OpenSSH 10 otherwise starts refusing a source
    # address after a few failed connections -- right for an internet-facing
    # server, a nuisance on a test LAN where every connection comes from ww-mod.
    local log
    log=$(mktemp)
    podman build -q -t "$CLUSTER_IMAGE" -f - "$CLUSTER_SRC" <<'EOF' >"$log" 2>&1 || { cat "$log"; rm -f "$log"; return 1; }
FROM registry.fedoraproject.org/fedora:44
RUN dnf -y -q install python3 openssh-server openssh-clients sudo shadow-utils \
        procps-ng iproute iproute-tc iptables-nft openssl findutils hostname tar \
    && dnf clean all
RUN ssh-keygen -A \
    && printf 'PasswordAuthentication no\nPerSourcePenalties no\n' > /etc/ssh/sshd_config.d/10-test.conf \
    && sed -i 's/^session *required *pam_loginuid.so/session optional pam_loginuid.so/' /etc/pam.d/sshd \
    && useradd -m admin && usermod -p '*' admin \
    && echo 'admin ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/admin
CMD ["/usr/sbin/sshd", "-D", "-e"]
EOF
    rm -f "$log"
}

cx() { podman exec "$@"; }                 # cx HOST cmd...   run as root on HOST
# HOME set explicitly: podman exec -u doesn't, and ssh-keygen/ssh need it.
cx_admin() { local h=$1; shift; podman exec -u admin -e HOME=/home/admin -w /home/admin "$h" "$@"; }

cluster_up() {
    cluster_image || return 1
    cluster_down quiet
    podman network create "$CLUSTER_NET" >/dev/null
    for h in "${CLUSTER_HOSTS[@]}"; do
        podman run -d -q --name "$h" --hostname "$h" --network "$CLUSTER_NET" \
            --cap-add NET_ADMIN --cap-add NET_RAW "$CLUSTER_IMAGE" >/dev/null || return 1
    done
    # The source tree goes to ww-mod only (as on a real moderator machine);
    # the player hosts get the game solely through deploy_remote.sh.
    cx ww-mod mkdir -p /src
    local f
    while read -r f; do
        cx ww-mod mkdir -p "/src/$(dirname "$f")"
        podman cp "$CLUSTER_SRC/$f" "ww-mod:/src/$f"
    done < <(cluster_files)
    cx ww-mod chmod -R a+rX /src
    # admin@ww-mod can ssh to admin@<every host> with a key.
    cx_admin ww-mod bash -c 'ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/id_ed25519 &&
        printf "Host *\n  StrictHostKeyChecking accept-new\n  LogLevel ERROR\n" > ~/.ssh/config &&
        chmod 600 ~/.ssh/config'
    local pub
    pub=$(cx_admin ww-mod cat .ssh/id_ed25519.pub)
    for h in "${CLUSTER_HOSTS[@]}"; do
        cx_admin "$h" bash -c "mkdir -p -m 700 ~/.ssh && echo '$pub' >> ~/.ssh/authorized_keys &&
            chmod 600 ~/.ssh/authorized_keys"
    done
    for h in "${CLUSTER_HOSTS[@]}"; do
        for _ in $(seq 50); do
            cx_admin ww-mod ssh -o BatchMode=yes "admin@$h" true 2>/dev/null && continue 2
            sleep 0.2
        done
        echo "error: can't ssh from ww-mod to $h" >&2
        return 1
    done
}

# cluster_provision N -- a ready-to-play LAN with N seats: setup_users.sh and
# make_certs.sh on ww-mod, then deploy_remote.sh puts player3+player4 on
# ww-pa and player5..N on ww-pb. (test_deploy.sh checks each of these steps;
# this is the quiet version for tests that just need it done.)
cluster_provision() {
    local n=${1:-6} rest=() i
    cx ww-mod bash -c "cd /src && N_PLAYERS=$n ./setup_users.sh >/dev/null &&
        N_PLAYERS=$n ./make_certs.sh -s DNS:ww-mod -s DNS:localhost -s IP:127.0.0.1 >/dev/null" || return 1
    cx_admin ww-mod /src/deploy_remote.sh admin@ww-pa player3 player4 >/dev/null 2>&1 || return 1
    for i in $(seq 5 "$n"); do rest+=("player$i"); done
    cx_admin ww-mod /src/deploy_remote.sh admin@ww-pb "${rest[@]}" >/dev/null 2>&1
}

cluster_down() {
    [[ ${KEEP:-0} == 1 && ${1:-} != quiet ]] && { echo "(KEEP=1: cluster left running)"; return; }
    podman rm -f -t 0 "${CLUSTER_HOSTS[@]}" >/dev/null 2>&1
    podman network rm -f "$CLUSTER_NET" >/dev/null 2>&1
    true
}
