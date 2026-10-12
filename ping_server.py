"""
ping_server.py -- stand-in "moderator" for testing commslib.py alone.

Exercises: 1:1 (send_to), multicast, broadcast, recv_any with a timeout,
and a large message that is guaranteed to be split across socket reads.

Usage:
  local dev:   python3 ping_server.py [--dir /tmp/werewolf-test] [p1 p2 p3]
  real users:  sudo -u modbot python3 /opt/werewolf/ping_server.py \
                   --dir /run/werewolf --peercred player1 player2 player3
"""

import argparse
import asyncio
import os

from commslib import Server

PONG_TIMEOUT = 2.0  # seconds to wait for pongs before giving up on laggards


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="/tmp/werewolf-test")
    parser.add_argument("--peercred", action="store_true",
                        help="enforce SO_PEERCRED (player ids must be Linux users)")
    parser.add_argument("players", nargs="*", default=["p1", "p2", "p3"])
    args = parser.parse_args()
    players = args.players

    # For local dev without setup_users.sh, make the per-seat directories.
    # Under /run/werewolf they already exist and this changes nothing.
    for pid in players:
        os.makedirs(os.path.join(args.dir, pid), exist_ok=True)

    server = Server()
    await server.start(players, base_dir=args.dir, check_peer_uid=args.peercred)
    print(f"[server] listening on {args.dir}/<id>/sock for {players}")

    await server.wait_for_players(timeout=15)
    print(f"[server] all connected: {server.connected()}")

    # --- 1:1: ping each player privately, then collect pongs from ANY ---
    for pid in players:
        await server.send_to(pid, {"type": "ping", "seq": 1})

    waiting = set(players)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PONG_TIMEOUT
    while waiting:
        remaining = deadline - loop.time()
        try:
            # The key line: wait for whoever answers first, but never past
            # the deadline. A slow player cannot stall the whole server.
            pid, msg = await server.recv_any(timeout=max(remaining, 0))
        except TimeoutError:
            print(f"[server] timed out waiting for: {sorted(waiting)}")
            break
        print(f"[server] <- {pid}: {msg}")
        if msg.get("type") == "pong":
            waiting.discard(pid)

    # --- multicast: only the first two players should get this ---
    group = players[:2]
    await server.multicast(group, {"type": "note", "text": f"secret for {group}"})

    # --- big 1:1 message: 300 KB forces multiple reads on the other side ---
    await server.send_to(players[0], {"type": "big", "data": "x" * 300_000})

    # --- broadcast: everyone ---
    await server.broadcast({"type": "note", "text": "hello everyone"})
    await server.broadcast({"type": "bye"})

    # Drain any late pongs / disconnect notices, briefly.
    try:
        while True:
            pid, msg = await server.recv_any(timeout=1.0)
            print(f"[server] <- {pid}: {msg}")
    except TimeoutError:
        pass

    await server.close()
    print("[server] done")


if __name__ == "__main__":
    asyncio.run(main())
