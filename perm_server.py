"""
perm_server.py -- minimal listener for test_permissions.sh.

Opens a seat for each player id (with SO_PEERCRED checking on), welcomes
whoever successfully takes a seat, and logs every inbox event. Seats are
freed again on disconnect, so probes can come and go.

    sudo -u modbot python3 /opt/werewolf/perm_server.py player1 player2
"""

import argparse
import asyncio

from commslib import Server


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("players", nargs="+")
    parser.add_argument("--dir", default="/run/werewolf")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="seconds to keep listening")
    args = parser.parse_args()

    server = Server()
    await server.start(args.players, base_dir=args.dir, check_peer_uid=True)
    print(f"[perm_server] listening for {args.players}", flush=True)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + args.duration
    while True:
        try:
            pid, msg = await server.recv_any(timeout=max(deadline - loop.time(), 0))
        except TimeoutError:
            break
        print(f"[perm_server] {pid}: {msg}", flush=True)
        if msg["type"] == "_connected":
            await server.send_to(pid, {"type": "welcome", "seat": pid})

    await server.close()


if __name__ == "__main__":
    asyncio.run(main())
