"""
perm_probe.py -- try to take a seat and report what happened.

Run it as different Linux users to prove socket isolation:
    sudo -u player2 python3 /opt/werewolf/perm_probe.py player1

Exit codes (test_permissions.sh checks these):
    0  ACCEPTED  connected and the server welcomed us
    2  DENIED    the kernel refused connect(): PermissionError (filesystem)
    3  REJECTED  connected, but the server refused us (SO_PEERCRED mismatch)
    4  OTHER     anything else (socket missing, timeout, ...)
"""

import argparse
import asyncio
import os
import pwd
import sys

from commslib import Client, ConnectionClosed, player_socket_path

ACCEPTED, DENIED, REJECTED, OTHER = 0, 2, 3, 4


async def probe(seat: str, base_dir: str) -> int:
    me = pwd.getpwuid(os.getuid()).pw_name
    path = player_socket_path(seat, base_dir)
    client = Client()
    try:
        await client.connect(path, retry_seconds=0)
    except PermissionError as e:
        print(f"DENIED    {me} -> {path}: {e}")
        return DENIED
    except OSError as e:
        print(f"OTHER     {me} -> {path}: {e!r}")
        return OTHER

    try:
        reply = await client.recv(timeout=3)
    except (ConnectionClosed, TimeoutError) as e:
        print(f"OTHER     {me} -> {path}: no reply ({e!r})")
        return OTHER
    finally:
        await client.close()

    if reply.get("type") == "welcome":
        print(f"ACCEPTED  {me} -> {path}")
        return ACCEPTED
    print(f"REJECTED  {me} -> {path}: {reply.get('text')}")
    return REJECTED


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("seat", help="player id whose socket to try, e.g. player1")
    parser.add_argument("--dir", default="/run/werewolf")
    args = parser.parse_args()
    sys.exit(asyncio.run(probe(args.seat, args.dir)))


if __name__ == "__main__":
    main()
