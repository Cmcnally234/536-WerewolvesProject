"""
test_commslib.py -- commslib's failure paths (Project 2, stage 0).

    python3 test_commslib.py

In-process, no sudo. Each test runs a real Server on a temp socket dir.
Misbehaving peers are played by raw asyncio connections (this test, like
perm_probe.py, is allowed to touch sockets directly: the point is to send
bytes a well-behaved Client never would).
"""

import asyncio
import json
import os
import struct
import tempfile
import time

import commslib
from commslib import Client, ConnectionClosed, Server, player_socket_path

SEATS = ["good", "bad"]


async def start_server(tmp: str, send_timeout: float = 1.0) -> Server:
    for seat in SEATS:
        os.makedirs(os.path.join(tmp, seat))
    server = Server(send_timeout=send_timeout)
    await server.start(SEATS, base_dir=tmp)
    return server


async def next_event(server: Server, pid: str, kind: str, timeout: float = 3.0) -> dict:
    """Skip inbox entries until `pid` produces a message of type `kind`."""
    deadline = time.monotonic() + timeout
    while True:
        got_pid, msg = await server.recv_any(timeout=deadline - time.monotonic())
        if got_pid == pid and msg["type"] == kind:
            return msg


async def bad_frame_case(name: str, raw: bytes, expect_in_reason: str) -> None:
    """A peer sends `raw`: it must be dropped, and a good player must not notice."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        server = await start_server(tmp)
        good = Client()
        await good.connect(player_socket_path("good", tmp))
        _, w = await asyncio.open_unix_connection(player_socket_path("bad", tmp))
        await next_event(server, "bad", "_connected")

        w.write(raw)
        await w.drain()
        ev = await next_event(server, "bad", "_disconnected")
        assert expect_in_reason in ev["reason"], ev

        # The server is still fine: 1:1 both ways with the good player.
        await good.send({"type": "PING"})
        assert (await next_event(server, "good", "PING"))["type"] == "PING"
        await server.send_to("good", {"type": "PONG"})
        assert (await good.recv(timeout=2))["type"] == "PONG"

        w.close()
        await good.close()
        await server.close()
    print(f"PASS {name}: bad peer dropped ({ev['reason']!r}); good player unaffected")


def frame(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body


async def test_bad_frames():
    await bad_frame_case("malformed JSON", frame(b"{not json"), "malformed")
    await bad_frame_case("invalid UTF-8", frame(b"\xff\xfe"), "malformed")
    await bad_frame_case("non-dict JSON", frame(json.dumps([1, 2]).encode()), "list, not an object")
    await bad_frame_case("oversized header", struct.pack(">I", commslib.MAX_MESSAGE_BYTES + 1),
                         "oversized")


async def test_client_sentinel_on_garbage():
    """A moderator that sends garbage must wake the player's recv(), not hang it."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        path = os.path.join(tmp, "sock")

        async def evil(reader, writer):
            writer.write(frame(b"[]"))
            await writer.drain()
            await asyncio.sleep(5)  # stay connected: EOF must not be what saves us

        srv = await asyncio.start_unix_server(evil, path=path)
        client = Client()
        await client.connect(path)
        try:
            await client.recv(timeout=2)
            raise AssertionError("recv() returned a non-dict frame")
        except ConnectionClosed as e:
            print(f"PASS client recv() raised ConnectionClosed on garbage: {e}")
        # And it keeps failing rather than hanging on a second call.
        try:
            await client.recv(timeout=2)
            raise AssertionError("second recv() returned")
        except ConnectionClosed:
            pass
        await client.close()
        srv.close()


async def test_client_sentinel_on_unexpected_error():
    """Even a non-ConnectionClosed error in the read loop must end recv()."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        server = await start_server(tmp)
        client = Client()

        async def broken_recv():
            raise RuntimeError("simulated bug")

        await client.connect(player_socket_path("good", tmp))
        client._reader_task.cancel()          # replace the reader with one that blows up
        await asyncio.gather(client._reader_task, return_exceptions=True)
        while not client._inbox.empty():      # drop the sentinel the cancel produced
            client._inbox.get_nowait()
        client._conn.recv = broken_recv
        client._reader_task = asyncio.create_task(client._read_loop())
        try:
            await client.recv(timeout=2)
            raise AssertionError("recv() returned")
        except ConnectionClosed as e:
            assert "simulated bug" in str(e), e
            print(f"PASS client recv() raised ConnectionClosed on a reader bug: {e}")
        await client.close()
        await server.close()


async def test_send_timeout():
    """A player that stops reading is dropped after send_timeout; others keep working."""
    send_timeout = 1.0
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        server = await start_server(tmp, send_timeout=send_timeout)
        good = Client()
        await good.connect(player_socket_path("good", tmp))
        # The stuck player connects and then never reads a byte.
        _, w = await asyncio.open_unix_connection(player_socket_path("bad", tmp))
        await server.wait_for_players(timeout=3)

        big = {"type": "FILLER", "pad": "x" * 900_000}
        start = time.monotonic()
        failed = []
        # Kernel socket buffers are a few hundred KB, so a handful of 900 KB
        # frames is guaranteed to fill them. The good player's reader task
        # drains its side, so only "bad" can block.
        for _ in range(10):
            failed = await server.broadcast(big)
            if failed:
                break
        elapsed = time.monotonic() - start
        assert failed == ["bad"], failed
        assert elapsed < send_timeout + 2, f"broadcast blocked {elapsed:.1f}s"
        ev = await next_event(server, "bad", "_disconnected")
        assert "bad" not in server.connected()

        # Everything after that is unaffected.
        await server.send_to("good", {"type": "AFTER"})
        while (msg := await good.recv(timeout=3))["type"] != "AFTER":
            pass
        w.close()
        await good.close()
        await server.close()
    print(f"PASS stuck reader dropped after {elapsed:.1f}s ({ev['reason']!r}); good player unaffected")


async def test_close_with_stuck_peer():
    """Server.close() must finish even if a peer never reads its pending data."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        server = await start_server(tmp, send_timeout=0.5)
        _, w = await asyncio.open_unix_connection(player_socket_path("bad", tmp))
        await next_event(server, "bad", "_connected")
        # Queue far more than the kernel buffers hold, without draining, so
        # most of it is still sitting in our transport buffer at close().
        conn = server._conns["bad"]
        for _ in range(3):
            conn._writer.write(frame(json.dumps({"pad": "x" * 900_000}).encode()))
        start = time.monotonic()
        await server.close()
        elapsed = time.monotonic() - start
        assert elapsed < commslib.CLOSE_TIMEOUT + 3, f"close() took {elapsed:.1f}s"
        w.close()
    print(f"PASS close() with a stuck peer finished in {elapsed:.1f}s")


async def main():
    await test_bad_frames()
    await test_client_sentinel_on_garbage()
    await test_client_sentinel_on_unexpected_error()
    await test_send_timeout()
    await test_close_with_stuck_peer()
    print("all commslib tests passed")


if __name__ == "__main__":
    asyncio.run(main())
