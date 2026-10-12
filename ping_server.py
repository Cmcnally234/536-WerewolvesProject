"""
ping_server.py -- stand-in "moderator" for testing commslib.py alone.

Exercises: 1:1 (send_to), multicast, broadcast, recv_any with a timeout,
and a large message that is guaranteed to be split across socket reads.

Usage: python3 ping_server.py [socket_dir] [player ids...]
"""

import asyncio
import os
import sys

from commslib import Server

SOCKET_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/werewolf-test"
PLAYERS = sys.argv[2:] or ["p1", "p2", "p3"]
PONG_TIMEOUT = 2.0  # seconds to wait for pongs before giving up on laggards


async def main():
    os.makedirs(SOCKET_DIR, exist_ok=True)
    paths = {pid: os.path.join(SOCKET_DIR, f"{pid}.sock") for pid in PLAYERS}

    server = Server()
    await server.start(paths)
    print(f"[server] listening: {paths}")

    await server.wait_for_players(timeout=15)
    print(f"[server] all connected: {server.connected()}")

    # --- 1:1: ping each player privately, then collect pongs from ANY ---
    for pid in PLAYERS:
        await server.send_to(pid, {"type": "ping", "seq": 1})

    waiting = set(PLAYERS)
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
    group = PLAYERS[:2]
    await server.multicast(group, {"type": "note", "text": f"secret for {group}"})

    # --- big 1:1 message: 300 KB forces multiple reads on the other side ---
    await server.send_to(PLAYERS[0], {"type": "big", "data": "x" * 300_000})

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
