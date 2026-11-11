"""
tls_server.py -- minimal TCP + TLS listener for test_tls.sh.

Opens the given seats on one port, and logs every inbox event (including
_rejected, with the reason). commslib itself sends WELCOME to whoever takes
a seat; seats are freed again on disconnect, so probes can come and go.

    python3 tls_server.py player1 player2 --tls /etc/werewolf/tls [--port 0]

Prints "PORT <n>" first, so a script can use --port 0 and read the port back.
"""

import argparse
import asyncio

from commslib import DEFAULT_TLS_DIR, Server


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("players", nargs="+")
    parser.add_argument("--tls", default=DEFAULT_TLS_DIR)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--duration", type=float, default=60.0,
                        help="seconds to keep listening")
    args = parser.parse_args()

    server = Server()
    await server.start(args.players, host=args.host, port=args.port, tls_dir=args.tls)
    print(f"PORT {server.port}", flush=True)
    print(f"[tls_server] listening for {args.players} on {args.host}:{server.port}", flush=True)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + args.duration
    while True:
        try:
            pid, msg = await server.recv_any(timeout=max(deadline - loop.time(), 0))
        except TimeoutError:
            break
        print(f"[tls_server] {pid}: {msg}", flush=True)

    await server.close()


if __name__ == "__main__":
    asyncio.run(main())
