"""
ping_client.py -- stand-in "player" for testing commslib.py alone.

Usage:
  local dev:   python3 ping_client.py p1 [--delay 3] [--dir /tmp/werewolf-test]
  real users:  sudo -u player1 python3 /opt/werewolf/ping_client.py player1 \
                   --dir /run/werewolf

--delay simulates a slow player so you can see the server's timeout.
"""

import argparse
import asyncio

from commslib import Client, ConnectionClosed, player_socket_path


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--dir", default="/tmp/werewolf-test")
    args = parser.parse_args()
    name, delay = args.name, args.delay
    socket_path = player_socket_path(name, args.dir)

    client = Client()
    await client.connect(socket_path)
    print(f"[{name}] connected to {socket_path}")

    try:
        while True:
            msg = await client.recv()
            if msg["type"] == "big":
                print(f"[{name}] got big message, {len(msg['data'])} bytes intact")
                continue
            print(f"[{name}] got {msg}")
            if msg["type"] == "ping":
                await asyncio.sleep(delay)
                await client.send({"type": "pong", "seq": msg["seq"], "from": name})
            elif msg["type"] == "bye":
                break
    except ConnectionClosed:
        print(f"[{name}] server hung up")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
