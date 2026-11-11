"""
test_commslib.py -- commslib's failure paths, the TCP/TLS handshake, heartbeats.

    python3 test_commslib.py

In-process, no sudo. Each test runs a real Server, over UNIX sockets in a
temp dir or over TCP + TLS on a free localhost port (certificates from a
throwaway PKI made by make_certs.sh); the failure-path tests run on both. Misbehaving peers are played by raw asyncio connections (this
test, like perm_probe.py, is allowed to touch sockets directly: the point
is to send bytes a well-behaved Client never would).
"""

import asyncio
import json
import os
import socket
import ssl
import struct
import tempfile
import time

import commslib
from commslib import (Client, ConnectionClosed, SeatRefused, Server,
                      player_socket_path, tls_paths)
from testnet import shared_pki

SEATS = ["good", "bad"]
# "mallory" has a certificate from the real CA but never gets a seat.
PKI = shared_pki(["good", "bad", "p1", "p2", "p3", "mallory"])


def frame(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> dict:
    (n,) = struct.unpack(">I", await reader.readexactly(4))
    return json.loads(await reader.readexactly(n))


async def tls_open(port: int, cert_of: str):
    """A hand-built TLS connection presenting `cert_of`'s certificate, no HELLO yet."""
    ca, cert, key = tls_paths(cert_of, PKI)
    ctx = ssl.create_default_context(cafile=ca)
    ctx.load_cert_chain(cert, key)
    return await asyncio.open_connection("127.0.0.1", port, ssl=ctx,
                                         server_hostname="127.0.0.1")


class Net:
    """One transport for one test: UNIX in a temp dir, or TCP on a free port."""

    def __init__(self, kind: str, tmp: str):
        self.kind, self.tmp = kind, tmp

    async def server(self, send_timeout: float = 1.0, seats=SEATS, **heartbeat) -> Server:
        server = Server(send_timeout=send_timeout, **heartbeat)
        if self.kind == "tcp":
            await server.start(seats, port=0, tls_dir=PKI)   # 0 = let the kernel pick
            self.port = server.port
        else:
            for seat in seats:
                os.makedirs(os.path.join(self.tmp, seat), exist_ok=True)
            await server.start(seats, base_dir=self.tmp)
        return server

    async def client(self, seat: str, retry_seconds: float = 2.0, port=None,
                     **heartbeat) -> Client:
        client = Client(**heartbeat)
        if self.kind == "tcp":
            await client.connect(port=port or self.port, player_id=seat, tls_dir=PKI,
                                 retry_seconds=retry_seconds)
        else:
            await client.connect(player_socket_path(seat, self.tmp), retry_seconds=retry_seconds)
        return client

    async def raw(self, seat: str):
        """A raw connection already sitting in `seat` (TLS + HELLO done for TCP)."""
        if self.kind == "unix":
            return await asyncio.open_unix_connection(player_socket_path(seat, self.tmp))
        r, w = await tls_open(self.port, seat)
        w.write(frame(json.dumps({"type": "HELLO", "player": seat}).encode()))
        assert (await read_frame(r))["type"] == "WELCOME"
        return r, w


async def next_event(server: Server, pid, kind: str, timeout: float = 3.0) -> dict:
    """Skip inbox entries until `pid` produces a message of type `kind`."""
    deadline = time.monotonic() + timeout
    while True:
        got_pid, msg = await server.recv_any(timeout=deadline - time.monotonic())
        if got_pid == pid and msg["type"] == kind:
            return msg


# ---------------------------------------------------------------------------
# Stage 0: failure paths (both transports)
# ---------------------------------------------------------------------------

async def bad_frame_case(kind: str, name: str, raw: bytes, expect_in_reason: str) -> None:
    """A peer sends `raw`: it must be dropped, and a good player must not notice."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        net = Net(kind, tmp)
        server = await net.server()
        good = await net.client("good")
        _, w = await net.raw("bad")
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
    print(f"PASS [{kind}] {name}: bad peer dropped ({ev['reason']!r}); good player unaffected")


async def test_bad_frames(kind: str):
    await bad_frame_case(kind, "malformed JSON", frame(b"{not json"), "malformed")
    await bad_frame_case(kind, "invalid UTF-8", frame(b"\xff\xfe"), "malformed")
    await bad_frame_case(kind, "non-dict JSON", frame(json.dumps([1, 2]).encode()),
                         "list, not an object")
    await bad_frame_case(kind, "oversized header",
                         struct.pack(">I", commslib.MAX_MESSAGE_BYTES + 1), "oversized")
    for interval in (0, -1, True, "2", commslib.MAX_HEARTBEAT_INTERVAL + 1):
        await bad_frame_case(kind, f"heartbeat interval {interval!r}",
                             frame(json.dumps({"type": "HEARTBEAT", "interval": interval}).encode()),
                             "bad heartbeat interval")


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
        net = Net("unix", tmp)
        server = await net.server()
        client = await net.client("good")

        async def broken_recv():
            raise RuntimeError("simulated bug")

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


async def test_send_timeout(kind: str):
    """A player that stops reading is dropped after send_timeout; others keep working."""
    send_timeout = 1.0
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        net = Net(kind, tmp)
        server = await net.server(send_timeout=send_timeout)
        good = await net.client("good")
        # The stuck player connects and then never reads a byte.
        _, w = await net.raw("bad")
        await server.wait_for_players(timeout=3)

        big = {"type": "FILLER", "pad": "x" * 900_000}
        start = time.monotonic()
        failed = []
        # Kernel socket buffers are a few hundred KB (a few MB for loopback
        # TCP), so a dozen 900 KB frames is guaranteed to fill them. The good
        # player's reader task drains its side, so only "bad" can block.
        for _ in range(20):
            failed = await server.broadcast(big)
            if failed:
                break
        elapsed = time.monotonic() - start
        assert failed == ["bad"], failed
        ev = await next_event(server, "bad", "_disconnected")
        assert "bad" not in server.connected()

        # Everything after that is unaffected.
        await server.send_to("good", {"type": "AFTER"})
        while (msg := await good.recv(timeout=3))["type"] != "AFTER":
            pass
        w.close()
        await good.close()
        await server.close()
    print(f"PASS [{kind}] stuck reader dropped ({ev['reason']!r}) after {elapsed:.1f}s "
          "of broadcasting; good player unaffected")


async def test_close_with_stuck_peer(kind: str):
    """Server.close() must finish even if a peer never reads its pending data."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        net = Net(kind, tmp)
        server = await net.server(send_timeout=0.5)
        _, w = await net.raw("bad")
        await next_event(server, "bad", "_connected")
        # Queue far more than the kernel buffers hold, without draining, so
        # most of it is still sitting in our transport buffer at close().
        conn = server._conns["bad"]
        for _ in range(10):
            conn._writer.write(frame(json.dumps({"pad": "x" * 900_000}).encode()))
        start = time.monotonic()
        await server.close()
        elapsed = time.monotonic() - start
        assert elapsed < commslib.CLOSE_TIMEOUT + 3, f"close() took {elapsed:.1f}s"
        w.close()
    print(f"PASS [{kind}] close() with a stuck peer finished in {elapsed:.1f}s")


# ---------------------------------------------------------------------------
# Stage 1: TCP
# ---------------------------------------------------------------------------

async def test_tcp_star():
    """Over one TLS port: seats by certificate, then 1:1, multicast and broadcast."""
    seats = ["p1", "p2", "p3"]
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        net = Net("tcp", tmp)
        server = await net.server(seats=seats)
        clients = {p: await net.client(p) for p in seats}
        await server.wait_for_players(timeout=3)
        assert sorted(server.connected()) == seats

        for p, c in clients.items():                       # player -> moderator
            await c.send({"type": "HI", "n": p})
        for p in seats:
            assert (await next_event(server, p, "HI"))["n"] == p   # tagged by seat

        await server.send_to("p2", {"type": "ONE"})               # 1:1
        await server.multicast(["p1", "p3"], {"type": "SOME"})    # multicast
        await server.broadcast({"type": "ALL"})                   # broadcast
        got = {}
        for p, c in clients.items():
            got[p] = [(await c.recv(timeout=2))["type"] for _ in range(2)]
        assert got == {"p1": ["SOME", "ALL"], "p2": ["ONE", "ALL"], "p3": ["SOME", "ALL"]}, got

        cipher = server._conns["p1"]._writer.get_extra_info("cipher")
        for c in clients.values():
            await c.close()
        await server.close()
    print(f"PASS [tcp] 3 players on one port ({cipher[1]}, {cipher[0]}), "
          f"1:1/multicast/broadcast {got}")


async def test_tcp_socket_options():
    """TCP_NODELAY and keepalive are on, at both ends."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        net = Net("tcp", tmp)
        server = await net.server()
        client = await net.client("good")
        await next_event(server, "good", "_connected")
        ends = {"moderator": server._conns["good"], "player": client._conn}
        for end, conn in ends.items():
            s = conn._writer.get_extra_info("socket")
            opts = {
                "TCP_NODELAY": s.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY),
                "SO_KEEPALIVE": s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE),
                "TCP_KEEPIDLE": s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE),
                "TCP_KEEPINTVL": s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL),
                "TCP_KEEPCNT": s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT),
                "TCP_USER_TIMEOUT": s.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT),
            }
            want = {"TCP_NODELAY": 1, "SO_KEEPALIVE": 1,
                    "TCP_KEEPIDLE": commslib.KEEPALIVE_IDLE,
                    "TCP_KEEPINTVL": commslib.KEEPALIVE_INTERVAL,
                    "TCP_KEEPCNT": commslib.KEEPALIVE_COUNT,
                    "TCP_USER_TIMEOUT": commslib.TCP_USER_TIMEOUT_MS}
            # getsockopt may report an on/off flag as any non-zero value.
            for flag in ("TCP_NODELAY", "SO_KEEPALIVE"):
                opts[flag] = int(bool(opts[flag]))
            assert opts == want, (end, opts)
        await client.close()
        await server.close()
    print(f"PASS [tcp] both ends: {opts}")


async def test_tcp_refusals():
    """Certificate for no seat, HELLO for someone else's seat, taken seat, wrong
    first frame, silent peers -- all turned away without taking a seat."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        net = Net("tcp", tmp)
        server = await net.server()

        try:
            await net.client("mallory")
            raise AssertionError("certificate for no seat was accepted")
        except SeatRefused as e:
            refusal = str(e)
        assert "which is not a seat" in refusal, refusal
        ev = await next_event(server, None, "_rejected")
        print(f"PASS [tcp] cert for no seat refused: client got {refusal!r}, server got {ev}")

        # good's (valid) certificate, but a HELLO claiming bad's seat.
        r, w = await tls_open(net.port, cert_of="good")
        w.write(frame(json.dumps({"type": "HELLO", "player": "bad"}).encode()))
        reply = await read_frame(r)
        assert reply == {"type": "error",
                         "text": "certificate is for 'good', not 'bad'"}, reply
        assert await r.read() == b""
        w.close()
        assert server.connected() == []
        print(f"PASS [tcp] HELLO for another player's seat refused: {reply}")

        first = await net.client("good")
        try:
            await net.client("good")
            raise AssertionError("second connection took a taken seat")
        except SeatRefused as e:
            refusal = str(e)
        assert "seat already taken" in refusal, refusal
        await next_event(server, "good", "_rejected")
        # The original occupant is undisturbed.
        await server.send_to("good", {"type": "STILL_HERE"})
        assert (await first.recv(timeout=2))["type"] == "STILL_HERE"
        print(f"PASS [tcp] duplicate seat refused ({refusal!r}); first occupant unaffected")

        r, w = await tls_open(net.port, cert_of="bad")
        w.write(frame(json.dumps({"type": "VOTE", "target": "good"}).encode()))
        reply = await read_frame(r)
        assert reply == {"type": "error", "text": "expected HELLO first"}, reply
        assert await r.read() == b""            # and then it hung up
        w.close()
        print(f"PASS [tcp] non-HELLO first frame refused: {reply}")

        # Peers that go quiet are dropped after HELLO_TIMEOUT: one that never
        # starts TLS, and one that finishes TLS but never sends HELLO.
        saved, commslib.HELLO_TIMEOUT = commslib.HELLO_TIMEOUT, 0.5
        try:
            for what, opener in [("before TLS", lambda: asyncio.open_connection("127.0.0.1", net.port)),
                                 ("after TLS", lambda: tls_open(net.port, cert_of="bad"))]:
                r, w = await opener()
                start = time.monotonic()
                try:
                    data = await asyncio.wait_for(r.read(), 3)
                except (ssl.SSLError, ConnectionError):
                    data = b""
                elapsed = time.monotonic() - start
                assert data == b"", data
                w.close()
                assert "bad" not in server.connected()
                print(f"PASS [tcp] peer silent {what} dropped after {elapsed:.1f}s, no seat taken")
        finally:
            commslib.HELLO_TIMEOUT = saved
        ev = await next_event(server, None, "_rejected")
        assert "TLS handshake failed" in ev["reason"], ev

        await first.close()
        await server.close()


async def test_tcp_connect_retry_and_close():
    """A player started before the moderator keeps retrying; close() isn't held up
    by a connection stuck in the handshake; peercred is refused over TCP."""
    port = socket.socket()
    port.bind(("127.0.0.1", 0))
    free = port.getsockname()[1]
    port.close()

    async def late_server():
        await asyncio.sleep(0.6)
        server = Server()
        await server.start(SEATS, port=free, tls_dir=PKI)
        return server

    server_task = asyncio.create_task(late_server())
    client = Client()
    start = time.monotonic()
    await client.connect(port=free, player_id="good", tls_dir=PKI, retry_seconds=5)
    waited = time.monotonic() - start
    server = await server_task
    assert waited >= 0.5, waited
    print(f"PASS [tcp] player started first connected after retrying {waited:.1f}s")

    r, w = await asyncio.open_connection("127.0.0.1", free)   # never starts TLS
    await asyncio.sleep(0.1)
    start = time.monotonic()
    await client.close()
    await server.close()
    elapsed = time.monotonic() - start
    assert elapsed < 1.5, f"close() took {elapsed:.1f}s with a handshake pending"
    assert await asyncio.wait_for(r.read(), 2) == b""
    w.close()
    print(f"PASS [tcp] close() with a peer mid-handshake took {elapsed:.2f}s")

    try:
        await Server().start(SEATS, check_peer_uid=True, port=0, tls_dir=PKI)
        raise AssertionError("check_peer_uid accepted over TCP")
    except ValueError as e:
        print(f"PASS [tcp] check_peer_uid refused: {e}")

    try:
        await Server().start(SEATS, port=0, tls_dir=os.path.join(PKI, "nope"))
        raise AssertionError("started without TLS files")
    except OSError as e:
        print(f"PASS [tcp] missing TLS files fail at start(): {e}")



# ---------------------------------------------------------------------------
# Stage 3: heartbeats
# ---------------------------------------------------------------------------

HB = {"heartbeat_interval": 0.2, "heartbeat_misses": 3}   # dead after 0.6s of silence
LIMIT = 0.6


async def test_heartbeat_keeps_idle_link(kind: str):
    """No game traffic for many heartbeat limits: the link stays up, and no
    HEARTBEAT ever reaches the game on either side."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        net = Net(kind, tmp)
        server = await net.server(**HB)
        client = await net.client("good", **HB)
        await next_event(server, "good", "_connected")
        await asyncio.sleep(LIMIT * 4)
        assert server.connected() == ["good"]
        try:
            pid, msg = await server.recv_any(timeout=0.1)
            raise AssertionError(f"moderator inbox got {pid}: {msg}")
        except TimeoutError:
            pass
        try:
            msg = await client.recv(timeout=0.1)
            raise AssertionError(f"player got {msg}")
        except TimeoutError:
            pass
        learned = (server._conns["good"].peer_interval, client._conn.peer_interval)
        assert learned == (0.2, 0.2), learned
        await client.close()
        await server.close()
    print(f"PASS [{kind}] idle {LIMIT * 4:.1f}s (4x the limit): still connected, "
          f"no heartbeat leaked to either side; peer intervals learned {learned}")


async def test_silent_player_detected(kind: str):
    """A seated peer that sends nothing at all is dropped within the limit."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        net = Net(kind, tmp)
        server = await net.server(**HB)
        _, w = await net.raw("bad")
        await next_event(server, "bad", "_connected")
        start = time.monotonic()
        ev = await next_event(server, "bad", "_disconnected", timeout=LIMIT + 2)
        elapsed = time.monotonic() - start
        assert "no heartbeat" in ev["reason"], ev
        assert elapsed < LIMIT + 0.3, f"took {elapsed:.2f}s"
        w.close()
        await server.close()
    print(f"PASS [{kind}] silent player dropped after {elapsed:.2f}s: {ev['reason']!r}")


async def test_silent_moderator_detected():
    """A moderator that accepts but never sends: the player's recv() ends."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        path = os.path.join(tmp, "sock")

        async def mute(reader, writer):
            await reader.read()   # keep the link open, say nothing

        srv = await asyncio.start_unix_server(mute, path=path)
        client = Client(**HB)
        await client.connect(path)
        start = time.monotonic()
        try:
            await client.recv(timeout=LIMIT + 2)
            raise AssertionError("recv() returned")
        except ConnectionClosed as e:
            reason = str(e)
        elapsed = time.monotonic() - start
        assert "no heartbeat" in reason and elapsed < LIMIT + 0.3, (reason, elapsed)
        await client.close()
        srv.close()
    print(f"PASS [unix] silent moderator: player's recv() raised after {elapsed:.2f}s: {reason!r}")


class BlackHoleProxy:
    """
    A TCP relay between player and moderator that can start silently
    discarding everything, in both directions, while keeping both TCP
    connections open. Its kernel still ACKs every segment, so neither end's
    TCP stack sees a problem: no reset, no retransmission timeout, and
    keepalive probes are answered. Only application heartbeats can tell.
    """

    def __init__(self, target_port: int):
        self.target_port = target_port
        self.dropping = False

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._relay, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _relay(self, client_r, client_w):
        server_r, server_w = await asyncio.open_connection("127.0.0.1", self.target_port)
        await asyncio.gather(self._pipe(client_r, server_w), self._pipe(server_r, client_w),
                             return_exceptions=True)

    async def _pipe(self, reader, writer):
        try:
            while data := await reader.read(65536):
                if not self.dropping:
                    writer.write(data)
                    await writer.drain()
        finally:
            writer.close()


async def test_blackhole_both_ends_notice():
    """The network drops everything silently: BOTH ends notice within the limit."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        net = Net("tcp", tmp)
        server = await net.server(**HB)
        proxy = BlackHoleProxy(net.port)
        proxy_port = await proxy.start()
        client = await net.client("good", port=proxy_port, **HB)
        await next_event(server, "good", "_connected")
        await asyncio.sleep(LIMIT * 2)            # healthy through the proxy...
        assert server.connected() == ["good"]

        proxy.dropping = True                     # ...then the partition
        start = time.monotonic()

        async def player_notices():
            try:
                await client.recv(timeout=LIMIT + 2)
            except ConnectionClosed as e:
                return time.monotonic() - start, str(e)
            raise AssertionError("player never noticed")

        (p_time, p_reason), ev = await asyncio.gather(
            player_notices(), next_event(server, "good", "_disconnected", timeout=LIMIT + 2))
        m_time = time.monotonic() - start
        assert "no heartbeat" in ev["reason"] and "no heartbeat" in p_reason, (ev, p_reason)
        assert max(p_time, m_time) < LIMIT + 0.3, (p_time, m_time)
        await client.close()
        await server.close()
        proxy.server.close()
    print(f"PASS [tcp] black-holed link: moderator dropped the player after {m_time:.2f}s, "
          f"player gave up on the moderator after {p_time:.2f}s ({p_reason!r})")


async def test_peer_interval_respected():
    """The detector uses the SENDER's announced interval: a player beating
    every 0.5s isn't dropped by a moderator that beats every 0.1s (whose own
    interval x misses would be just 0.3s)."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        net = Net("tcp", tmp)
        server = await net.server(heartbeat_interval=0.1, heartbeat_misses=3)
        client = await net.client("good", heartbeat_interval=0.5, heartbeat_misses=3)
        await next_event(server, "good", "_connected")
        await asyncio.sleep(1.2)
        assert server.connected() == ["good"], "slow-beating player was dropped"
        assert server._conns["good"].peer_interval == 0.5
        await client.close()
        await server.close()
    print("PASS [tcp] moderator (0.1s x 3) kept a player that beats every 0.5s: "
          "the limit follows the sender's interval")


async def test_heartbeat_config_validated():
    for bad in ({"heartbeat_interval": 0}, {"heartbeat_interval": 61},
                {"heartbeat_misses": 0}):
        for cls in (Server, Client):
            try:
                cls(**bad)
                raise AssertionError(f"{cls.__name__}({bad}) accepted")
            except ValueError:
                pass
    print("PASS heartbeat settings validated (interval in (0, 60], misses >= 1)")


async def main():
    for kind in ("unix", "tcp"):
        await test_bad_frames(kind)
        await test_send_timeout(kind)
        await test_close_with_stuck_peer(kind)
    await test_client_sentinel_on_garbage()
    await test_client_sentinel_on_unexpected_error()
    await test_tcp_star()
    await test_tcp_socket_options()
    await test_tcp_refusals()
    await test_tcp_connect_retry_and_close()
    for kind in ("unix", "tcp"):
        await test_heartbeat_keeps_idle_link(kind)
        await test_silent_player_detected(kind)
    await test_silent_moderator_detected()
    await test_blackhole_both_ends_notice()
    await test_peer_interval_respected()
    await test_heartbeat_config_validated()
    print("all commslib tests passed")


if __name__ == "__main__":
    asyncio.run(main())
