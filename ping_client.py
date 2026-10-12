"""
ping_client.py -- stand-in "player" for testing commslib.py alone.

Usage: python3 ping_client.py <socket_path> <name> [reply_delay_seconds]

reply_delay lets us simulate a slow player to see the server's timeout.
"""

import asyncio
import sys

from commslib import Client, ConnectionClosed


async def main():
    socket_path = sys.argv[1]
    name = sys.argv[2]
    delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

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
