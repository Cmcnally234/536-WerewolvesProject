"""
ping_client.py -- stand-in "player" for testing commslib.py alone.

Usage (certificates as for ping_server.py):
  local dev:   python3 ping_client.py p1 --tls /tmp/pki [--delay 3]
  real users:  sudo -u player1 python3 /opt/werewolf/ping_client.py player1

--delay simulates a slow player so you can see the server's timeout.
"""

import argparse
import asyncio

from commslib import DEFAULT_PORT, DEFAULT_TLS_DIR, Client, ConnectionClosed


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--tls", default=DEFAULT_TLS_DIR)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    name, delay = args.name, args.delay

    client = Client()
    await client.connect(player_id=name, host=args.host, port=args.port, tls_dir=args.tls)
    print(f"[{name}] connected to {args.host}:{args.port} (TLS)")

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
