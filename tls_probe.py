"""
tls_probe.py -- try to take a seat over TCP + TLS; report what happened.

Used by test_tls.sh. Exit status:
    0  ACCEPTED   the moderator sent WELCOME
    3  REFUSED    the moderator turned us away (TLS alert, error reply, hang-up)
    5  UNTRUSTED  WE refused the moderator: its certificate failed verification
    4  OTHER      anything else (e.g. couldn't read our own key)

    python3 tls_probe.py player1 --port 5555 --tls /etc/werewolf/tls
    python3 tls_probe.py player1 --port 5555 --tls DIR --no-cert   # present no certificate
    python3 tls_probe.py player1 --port 5555 --tls DIR --plain     # no TLS at all

The normal mode goes through commslib.Client, so it tests the real player
code path. --no-cert and --plain have to build the connection by hand,
because commslib (correctly) can't be made to do either.
"""

import argparse
import asyncio
import json
import ssl
import struct
import sys

from commslib import Client, ConnectionClosed, SeatRefused, tls_paths

ACCEPTED, REFUSED, OTHER, UNTRUSTED = 0, 3, 4, 5


async def raw_attempt(args) -> dict:
    """HELLO over a hand-built connection; returns the moderator's first reply."""
    tls = None
    if not args.plain:
        tls = ssl.create_default_context(cafile=tls_paths(args.seat, args.tls)[0])
        tls.minimum_version = ssl.TLSVersion.TLSv1_3
    reader, writer = await asyncio.open_connection(
        args.host, args.port, ssl=tls, server_hostname=args.host if tls else None)
    try:
        body = json.dumps({"type": "HELLO", "player": args.seat}).encode()
        writer.write(struct.pack(">I", len(body)) + body)
        await writer.drain()
        (n,) = struct.unpack(">I", await asyncio.wait_for(reader.readexactly(4), 5))
        return json.loads(await reader.readexactly(n))
    finally:
        writer.close()


async def probe(args) -> int:
    who = f"{args.seat} ({'no cert' if args.no_cert else 'plain TCP' if args.plain else 'cert'})"
    try:
        if args.no_cert or args.plain:
            reply = await raw_attempt(args)
            if reply.get("type") == "WELCOME":
                print(f"ACCEPTED  {who}")
                return ACCEPTED
            print(f"REFUSED   {who}: {reply}")
            return REFUSED
        client = Client()
        await client.connect(port=args.port, host=args.host, player_id=args.seat,
                             tls_dir=args.tls, retry_seconds=0)
        print(f"ACCEPTED  {who}")
        if args.hold:
            await asyncio.sleep(args.hold)
        await client.close()
        return ACCEPTED
    except ssl.SSLCertVerificationError as e:
        print(f"UNTRUSTED {who}: we rejected the moderator: {e.verify_message}")
        return UNTRUSTED
    except SeatRefused as e:
        print(f"REFUSED   {who}: {e}")
        return REFUSED
    except (ConnectionClosed, ssl.SSLError, asyncio.IncompleteReadError, ConnectionError) as e:
        # Raw modes: the moderator's alert / hang-up arrives as one of these.
        print(f"REFUSED   {who}: {getattr(e, 'reason', None) or e!r}")
        return REFUSED
    except OSError as e:
        print(f"OTHER     {who}: {e}")
        return OTHER


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("seat")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--tls", required=True)
    parser.add_argument("--no-cert", action="store_true")
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--hold", type=float, default=0,
                        help="once seated, stay connected this many seconds")
    args = parser.parse_args()
    sys.exit(asyncio.run(probe(args)))


if __name__ == "__main__":
    main()
