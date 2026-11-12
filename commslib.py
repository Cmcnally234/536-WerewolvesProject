"""
commslib.py -- the ONLY file in this project that touches sockets.

Transport: TCP + mutual TLS 1.3, driven by asyncio. The moderator listens
on ONE host:port; every player connects to it -- from this machine or any
other. Both ends present certificates signed by the game's own CA
(make_certs.sh) and trust nothing else. The seat is the CN of the player's
*verified* certificate; the player's HELLO must name that same seat, and the
moderator answers WELCOME or an error. See DISTRIBUTED.md.

Topology: star -- players only ever talk to the moderator.

Sessions: every connection starts with HELLO -> WELCOME.
The WELCOME carries a session token. Game messages are numbered and kept
until acknowledged (_Session), so when a link breaks the player reconnects
with its token and both sides replay what the other missed -- in order,
with no duplicates. See DISTRIBUTED.md for the protocol.

Wire format (framing):
    +----------------------+------------------------------+
    | 4 bytes, big-endian  |  N bytes of UTF-8 JSON       |
    | unsigned length N    |  (one message = one dict)    |
    +----------------------+------------------------------+

Why framing is needed: a stream socket is a byte stream with no message
boundaries. Two quick send() calls can arrive as one read, and one large
send() can arrive split across several reads. The length prefix tells the
receiver exactly how many bytes make up the next message.

Inbox event types the Server generates itself (prefixed with "_"):
    _connected     a player took its seat (a new session)
    _away          its link broke ("reason"); the seat is held for
                   "resume_timeout" seconds and messages to it are kept
    _resumed       it came back ("away" seconds, "replayed" messages)
    _disconnected  its session is over for good ("reason": left the game,
                   didn't come back in time, protocol error, ...)
    _rejected      a connection was turned away ("reason" says why): seat
                   already taken or a bad session token (filed under that
                   seat), or a failed TLS handshake / a certificate or HELLO
                   naming no seat or the wrong one (player id None)

Public API:
    tls_paths(identity, tls_dir) -> (CA cert, cert, key) for TLS
    ConnectionClosed, ProtocolError -- link failures (both ConnectionError)
    Connection  -- one framed, bidirectional link (used by Server and Client)
    Server      -- moderator side: start, send_to, multicast, broadcast, recv_any
    Client      -- player side:    connect, send, recv (reconnects by itself)
    SeatRefused -- Client.connect(): the moderator said no
"""

import asyncio
import collections
import hmac
import json
import os
import secrets
import socket
import ssl
import struct
import time

# ">I" = big-endian ("network order") unsigned 32-bit int -> the length prefix.
_HEADER = struct.Struct(">I")

# Refuse absurd lengths. If a peer sends garbage, the first 4 bytes could
# decode to e.g. 3 GB, and we would try to buffer all of it. 1 MiB is far
# more than any game message needs.
MAX_MESSAGE_BYTES = 1024 * 1024


# How long a single send may wait for the peer to make room before we give
# up on that peer. A player whose process is stopped (Ctrl-Z) or wedged stops
# reading; once its kernel buffer fills, drain() would wait forever and --
# because broadcast() waits for every send -- freeze the whole game with it.
DEFAULT_SEND_TIMEOUT = 5.0

# Application-level heartbeats (Connection.heartbeat). Both ends send one
# every HEARTBEAT_INTERVAL seconds; an end that hears NOTHING from its peer
# for HEARTBEAT_MISSES of the peer's intervals declares it dead. 2s x 3:
# a dead player is noticed within ~6s, well inside any game phase, while a
# couple of seconds of jitter or a slow frame never trips it.
HEARTBEAT = "HEARTBEAT"             # wire type; consumed by commslib, never delivered
DEFAULT_HEARTBEAT_INTERVAL = 2.0
DEFAULT_HEARTBEAT_MISSES = 3
# Cap on the interval a peer may announce, so "bounded time" stays bounded
# even if a peer is misconfigured (it's authenticated, not trusted blindly).
MAX_HEARTBEAT_INTERVAL = 60.0

# Sessions (see _Session). Every game message travels as
#   {"type": "DATA", "seq": n, "ack": m, "msg": {...the game message...}}
# BYE ends a session on purpose (game over, player quit): no resume.
DATA = "DATA"
BYE = "BYE"
# How long the moderator holds an away player's seat (and keeps its
# messages) for it to reconnect. 0 = no resuming: a broken link ends the
# session at once, as in Project 1.
DEFAULT_RESUME_TIMEOUT = 120.0
# Unacknowledged messages one session may hold. A whole game is a few
# hundred, so hitting this means the peer is hopelessly behind.
MAX_OUTBOX = 1000
# Room for the envelope around a game message within MAX_MESSAGE_BYTES.
ENVELOPE_SLACK = 256

# How long close() waits for buffered bytes to flush before hard-aborting.
# A graceful close waits until the peer has read everything we queued, which
# never happens if the peer has stopped reading.
CLOSE_TIMEOUT = 2.0


# --- TCP ------------------------------------------------------------------

# Loopback by default: least exposure unless the operator asks for more
# (--host <this machine's address> to accept players from other machines).
DEFAULT_TCP_HOST = "127.0.0.1"
DEFAULT_PORT = 5555

# TLS file layout, created by make_certs.sh:
#   <tls_dir>/ca.crt                     the game CA (trusted by both sides)
#   <tls_dir>/<identity>/cert.pem        that identity's certificate
#   <tls_dir>/<identity>/key.pem         its private key (0600, owned by it)
# <identity> is MODERATOR_ID or a player id. Defined here, in one place, for
# the same reason: so the two sides can never disagree about where a file is.
DEFAULT_TLS_DIR = "/etc/werewolf/tls"
MODERATOR_ID = "moderator"


def tls_paths(identity: str, tls_dir: str = DEFAULT_TLS_DIR) -> tuple[str, str, str]:
    """(CA certificate, certificate, private key) paths for `identity`."""
    return (os.path.join(tls_dir, "ca.crt"),
            os.path.join(tls_dir, identity, "cert.pem"),
            os.path.join(tls_dir, identity, "key.pem"))


def _tls_context(identity: str, tls_dir: str, server_side: bool) -> ssl.SSLContext:
    """
    The TLS settings for one end. Both ends:
      - trust ONLY the game CA (cafile given => the system's public CAs are
        not loaded), so a certificate from anywhere else is worthless here;
      - present their own certificate (that's what makes it *mutual*);
      - require TLS 1.3 -- both ends are ours, so there's no old peer to
        stay compatible with.
    """
    ca, cert, key = tls_paths(identity, tls_dir)
    purpose = ssl.Purpose.CLIENT_AUTH if server_side else ssl.Purpose.SERVER_AUTH
    try:
        ctx = ssl.create_default_context(purpose, cafile=ca)
        ctx.load_cert_chain(cert, key)
    except OSError as e:   # FileNotFoundError, PermissionError, ssl.SSLError
        raise OSError(f"can't load TLS files for {identity!r} from {tls_dir}: {e}") from e
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    if server_side:
        # A server context doesn't ask for client certificates by default.
        # CERT_REQUIRED: no certificate, or one our CA didn't sign, fails
        # the handshake -- before a single byte of game data is read.
        ctx.verify_mode = ssl.CERT_REQUIRED
    # Client side, create_default_context already did the right thing:
    # CERT_REQUIRED plus check_hostname, so the moderator's certificate must
    # be signed by our CA AND list the address we dialled in its SANs.
    return ctx


def _cert_common_name(cert: dict | None) -> str | None:
    """
    The subject CN from a verified peer certificate, or None if it doesn't
    have exactly one. getpeercert() gives the subject as a tuple of RDNs,
    each a tuple of (field, value) pairs.
    """
    if not cert:
        return None
    names = [value for rdn in cert.get("subject", ()) for field, value in rdn
             if field == "commonName"]
    return names[0] if len(names) == 1 else None

# How long either side waits for the other's half of the HELLO/WELCOME
# handshake, and how long a player waits for the TCP connect itself (an
# unreachable host would otherwise take ~2 minutes of SYN retries to fail).
HELLO_TIMEOUT = 5.0
CONNECT_TIMEOUT = 5.0

# Kernel-level liveness for TCP links (Connection.heartbeat adds our own,
# application-level heartbeats on top). With keepalive, an idle link whose peer has vanished -- crashed host,
# pulled cable -- is noticed after IDLE + INTERVAL * COUNT = 25s instead of
# never. Keepalive only probes an IDLE link, though; if we have unacked data
# in flight, TCP retransmits instead, for ~15 minutes by default.
# TCP_USER_TIMEOUT caps that: unacked data older than this kills the link.
KEEPALIVE_IDLE = 10        # seconds of silence before the first probe
KEEPALIVE_INTERVAL = 5     # seconds between probes
KEEPALIVE_COUNT = 3        # unanswered probes before the kernel gives up
TCP_USER_TIMEOUT_MS = 30_000


def _tune_tcp(writer: asyncio.StreamWriter) -> None:
    """Socket options for every TCP link, on both ends."""
    sock = writer.get_extra_info("socket")
    # TCP_NODELAY turns off Nagle's algorithm, which holds back a small
    # write until the previous one is acked so it can batch them. Our
    # messages are small and latency-sensitive (a vote, a chat line); with
    # Nagle on, one could sit in our buffer for a whole round trip, or
    # ~40ms more when it meets the peer's delayed ACK. (asyncio already
    # sets this; we set it explicitly because we depend on it.)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, KEEPALIVE_IDLE)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, KEEPALIVE_INTERVAL)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, KEEPALIVE_COUNT)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, TCP_USER_TIMEOUT_MS)


def _check_heartbeat(interval: float, misses: int) -> None:
    if not 0 < interval <= MAX_HEARTBEAT_INTERVAL:
        raise ValueError(f"heartbeat interval must be in (0, {MAX_HEARTBEAT_INTERVAL}]")
    if misses < 1:
        raise ValueError("heartbeat misses must be at least 1")


class ConnectionClosed(ConnectionError):
    """
    The link is gone (peer hung up, reset, timed out or broke the protocol).

    A ConnectionError subclass, so code that already catches ConnectionError
    (e.g. Game.tell) handles every link failure without listing them all.
    """


class SeatRefused(ConnectionClosed):
    """
    Client.connect(): the moderator turned us away -- it answered HELLO with
    an error, or rejected our certificate during the handshake.
    """


class ProtocolError(ConnectionClosed):
    """
    The peer sent a frame we refuse: oversized, not UTF-8, not JSON, or not
    a JSON object. We hang up on such a peer rather than skip the frame: a
    peer that sends one bad frame is buggy or hostile, and in either case
    nothing it sends afterwards can be trusted.
    """


# ---------------------------------------------------------------------------
# Connection: framing on top of an asyncio (reader, writer) pair
# ---------------------------------------------------------------------------

class Connection:
    """
    A single framed link. Wraps asyncio's StreamReader/StreamWriter.

    Also does liveness (see heartbeat()): every frame received counts as
    proof that the peer is alive. It knows nothing about sequence numbers
    -- that's _Session's job, one layer up -- so a link can come and go
    while the session carries on over the next one.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        # Liveness bookkeeping. time.monotonic() is the clock asyncio's own
        # loop.time() uses; it never jumps when the wall clock is changed.
        self.last_heard = time.monotonic()
        self.peer_interval: float | None = None   # from the peer's HEARTBEATs
        # Why WE killed the link, if we did. recv() reports this instead of
        # the generic "peer closed the connection" it would otherwise see.
        self._abort_reason: str | None = None

    def write(self, msg: dict) -> None:
        """
        Hand one frame to the transport. Deliberately NOT a coroutine:

        - header and body go out in ONE write() call, and no other task can
          run in the middle of a plain function call, so frames from
          different tasks can never interleave on the wire (no lock needed);
        - a frame is either entirely in the send buffer or not at all, so
          cancelling a later drain() can never leave half a frame behind;
        - _Session relies on it: it numbers a message and writes it in the
          same synchronous step, so wire order always equals seq order.
        """
        if not isinstance(msg, dict):
            raise TypeError("messages must be dicts")
        body = json.dumps(msg).encode("utf-8")
        if len(body) > MAX_MESSAGE_BYTES:
            raise ValueError(f"message too large ({len(body)} bytes)")
        if self._writer.transport.is_closing():
            # Writing to a dead transport is silently discarded (and logged
            # by asyncio); saying so lets callers fall back to the outbox.
            raise ConnectionClosed(self._abort_reason or "the link is closed")
        self._writer.write(_HEADER.pack(len(body)) + body)

    async def drain(self) -> None:
        """
        Wait until the OS has room for what write() queued. Without it, a
        slow reader would make our in-memory buffer grow forever.
        """
        await self._writer.drain()

    async def send(self, msg: dict) -> None:
        self.write(msg)
        await self.drain()

    async def recv(self) -> dict:
        """
        The next frame, whatever its type. Records it as a sign of life, and
        checks a HEARTBEAT's interval (the rest of it is _Session's business).
        """
        msg = await self._recv_frame()
        self.last_heard = time.monotonic()
        if msg.get("type") == HEARTBEAT:
            interval = msg.get("interval")
            # bool is an int subclass, so `True` would otherwise pass as 1.
            if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                    or not 0 < interval <= MAX_HEARTBEAT_INTERVAL):
                raise ProtocolError(f"bad heartbeat interval {interval!r}")
            self.peer_interval = float(interval)
        return msg

    async def _recv_frame(self) -> dict:
        try:
            # readexactly() keeps reading until it has exactly n bytes, which
            # is precisely what framing needs: first the 4-byte header, then
            # exactly `length` bytes of body -- no more, no less.
            header = await self._reader.readexactly(_HEADER.size)
            (length,) = _HEADER.unpack(header)
            if length > MAX_MESSAGE_BYTES:
                raise ProtocolError(f"peer sent oversized frame ({length} bytes)")
            body = await self._reader.readexactly(length)
        except ConnectionClosed:
            raise  # our own oversized-frame error; it's an OSError too
        except asyncio.IncompleteReadError as e:
            # EOF arrived mid-message (or before one).
            raise ConnectionClosed(self._abort_reason or "peer closed the connection") from e
        except OSError as e:
            # Resets, broken pipes, TCP/TLS errors -- ssl.SSLError is an
            # OSError too. All mean the same thing to the caller.
            raise ConnectionClosed(self._abort_reason or f"connection error: {e!r}") from e

        # The frame boundary is intact here, but the contents still come from
        # the peer and may be garbage. Without these checks a bad body would
        # raise an exception the reader loops don't expect (killing them
        # silently), and a valid-JSON non-dict like [1, 2] would reach the
        # game logic and crash it on msg.get().
        try:
            msg = json.loads(body.decode("utf-8"))
        except ValueError as e:  # UnicodeDecodeError and JSONDecodeError
            raise ProtocolError(f"peer sent a malformed frame: {e}") from e
        if not isinstance(msg, dict):
            raise ProtocolError(f"peer sent a {type(msg).__name__}, not an object")
        return msg

    async def heartbeat(self, interval: float, misses: int, ack) -> None:
        """
        Run for the life of the link (as its own task): send a HEARTBEAT
        every `interval` seconds, and abort the link if the peer goes silent
        for `misses` of ITS heartbeat intervals. `ack()` gives the value to
        piggyback on each heartbeat (see _Session), so acknowledgements keep
        flowing even when no game messages are.

        Why at the application level, when TCP has keepalive? Keepalive is
        answered by the peer's KERNEL: a frozen or deadlocked process still
        "answers". And a middlebox that accepts our packets but never
        delivers them (or a silently dropping network, with TCP_USER_TIMEOUT
        not yet expired) looks fine from the socket's point of view. Only
        the far-end *program* producing frames proves it's alive.

        Detection is bounded: a peer is declared dead at most
        misses * peer_interval seconds after the last frame we got from it.

        The interval belongs to the SENDER (it's announced in each heartbeat)
        and the miss limit to the DETECTOR, so two ends configured
        differently still agree on when a heartbeat is late.
        """
        next_send = time.monotonic()   # the first heartbeat goes out at once
        while True:
            now = time.monotonic()
            # Until the peer's first heartbeat tells us its interval, assume
            # it uses ours.
            peer_interval = self.peer_interval or interval
            limit = misses * peer_interval
            silent = now - self.last_heard
            if silent >= limit:
                self.abort(f"no heartbeat for {silent:.1f}s "
                           f"(limit {misses} x {peer_interval:g}s)")
                return
            if now >= next_send:
                try:
                    self.write({"type": HEARTBEAT, "interval": interval, "ack": ack()})
                    # Bounded like every other send. If it times out, the frame
                    # is still queued, and the silence check above decides
                    # whether the peer is gone.
                    await asyncio.wait_for(self.drain(), interval)
                except TimeoutError:
                    pass
                except ConnectionError:
                    return   # the link is already gone; the reader reports it
                next_send = now + interval
            # Sleep until the next thing that can happen: our next send, or
            # the moment the peer would be overdue. If a frame arrives in the
            # meantime, last_heard moves and the next pass just sleeps again.
            wake = min(next_send, self.last_heard + limit)
            await asyncio.sleep(max(wake - time.monotonic(), 0))

    def is_closing(self) -> bool:
        return self._writer.transport.is_closing()

    def abort(self, reason: str | None = None) -> None:
        """Drop the link immediately, discarding anything still buffered."""
        if reason and self._abort_reason is None:
            self._abort_reason = reason
        self._writer.transport.abort()

    async def close(self) -> None:
        self._writer.close()
        try:
            # Bounded: a graceful close waits for our unsent bytes to be read,
            # and a peer that has stopped reading would make that forever.
            await asyncio.wait_for(self._writer.wait_closed(), CLOSE_TIMEOUT)
        except TimeoutError:
            self.abort()
        except OSError:
            pass  # already gone; nothing to clean up


# ---------------------------------------------------------------------------
# _Session: sequence numbers, acks and replay -- the part that survives a link
# ---------------------------------------------------------------------------

def _is_count(value) -> bool:
    """A non-negative int (and not a bool, which is an int subclass)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class _Session:
    """
    One seat's message stream, numbered so it can outlive any single link.
    BOTH ends keep one (the Server one per seat, the Client one for itself),
    so both directions get the same guarantees:

      out_seq  number of the last message we sent; each new one gets +1
      outbox   (seq, msg) for everything sent but not yet acknowledged
      in_seq   number of the last message we DELIVERED; doubles as our ack

    Every frame we send carries ack = in_seq ("I have everything up to
    here"); the peer then forgets those from its outbox. On a new link
    each side states its in_seq in the handshake, and the other replays
    everything after it, in order. The receiver delivers only seq ==
    in_seq + 1 and silently drops seq <= in_seq, so a replay that overlaps
    what already arrived can never produce a duplicate.
    """

    def __init__(self, token: str):
        self.token = token
        self.out_seq = 0
        self.outbox: collections.deque[tuple[int, dict]] = collections.deque()
        self.in_seq = 0
        self.conn: Connection | None = None      # the live link; None while away
        self.away_since = 0.0                    # Server side, for _resumed
        self.expiry: asyncio.TimerHandle | None = None   # Server side

    def send(self, msg: dict) -> None:
        """
        Number `msg`, keep it until acked, and write it to the live link if
        there is one. Synchronous, so numbering and writing can't be pulled
        apart by another task (see Connection.write). Raises ValueError /
        TypeError for a bad message, ConnectionClosed if the peer has fallen
        hopelessly behind.
        """
        if not isinstance(msg, dict):
            raise TypeError("messages must be dicts")
        # Check the size up front: a message that can't be framed must never
        # get a sequence number, or every later one would wait behind it.
        if len(json.dumps(msg).encode("utf-8")) > MAX_MESSAGE_BYTES - ENVELOPE_SLACK:
            raise ValueError("message too large")
        if len(self.outbox) >= MAX_OUTBOX:
            raise ConnectionClosed(f"{MAX_OUTBOX} messages unacknowledged: peer too far behind")
        self.out_seq += 1
        self.outbox.append((self.out_seq, msg))
        if self.conn is not None:
            try:
                self.conn.write(self._data(self.out_seq, msg))
            except ConnectionError:
                pass   # the link is dying; the message waits in the outbox

    def _data(self, seq: int, msg: dict) -> dict:
        return {"type": DATA, "seq": seq, "ack": self.in_seq, "msg": msg}

    def on_frame(self, frame: dict) -> dict | None:
        """
        Process one frame from the peer. Returns the game message to deliver,
        or None (a heartbeat, or a duplicate). ProtocolError if it's invalid.
        """
        kind = frame.get("type")
        if kind not in (DATA, HEARTBEAT):
            raise ProtocolError(f"unexpected {kind!r} frame")
        self.on_ack(frame.get("ack"))
        if kind == HEARTBEAT:
            return None
        seq, msg = frame.get("seq"), frame.get("msg")
        if not _is_count(seq) or seq == 0 or not isinstance(msg, dict):
            raise ProtocolError("malformed DATA frame")
        if seq <= self.in_seq:
            return None     # already delivered: a replay overlapping what we had
        if seq != self.in_seq + 1:
            # Can't happen on a stream socket with correct replay -- if it
            # does, delivering it would silently lose the messages between.
            raise ProtocolError(f"sequence gap: expected {self.in_seq + 1}, got {seq}")
        self.in_seq = seq
        return msg

    def on_ack(self, ack) -> None:
        """The peer has everything up to `ack`: forget those."""
        if not _is_count(ack) or ack > self.out_seq:
            raise ProtocolError(f"bad ack {ack!r} (we have only sent {self.out_seq})")
        while self.outbox and self.outbox[0][0] <= ack:
            self.outbox.popleft()

    def pending_after(self, ack: int) -> int:
        """How many messages a peer that has everything up to `ack` is missing."""
        return sum(1 for seq, _ in self.outbox if seq > ack)

    def attach(self, conn: Connection, peer_ack: int) -> int:
        """
        Make `conn` the live link: forget what the peer says it already has,
        then replay the rest, oldest first. Returns how many were replayed.
        Synchronous for the same reason as send(): a message sent by another
        task can't slip in between the replay and the link going live.
        """
        self.on_ack(peer_ack)
        self.conn = conn
        for seq, msg in self.outbox:
            conn.write(self._data(seq, msg))
        return len(self.outbox)


# ---------------------------------------------------------------------------
# Server: moderator side of the star
# ---------------------------------------------------------------------------

class Server:
    """
    The moderator's side of the star: ONE listening TCP socket for everybody.
    Each connection is upgraded to TLS; its seat is the CN of the player's
    verified certificate (see _handshake_tls). It then says HELLO, and gets a
    session: a new one, or (with the session token) the one it had before
    its link broke.

    Concurrency model: every accepted connection gets its own reader task.
    Each reader task loops pulling frames off its socket and dropping the
    game messages, tagged with the player id, into ONE shared asyncio.Queue
    (the "inbox"). The moderator's game logic then only has to await
    recv_any() -- it never blocks on any single player.
    """

    def __init__(self, send_timeout: float = DEFAULT_SEND_TIMEOUT,
                 heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
                 heartbeat_misses: int = DEFAULT_HEARTBEAT_MISSES,
                 resume_timeout: float = DEFAULT_RESUME_TIMEOUT):
        _check_heartbeat(heartbeat_interval, heartbeat_misses)
        if resume_timeout < 0:
            raise ValueError("resume_timeout can't be negative")
        self.send_timeout = send_timeout
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_misses = heartbeat_misses
        self.resume_timeout = resume_timeout
        self.port: int | None = None      # the port actually bound (see start)
        self._tls: ssl.SSLContext | None = None
        self._handshaking: set[asyncio.Task] = set()   # connections not seated yet
        self._seats: list[str] = []
        self._listeners: list[asyncio.base_events.Server] = []
        self._sessions: dict[str, _Session] = {}   # seat -> its session (live or away)
        self._ended: set[str] = set()              # seats whose session is over for good
        self._roster_locked = False                # see lock_roster()
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._all_connected = asyncio.Event()
        self._closing = False

    async def start(
        self,
        player_ids,
        *,
        host: str | None = None,
        port: int = DEFAULT_PORT,
        tls_dir: str = DEFAULT_TLS_DIR,
    ) -> None:
        """
        Start listening for the given seats: one TCP listener on host:port
        (port=0 picks a free port; read it back from self.port), using the
        moderator's certificate and the CA from tls_dir. Raises OSError if
        those can't be loaded, or the port can't be bound.
        """
        self._seats = list(player_ids)
        # Built before listening, so a missing or unreadable key fails here,
        # at startup, instead of on every connection.
        self._tls = _tls_context(MODERATOR_ID, tls_dir, server_side=True)

        async def on_connect(reader, writer):
            await self._accept(self._handshake_tls(reader, writer))

        # The listener itself is plain TCP: _handshake_tls upgrades each
        # connection to TLS itself, before anything else is read (see there
        # for why). There is no other path from accept() to a seat.
        # reuse_address (SO_REUSEADDR): restarting the moderator right after
        # a game would otherwise fail with "Address already in use" while
        # the old connections sit in TIME_WAIT for a minute.
        listener = await asyncio.start_server(on_connect, host or DEFAULT_TCP_HOST, port,
                                              reuse_address=True)
        self.port = listener.sockets[0].getsockname()[1]
        self._listeners.append(listener)

    # --- admitting a connection ---------------------------------------------

    async def _accept(self, handshake) -> None:
        """
        Runs as its own task for each connection: TLS handshake (`handshake`),
        then HELLO -> session, then serve it.
        """
        # Tracked until seated, so close() can cancel a handshake in progress.
        task = asyncio.current_task()
        self._handshaking.add(task)
        try:
            identified = await handshake
            admitted = await self._hello(*identified) if identified else None
        finally:
            self._handshaking.discard(task)
        if admitted is not None:
            await self._serve_link(*admitted)

    async def _handshake_tls(self, reader, writer):
        """
        TLS handshake; the seat is the CN of the verified certificate.
        Returns (seat, conn, mismatch text) or None.

        All seats share one port, so the socket says nothing about who is on
        it. The player's certificate does: the TLS handshake has verified it
        against our CA, so it's something the peer can't forge (short of
        stealing that player's key). The HELLO's "player" field is only
        *checked* against it, never trusted on its own.
        """
        _tune_tcp(writer)
        try:
            # The TLS handshake runs here instead of inside start_server(ssl=)
            # so that a failed handshake comes back to US as an exception we
            # can report (_rejected) -- start_server would only log it.
            # Bounded, like HELLO: a peer that never finishes the handshake
            # must not hold the task open.
            await writer.start_tls(self._tls, ssl_handshake_timeout=HELLO_TIMEOUT)
        except (OSError, TimeoutError) as e:   # ssl.SSLError is an OSError
            writer.transport.abort()
            reason = getattr(e, "reason", None) or str(e) or type(e).__name__
            await self._inbox.put((None, {"type": "_rejected",
                                          "reason": f"TLS handshake failed: {reason}"}))
            return None
        conn = Connection(reader, writer)
        cert_name = _cert_common_name(writer.get_extra_info("peercert"))
        if cert_name not in self._seats:
            # Signed by our CA, but not for any seat in this game (an old
            # player name, the moderator's own certificate, ...).
            reason = f"certificate is for {cert_name!r}, which is not a seat in this game"
            await self._refuse(conn, reason)
            await self._inbox.put((None, {"type": "_rejected", "reason": reason}))
            return None
        return cert_name, conn, f"certificate is for {cert_name!r}"

    async def _hello(self, seat: str, conn: Connection, identity: str):
        """
        Read HELLO; give the connection a new session or resume its old one;
        send WELCOME and replay. Returns (seat, session, conn) to serve, or
        None if it was turned away.

        HELLO   {"player": seat, "token": None | str, "ack": int}
        WELCOME {"player", "token", "ack", "resumed", "replaying", "resume_timeout"}
        """
        try:
            # Bounded: a connection that never sends HELLO must not hold a
            # task (and a file descriptor) open forever. Cancelling a recv
            # mid-frame is safe HERE only because we hang up straight after.
            hello = await asyncio.wait_for(conn.recv(), HELLO_TIMEOUT)
        except (TimeoutError, ConnectionError):
            await conn.close()
            return None
        claimed, token, ack = hello.get("player"), hello.get("token"), hello.get("ack", 0)
        if (hello.get("type") != "HELLO" or not isinstance(claimed, str)
                or not isinstance(token, (str, type(None))) or not _is_count(ack)):
            await self._refuse(conn, "expected HELLO first")
            return None
        # The seat it names must be the certificate's.
        if claimed != seat:
            reason = f"{identity}, not {claimed!r}"
            await self._refuse(conn, reason)
            await self._inbox.put((None, {"type": "_rejected", "reason": reason}))
            return None

        # From here to attach() there is no `await`, so two connections racing
        # for the same seat (or the same session) can't both get it: asyncio
        # only switches tasks at an await. For the same reason the link can't
        # die in the middle: checking it once here means the writes below
        # (WELCOME, replay) can't fail half-way through taking the seat.
        if conn.is_closing():
            return None
        session = self._sessions.get(seat)
        if token is None:
            if session is not None:
                reason = "seat already taken (a reconnect must present its session token)"
            elif seat in self._ended and self._roster_locked:
                reason = "this seat's session has ended and the game has started"
            else:
                reason = None
                session = self._sessions[seat] = _Session(secrets.token_urlsafe(16))
        elif session is None or not hmac.compare_digest(token, session.token):
            # compare_digest: constant time, so response timing can't be
            # used to guess the token one character at a time.
            reason = "unknown or expired session token"
        elif ack > session.out_seq:
            reason = f"ack {ack} is ahead of what we sent ({session.out_seq})"
        else:
            reason = None
        if reason is not None:
            await self._refuse(conn, reason)
            await self._inbox.put((seat, {"type": "_rejected", "reason": reason}))
            return None

        resumed = token is not None
        old = session.conn
        if old is not None:
            # The player noticed the dead link before we did (or it isn't
            # dead yet from here). The token proves it's the same session,
            # so the new link wins; the old reader sees it was replaced and
            # stays quiet.
            old.abort("replaced by a resumed connection")
        if session.expiry is not None:
            session.expiry.cancel()
            session.expiry = None
        conn.write({"type": "WELCOME", "player": seat, "token": session.token,
                    "ack": session.in_seq, "resumed": resumed,
                    "replaying": session.pending_after(ack),
                    "resume_timeout": self.resume_timeout})
        replayed = session.attach(conn, ack)

        if resumed:
            away = time.monotonic() - session.away_since if old is None else 0.0
            await self._inbox.put((seat, {"type": "_resumed", "replayed": replayed,
                                          "away": round(away, 2)}))
        else:
            await self._inbox.put((seat, {"type": "_connected"}))
            if len(self._sessions) == len(self._seats):
                self._all_connected.set()
        return seat, session, conn

    # --- serving one live link ----------------------------------------------

    async def _serve_link(self, seat: str, session: _Session, conn: Connection) -> None:
        """Pump one link's game messages into the inbox until the link ends."""
        # Runs beside this reader loop. If the player goes silent, it aborts
        # the link and conn.recv() below raises with its reason.
        heartbeat = asyncio.create_task(conn.heartbeat(
            self.heartbeat_interval, self.heartbeat_misses, ack=lambda: session.in_seq))
        reason, final = "closed", False
        try:
            # WELCOME and the replay were written in _hello(); now wait for
            # them to leave, bounded like any send.
            await asyncio.wait_for(conn.drain(), self.send_timeout)
            while True:
                frame = await conn.recv()
                if frame.get("type") == BYE:
                    reason, final = "left the game", True
                    break
                msg = session.on_frame(frame)
                if msg is not None:
                    await self._inbox.put((seat, msg))
        except TimeoutError:
            reason = f"stopped reading (replay not taken within {self.send_timeout}s)"
            conn.abort(reason)
        except ProtocolError as e:
            # See ProtocolError: a peer that breaks the protocol is not trusted
            # again, so its session ends rather than waiting for a resume.
            reason, final = str(e), True
        except ConnectionClosed as e:
            reason = str(e)
        except Exception as e:
            # Anything else is a bug, but it must still end the link
            # properly: otherwise the seat looks live forever and the game
            # waits on a player nobody is reading from.
            reason, final = f"reader failed: {e!r}", True
        finally:
            heartbeat.cancel()
            # Only the CURRENT link may change the session's state: if a
            # resume already replaced this link, the player isn't away.
            if session.conn is conn and not self._closing:
                session.conn = None
                if final:
                    self._end(seat, session, reason)
                else:
                    self._away(seat, session, reason)
            await conn.close()
            await asyncio.gather(heartbeat, return_exceptions=True)

    def _away(self, seat: str, session: _Session, reason: str) -> None:
        """The link is gone but the session isn't: hold the seat for a while."""
        if self.resume_timeout <= 0:
            self._end(seat, session, reason)
            return
        session.away_since = time.monotonic()
        session.expiry = asyncio.get_running_loop().call_later(
            self.resume_timeout, self._expire, seat, session, reason)
        # put_nowait: the inbox is unbounded, and this runs from a finally
        # block that may be under cancellation.
        self._inbox.put_nowait((seat, {"type": "_away", "reason": reason,
                                       "resume_timeout": self.resume_timeout}))

    def _expire(self, seat: str, session: _Session, reason: str) -> None:
        """Timer callback: the player didn't come back in time."""
        session.expiry = None
        if self._sessions.get(seat) is session and session.conn is None:
            self._end(seat, session,
                      f"did not come back within {self.resume_timeout:g}s (lost: {reason})")

    def _end(self, seat: str, session: _Session, reason: str) -> None:
        """The session is over for good: the game is told with _disconnected."""
        if self._sessions.get(seat) is not session:
            return   # already ended
        del self._sessions[seat]
        self._ended.add(seat)
        session.conn = None
        if session.expiry is not None:
            session.expiry.cancel()
            session.expiry = None
        self._inbox.put_nowait((seat, {"type": "_disconnected", "reason": reason}))

    async def _refuse(self, conn: Connection, text: str) -> None:
        """Best-effort error message, then hang up. Never raises."""
        try:
            await asyncio.wait_for(conn.send({"type": "error", "text": text}), self.send_timeout)
        except (TimeoutError, ConnectionError):
            pass  # the peer is being turned away anyway
        await conn.close()

    # --- who's here -----------------------------------------------------------

    async def wait_for_players(self, timeout: float | None = None) -> None:
        """Block until every seat has a session (or raise TimeoutError)."""
        await asyncio.wait_for(self._all_connected.wait(), timeout)

    def lock_roster(self) -> None:
        """
        From now on, a seat whose session has ended can't be taken by a new
        one. Call it when the game starts: by then each session has been
        sent things (a role!) that a fresh session wouldn't have. Before it
        -- in the lobby -- a player who left can simply join again.
        """
        self._roster_locked = True

    def connected(self) -> list[str]:
        """
        Seats still in the game: a live link, or away but resumable. Messages
        to an away seat are kept and replayed when it comes back, so for
        sending purposes it's as good as connected.
        """
        return list(self._sessions)

    def live(self) -> list[str]:
        """Seats with a working link right now."""
        return [seat for seat, s in self._sessions.items() if s.conn is not None]

    def undelivered(self) -> list[str]:
        """Seats with messages not yet acknowledged (in flight, or waiting for a resume)."""
        return [seat for seat, s in self._sessions.items() if s.outbox]

    async def wait_for_delivery(self, timeout: float) -> list[str]:
        """
        Wait up to `timeout` seconds until every player still in the game has
        acknowledged everything sent to it (or has left). Returns the seats
        that still haven't.

        For the end of a game, before close(). Without it, the last messages
        -- GAME_OVER! -- can be lost two ways:
          - a player that is reconnecting finds the moderator gone and never
            gets its replay;
          - on a slow, lossy link, TCP can still be retransmitting them when
            close() gives up flushing (CLOSE_TIMEOUT) and aborts the socket,
            which discards them.
        A player that gets GAME_OVER leaves at once (BYE ends its session),
        so on a healthy network this returns within milliseconds.
        """
        deadline = time.monotonic() + timeout
        # Polling is plenty: this runs once per game, and acks arrive within
        # a heartbeat interval.
        while (pending := self.undelivered()) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return pending

    # --- sending: 1:1, multicast, broadcast -------------------------------

    async def send_to(self, player_id: str, msg: dict) -> None:
        """
        1:1 -- send to exactly one player.

        If the player is away, the message waits in its outbox and is
        replayed, in order, when it comes back. If its link is up but it
        hasn't made room within send_timeout seconds, the link is dropped
        (the player is then away; the message is still kept).

        Raises KeyError if the seat has no session (never joined, or ended),
        ConnectionClosed if the player has fallen too far behind (its
        session is ended).
        """
        session = self._sessions.get(player_id)
        if session is None:
            raise KeyError(f"{player_id} is not in the game")
        try:
            session.send(msg)
        except ConnectionClosed as e:
            conn = session.conn
            self._end(player_id, session, str(e))
            if conn is not None:
                conn.abort(str(e))
            raise
        conn = session.conn
        if conn is None:
            return   # away: queued for replay
        try:
            await asyncio.wait_for(conn.drain(), self.send_timeout)
        except TimeoutError:
            # The player isn't reading, and every later send would pile up in
            # memory behind this one. Drop the link: the reader then marks
            # the player away, and the outbox still has the message.
            conn.abort(f"{player_id} stopped reading (send timed out after {self.send_timeout}s)")
        except ConnectionError:
            pass   # the link died; the reader reports it, the outbox keeps the message

    async def multicast(self, player_ids, msg: dict) -> list[str]:
        """
        Send the same message to a subset of players, concurrently.
        Returns the ids that could NOT be reached (no session).
        """
        player_ids = list(player_ids)
        # gather() runs all sends at once, so one slow player's full socket
        # buffer doesn't delay delivery to everyone else.
        # return_exceptions=True: one dead player must not abort the rest.
        results = await asyncio.gather(
            *(self.send_to(pid, msg) for pid in player_ids),
            return_exceptions=True,
        )
        return [pid for pid, r in zip(player_ids, results) if isinstance(r, Exception)]

    async def broadcast(self, msg: dict) -> list[str]:
        """Send to every player still in the game."""
        return await self.multicast(self.connected(), msg)

    # --- receiving ---------------------------------------------------------

    async def recv_any(self, timeout: float | None = None) -> tuple[str, dict]:
        """
        Return the next (player_id, msg) from ANY player.
        Raises TimeoutError if nothing arrives within `timeout` seconds.
        """
        return await asyncio.wait_for(self._inbox.get(), timeout)

    async def close(self) -> None:
        """
        Orderly shutdown: stop accepting, tell every live player the session
        is over (BYE, so it doesn't try to reconnect), hang up, and wait for
        each connection's reader task to finish. Anything already sent is
        still delivered: closing a TCP/TLS stream sends close_notify and FIN
        *after* the bytes already queued, never instead of them.
        """
        self._closing = True
        for listener in self._listeners:
            listener.close()                 # no new connections
        # Connections still in the TLS/HELLO handshake aren't seated yet.
        # Cancel their tasks: asyncio then closes each one cleanly. (Closing
        # their transports from here instead hits a CPython quirk: a clean
        # close mid-TLS-handshake makes start_tls() *return* -- with no
        # transport -- instead of raising.)
        for task in list(self._handshaking):
            task.cancel()
        live = []
        for session in self._sessions.values():
            if session.expiry is not None:
                session.expiry.cancel()
            if session.conn is not None:
                try:
                    session.conn.write({"type": BYE})
                except ConnectionError:
                    pass
                live.append(session.conn)
        await asyncio.gather(*(conn.close() for conn in live))
        for listener in self._listeners:
            try:
                # Returns once every connection handler has exited. Bounded,
                # so one wedged peer can't hang shutdown.
                await asyncio.wait_for(listener.wait_closed(), 2)
            except TimeoutError:
                pass


# ---------------------------------------------------------------------------
# Client: player side of the star
# ---------------------------------------------------------------------------

class Client:
    """
    A player's session with the moderator, carried over one link at a time.

    Like the Server, the Client reads the socket in a background task and
    puts complete messages into a queue. This matters for timeouts: if
    recv(timeout=...) cancelled a read *directly*, it might cancel after the
    4-byte header was consumed but before the body arrived -- the header
    would be lost and every later frame would be misread. Cancelling a
    queue.get() is always safe because frames enter the queue whole.

    If the link breaks (anything but BYE or a protocol error), the same
    background task reconnects with the session token and both sides replay
    what the other missed. recv() meanwhile just waits; send() queues. The
    game sees two extra events in recv():
        {"type": "_reconnecting", "reason", "resume_timeout"}
        {"type": "_resumed", "away", "replaying"}
    and ConnectionClosed only when the session is over for good.
    """

    def __init__(self, heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
                 heartbeat_misses: int = DEFAULT_HEARTBEAT_MISSES):
        _check_heartbeat(heartbeat_interval, heartbeat_misses)
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_misses = heartbeat_misses
        self._session: _Session | None = None
        self._dial = None                    # opens a fresh link (set by connect)
        self._player_id: str | None = None
        self._resume_timeout = 0.0           # from WELCOME: how long we may try
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._done = False
        self._closing = False                # close() called: don't reconnect
        self._closed_reason = "moderator closed the connection"

    async def connect(
        self,
        *,
        player_id: str,
        host: str | None = None,
        port: int = DEFAULT_PORT,
        tls_dir: str = DEFAULT_TLS_DIR,
        retry_seconds: float = 10.0,
    ) -> None:
        """
        Connect to the moderator over TCP + mutual TLS at host:port as
        player_id, using that player's certificate and the CA from tls_dir.

        Retries briefly because players may be launched a moment before the
        moderator is listening. These are final and NOT retried:
          SeatRefused                 the moderator turned us down
          ssl.SSLCertVerificationError  the moderator's certificate isn't
                                      from our CA, or doesn't name `host`
                                      -- we may be talking to an impostor
        """
        if not player_id:
            raise ValueError("player_id is required (it's the seat, and the certificate's name)")
        self._player_id = player_id
        host = host or DEFAULT_TCP_HOST
        self._dial = lambda: self._open_tcp(host, port, player_id, tls_dir)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + retry_seconds
        while True:
            try:
                conn = await self._dial()
                break
            except (ConnectionRefusedError, TimeoutError):
                # Nothing listening (yet), or the connect timed out.
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(0.2)
        welcome = await self._hello(conn, token=None, ack=0)
        self._session = _Session(welcome["token"])
        self._resume_timeout = welcome["resume_timeout"]
        self._session.attach(conn, welcome["ack"])
        self._task = asyncio.create_task(self._run(conn))

    async def _open_tcp(self, host: str, port: int, player_id: str, tls_dir: str) -> Connection:
        """TCP connect + TLS handshake. Returns the link."""
        tls = _tls_context(player_id, tls_dir, server_side=False)
        # server_hostname=host: check_hostname compares the address we
        # dialled against the SANs in the moderator's certificate.
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=tls, server_hostname=host,
                                    ssl_handshake_timeout=CONNECT_TIMEOUT),
            CONNECT_TIMEOUT)
        _tune_tcp(writer)
        return Connection(reader, writer)

    async def _hello(self, conn: Connection, token: str | None, ack: int) -> dict:
        """HELLO -> WELCOME on a fresh link. Returns the WELCOME."""
        try:
            conn.write({"type": "HELLO", "player": self._player_id, "token": token, "ack": ack})
            # Read the reply directly, not through the reader task: nothing
            # else may happen on this link until the moderator has said yes.
            # (Cancelling mid-frame is fine here: on timeout we hang up.)
            reply = await asyncio.wait_for(conn.recv(), HELLO_TIMEOUT)
        except TimeoutError:
            conn.abort()
            # Not a TimeoutError, so connect() doesn't retry: the moderator
            # IS there, it just didn't answer.
            raise ConnectionClosed(f"no WELCOME within {HELLO_TIMEOUT}s") from None
        except ConnectionClosed as e:
            conn.abort()
            # Our side of the TLS handshake succeeded, then the moderator
            # hung up before WELCOME. In TLS 1.3 the client's half of the
            # handshake finishes BEFORE the server has checked the client's
            # certificate, so this is what a rejected certificate looks like
            # from here. The TLS alert that would say *why* never arrives:
            # asyncio drops the connection without flushing it. The
            # moderator's log has the reason (a _rejected event).
            raise SeatRefused("the moderator hung up before WELCOME, most likely because "
                              f"it rejected our certificate (see its log): {e}") from e
        except BaseException:
            conn.abort()  # don't leak the socket on any other failure
            raise
        if reply.get("type") != "WELCOME":
            await conn.close()
            raise SeatRefused(reply.get("text") or f"unexpected {reply.get('type')!r} reply")
        if (not isinstance(reply.get("token"), str) or not _is_count(reply.get("ack"))
                or not isinstance(reply.get("resume_timeout"), (int, float))
                or reply.get("resumed") is not (token is not None)):
            conn.abort()
            raise ProtocolError(f"malformed WELCOME {reply}")
        return reply

    async def _run(self, conn: Connection) -> None:
        """
        The background task for the whole session: read the current link;
        when it breaks, reconnect and resume; repeat until the session ends.
        """
        try:
            while True:
                reason, final = await self._pump(conn)
                if final or self._closing or self._resume_timeout <= 0:
                    self._closed_reason = reason
                    return
                lost_at = time.monotonic()
                self._inbox.put_nowait({"type": "_reconnecting", "reason": reason,
                                        "resume_timeout": self._resume_timeout})
                resumed = await self._resume(reason)
                if resumed is None:
                    return          # _resume recorded why
                conn, welcome = resumed
                self._session.attach(conn, welcome["ack"])
                self._inbox.put_nowait({"type": "_resumed",
                                        "away": round(time.monotonic() - lost_at, 2),
                                        "replaying": welcome["replaying"]})
        except ProtocolError as e:      # e.g. a bad ack in the WELCOME
            self._closed_reason = str(e)
        except Exception as e:
            # A bug or an error we didn't foresee. Record it rather than
            # re-raise: nobody awaits this task, so a raised exception would
            # only surface as a log line at exit.
            self._closed_reason = f"reader failed: {e!r}"
        finally:
            self._done = True
            if self._session is not None and self._session.conn is not None:
                self._session.conn.abort()
                self._session.conn = None
            # The sentinel goes in on EVERY exit path -- BYE, garbage frame,
            # failed resume, unexpected exception, or cancellation by close().
            # Without it a player blocked in recv() would wait forever.
            self._inbox.put_nowait(None)

    async def _pump(self, conn: Connection) -> tuple[str, bool]:
        """Read one link until it fails. Returns (reason, final?)."""
        session = self._session
        # The player watches the moderator too: if it goes silent, the link
        # is aborted and recv() below raises with the reason.
        heartbeat = asyncio.create_task(conn.heartbeat(
            self.heartbeat_interval, self.heartbeat_misses, ack=lambda: session.in_seq))
        try:
            while True:
                frame = await conn.recv()
                if frame.get("type") == BYE:
                    return "the moderator ended the session", True
                msg = session.on_frame(frame)
                if msg is not None:
                    self._inbox.put_nowait(msg)
        except ProtocolError as e:
            return str(e), True
        except ConnectionClosed as e:
            return str(e), False
        finally:
            heartbeat.cancel()
            if session.conn is conn:
                session.conn = None
            conn.abort()    # it's dead (or done); nothing left worth flushing
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _resume(self, reason: str):
        """
        Keep redialling until the moderator takes us back, the window runs
        out, or it refuses. Returns (conn, welcome) or None.
        """
        deadline = time.monotonic() + self._resume_timeout
        delay = 0.2
        while True:
            try:
                conn = await self._dial()
                welcome = await self._hello(conn, self._session.token, self._session.in_seq)
                return conn, welcome
            except (SeatRefused, ssl.SSLCertVerificationError, ProtocolError) as e:
                # A definite no (e.g. our session already expired), or a
                # moderator we mustn't trust: retrying won't change that.
                self._closed_reason = f"could not resume: {e}"
                return None
            except (OSError, TimeoutError):
                pass   # not reachable yet: refused, unreachable, timed out, reset
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._closed_reason = (f"could not reconnect within "
                                       f"{self._resume_timeout:g}s (lost: {reason})")
                return None
            # Exponential backoff, capped: quick retries for a blip, without
            # hammering a moderator that is down for longer.
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, 2.0)

    async def send(self, msg: dict) -> None:
        """
        Send one message. While reconnecting it is queued and sent on resume;
        raises ConnectionClosed only once the session is over.
        """
        if self._done or self._session is None:
            raise ConnectionClosed(self._closed_reason)
        self._session.send(msg)
        conn = self._session.conn
        if conn is not None:
            try:
                await conn.drain()
            except ConnectionError:
                pass   # the link died; the message waits in the outbox

    async def recv(self, timeout: float | None = None) -> dict:
        """
        Next message from the moderator (or a _reconnecting / _resumed event).
        Raises TimeoutError if `timeout` expires, ConnectionClosed once the
        session is over.
        """
        msg = await asyncio.wait_for(self._inbox.get(), timeout)
        if msg is None:
            self._inbox.put_nowait(None)  # keep later recv() calls failing too
            raise ConnectionClosed(self._closed_reason)
        return msg

    async def close(self) -> None:
        """
        Leave for good: BYE (so the moderator ends our session at once rather
        than holding the seat for a resume), then stop the background task.
        """
        # Set first: closing the link below wakes our own reader with an EOF,
        # which must end the session, not start a reconnect.
        self._closing = True
        self._closed_reason = "closed"
        conn = self._session.conn if self._session is not None else None
        if conn is not None and not self._done:
            try:
                conn.write({"type": BYE})
                await conn.close()      # graceful: lets the BYE get out first
            except ConnectionError:
                pass
        if self._task is not None:
            self._task.cancel()
            # Wait for the cancellation to finish so no task is left dangling
            # ("Task was destroyed but it is pending!").
            await asyncio.gather(self._task, return_exceptions=True)
