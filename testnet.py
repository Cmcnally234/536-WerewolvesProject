"""
testnet.py -- which transport the process-launching tests use.

    python3 test_game.py                   # UNIX sockets (Project 1 setup)
    WW_TRANSPORT=tcp python3 test_game.py  # the same games over localhost TCP

Each game gets its own arguments from net_args(): a temp socket directory
for UNIX, or a fresh free port for TCP.
"""

import os
import socket

TRANSPORT = os.environ.get("WW_TRANSPORT", "unix")
if TRANSPORT not in ("unix", "tcp"):
    raise SystemExit(f"WW_TRANSPORT must be 'unix' or 'tcp', not {TRANSPORT!r}")


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
        port = ["--port", str(free_port())]
        return port, port
    return ["--dir", sock_dir, "--local"], ["--dir", sock_dir]
