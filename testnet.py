"""
testnet.py -- network arguments for the process-launching tests.

Every game gets a fresh free port on localhost, and every process gets the
same throwaway PKI (made once per test process by the real make_certs.sh),
so the tests run over exactly the TCP + mutual TLS path a real game uses.
"""

import atexit
import os
import shutil
import socket
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SEATS = ["p1", "p2", "p3", "p4", "p5", "p6"]   # every seat name the tests use

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


def port_released(port: int) -> bool:
    """
    True if nothing is listening on `port` any more. SO_REUSEADDR, as the
    moderator itself uses: the game's old connections may still sit in
    TIME_WAIT, which doesn't count -- only a listener left open does.
    """
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def net_args() -> tuple[list[str], list[str]]:
    """(moderator args, player args) for one game: a fresh port, the shared PKI."""
    net = ["--port", str(free_port()), "--tls", shared_pki()]
    return net, net
