"""
testnet.py -- which transport the process-launching tests use.

    python3 test_game.py                   # UNIX sockets (Project 1 setup)
    WW_TRANSPORT=tcp python3 test_game.py  # the same games over localhost TCP + TLS

Each game gets its own arguments from net_args(): a temp socket directory
for UNIX, or a fresh free port plus a throwaway PKI for TCP.
"""

import atexit
import os
import shutil
import socket
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SEATS = ["p1", "p2", "p3", "p4", "p5", "p6"]   # every seat name the tests use

TRANSPORT = os.environ.get("WW_TRANSPORT", "unix")
if TRANSPORT not in ("unix", "tcp"):
    raise SystemExit(f"WW_TRANSPORT must be 'unix' or 'tcp', not {TRANSPORT!r}")

_pki: str | None = None


def shared_pki(names=SEATS) -> str:
    """
    A CA plus certificates for the moderator and `names`, made once per test
    process by the real make_certs.sh and deleted at exit. -U: nothing is
    chowned, so this works without root and with names that aren't users.
    """
    global _pki
    if _pki is None:
        _pki = tempfile.mkdtemp(prefix="ww-pki-")
        atexit.register(shutil.rmtree, _pki, ignore_errors=True)
        subprocess.run([os.path.join(HERE, "make_certs.sh"), "-U", "-o", _pki, *names],
                       check=True, stdout=subprocess.DEVNULL)
    return _pki


def free_port() -> int:
    """
    Ask the kernel for an unused port, then release it for the moderator.
    (Another process could grab it in between; on a test box that's rare
    enough not to matter.)
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def net_args(sock_dir: str) -> tuple[list[str], list[str]]:
    """(moderator args, player args) for one game."""
    if TRANSPORT == "tcp":
        tcp = ["--port", str(free_port()), "--tls", shared_pki()]
        return tcp, tcp
    return ["--dir", sock_dir, "--local"], ["--dir", sock_dir]
