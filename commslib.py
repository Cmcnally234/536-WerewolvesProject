"""
commslib.py -- the ONLY file in this project that touches sockets.

IPC primitive: *stream* sockets driven by asyncio, over one of two
transports (the framing and the API are identical on both):

  UNIX (Project 1, same machine). The moderator owns one listening socket
  per player (/run/werewolf/<player_id>/sock, see player_socket_path()).
  Each player connects only to its own socket. Because file permissions
  decide who can open which socket, the moderator knows *who* is on a
  connection by *which socket it arrived on*.

  TCP + mutual TLS (Project 2, across machines). The moderator listens on
  ONE host:port. Both ends present certificates signed by the game's own CA
  (make_certs.sh). The seat is the CN of the player's *verified*
  certificate; the player's HELLO must name that same seat, and the
  moderator answers WELCOME or an error. See DISTRIBUTED.md.

Topology: star either way -- players only ever talk to the moderator.

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
    _connected     a player took its seat
    _disconnected  a player's connection closed ("reason" says why, e.g.
                   "no heartbeat for 6.0s (limit 3 x 2s)")
    _rejected      a connection was turned away ("reason" says why): wrong
                   uid (SO_PEERCRED), seat already taken, or (TCP) a failed
                   TLS handshake / a certificate or HELLO naming no seat or
                   the wrong one -- those have player id None

Public API:
    player_socket_path(player_id, base_dir) -> the socket path for a seat
    tls_paths(identity, tls_dir) -> (CA cert, cert, key) for TLS
    ConnectionClosed, ProtocolError -- link failures (both ConnectionError)
    Connection  -- one framed, bidirectional link (used by Server and Client)
    Server      -- moderator side: start, send_to, multicast, broadcast, recv_any
    Client      -- player side:    connect, send, recv
    SeatRefused -- Client.connect() over TCP: the moderator said no
"""

import asyncio
import json
import os
import pwd
import socket
import ssl
import struct
import time

# ">I" = big-endian ("network order") unsigned 32-bit int -> the length prefix.
_HEADER = struct.Struct(">I")

# Socket layout:  <base_dir>/<player_id>/sock
# e.g. /run/werewolf/player1/sock. Each player's socket lives in its own
# directory because the *directory* is what carries the per-player group and
# permissions (created by setup_users.sh). Defined here, in one place, so the
# moderator and the players can never disagree about where a socket lives.
DEFAULT_SOCKET_DIR = "/run/werewolf"
SOCKET_NAME = "sock"


def player_socket_path(player_id: str, base_dir: str = DEFAULT_SOCKET_DIR) -> str:
    return os.path.join(base_dir, player_id, SOCKET_NAME)


def _peer_uid(writer: asyncio.StreamWriter) -> int:
    """
    Ask the kernel which uid is on the other end of a UNIX socket.

    SO_PEERCRED returns a C `struct ucred { pid_t pid; uid_t uid; gid_t gid; }`
    -- three 32-bit ints. The kernel fills it in at connect() time, so the
    peer cannot fake it (unlike anything it writes into a message).
    """
    sock = writer.get_extra_info("socket")
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid

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

# How long close() waits for buffered bytes to flush before hard-aborting.
# A graceful close waits until the peer has read everything we queued, which
# never happens if the peer has stopped reading.
CLOSE_TIMEOUT = 2.0


# --- TCP ------------------------------------------------------------------

# Loopback by default: least exposure unless the operator asks for more
# (--host 0.0.0.0 to accept players from other machines).
DEFAULT_TCP_HOST = "127.0.0.1"

# TLS file layout, created by make_certs.sh:
#   <tls_dir>/ca.crt                     the game CA (trusted by both sides)
#   <tls_dir>/<identity>/cert.pem        that identity's certificate
#   <tls_dir>/<identity>/key.pem         its private key (0600, owned by it)
# <identity> is MODERATOR_ID or a player id. Defined here, in one place, for
# the same reason as player_socket_path().
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

# Kernel-level liveness for TCP links (stage 3 adds our own heartbeats on
# top). With keepalive, an idle link whose peer has vanished -- crashed host,
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
    Client.connect() over TCP: the moderator turned us away -- it answered
    HELLO with an error, or rejected our certificate during the handshake.
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

    Also does liveness (see heartbeat()): recv() swallows the peer's
    HEARTBEAT frames, and every frame received -- heartbeat or not --
    counts as proof that the peer is alive.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        # Two different tasks could call send() at the same time (e.g. a
        # broadcast and a private reply). Each send is two logical pieces
        # (header + body); the lock guarantees they are never interleaved
        # with another message's bytes on the wire.
        self._send_lock = asyncio.Lock()
        # Liveness bookkeeping. time.monotonic() is the clock asyncio's own
        # loop.time() uses; it never jumps when the wall clock is changed.
        self.last_heard = time.monotonic()
        self.peer_interval: float | None = None   # from the peer's HEARTBEATs
        # Why WE killed the link, if we did. recv() reports this instead of
        # the generic "peer closed the connection" it would otherwise see.
        self._abort_reason: str | None = None

    async def send(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            raise TypeError("messages must be dicts")
        body = json.dumps(msg).encode("utf-8")
        if len(body) > MAX_MESSAGE_BYTES:
            raise ValueError(f"message too large ({len(body)} bytes)")
        async with self._send_lock:
            self._writer.write(_HEADER.pack(len(body)) + body)
            # drain() waits until the OS socket buffer has room. Without it,
            # a slow reader would make our in-memory buffer grow forever.
            await self._writer.drain()

    async def recv(self) -> dict:
        """The next frame that isn't a HEARTBEAT."""
        while True:
            msg = await self._recv_frame()
            self.last_heard = time.monotonic()
            if msg.get("type") != HEARTBEAT:
                return msg
            interval = msg.get("interval")
            # bool is an int subclass, so `True` would otherwise pass as 1.
            if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                    or not 0 < interval <= MAX_HEARTBEAT_INTERVAL):
                raise ProtocolError(f"bad heartbeat interval {interval!r}")
            self.peer_interval = float(interval)

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

    async def heartbeat(self, interval: float, misses: int) -> None:
        """
        Run for the life of the link (as its own task): send a HEARTBEAT
        every `interval` seconds, and abort the link if the peer goes silent
        for `misses` of ITS heartbeat intervals.

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
                    # Bounded like every other send. If it times out, the frame
                    # is still queued (see Server.send_to), and the silence
                    # check above decides whether the peer is gone.
                    await asyncio.wait_for(
                        self.send({"type": HEARTBEAT, "interval": interval}), interval)
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
# Server: moderator side of the star
# ---------------------------------------------------------------------------

class Server:
    """
    The moderator's side of the star. Two transports, chosen in start():

      UNIX  one listening socket per seat; the socket a connection arrives
            on says which seat it is (file permissions enforce that).
      TCP   ONE listening socket for everybody; a connection says which
            seat it wants in its first frame (HELLO) -- see _accept_tcp.

    Concurrency model (same for both): every accepted connection gets its
    own reader task. Each reader task loops forever pulling frames off its
    socket and dropping them, tagged with the player id, into ONE shared
    asyncio.Queue (the "inbox"). The moderator's game logic then only has
    to await recv_any() -- it never blocks on any single player.
    """

    def __init__(self, send_timeout: float = DEFAULT_SEND_TIMEOUT,
                 heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
                 heartbeat_misses: int = DEFAULT_HEARTBEAT_MISSES):
        _check_heartbeat(heartbeat_interval, heartbeat_misses)
        self.send_timeout = send_timeout
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_misses = heartbeat_misses
        self.port: int | None = None      # TCP: the port actually bound
        self._tls: ssl.SSLContext | None = None
        self._handshaking: set[asyncio.Task] = set()   # TCP: not seated yet
        self._seats: list[str] = []
        self._listeners: list[asyncio.base_events.Server] = []
        self._paths: dict[str, str] = {}
        self._conns: dict[str, Connection] = {}
        self._expected_uid: dict[str, int] = {}
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._all_connected = asyncio.Event()

    async def start(
        self,
        player_ids,
        base_dir: str = DEFAULT_SOCKET_DIR,
        check_peer_uid: bool = False,
        *,
        host: str | None = None,
        port: int | None = None,
        tls_dir: str = DEFAULT_TLS_DIR,
    ) -> None:
        """
        Start listening for the given seats.

        port=None (the default) -> UNIX: a listening socket at
        <base_dir>/<player_id>/sock for each player. The per-player
        directories must already exist. In the real setup, setup_users.sh
        creates them owned modbot:playerN, mode 2750. We do NOT create them
        here: a directory made by the moderator would have the moderator's
        group, and the isolation would silently disappear.
        check_peer_uid=True additionally requires each player_id to be a
        Linux username and rejects any connection whose kernel-reported uid
        (SO_PEERCRED) isn't that user's.

        port given -> TCP + mutual TLS: one listener on host:port (port=0
        picks a free port; read it back from self.port), using the
        moderator's certificate and the CA from tls_dir. base_dir is unused.
        """
        self._seats = list(player_ids)
        if port is not None:
            await self._start_tcp(host or DEFAULT_TCP_HOST, port, check_peer_uid, tls_dir)
        else:
            await self._start_unix(base_dir, check_peer_uid)

    async def _start_unix(self, base_dir: str, check_peer_uid: bool) -> None:
        for player_id in self._seats:
            seat_dir = os.path.join(base_dir, player_id)
            if not os.path.isdir(seat_dir):
                raise FileNotFoundError(
                    f"{seat_dir} does not exist -- run setup_users.sh first"
                )
            self._paths[player_id] = player_socket_path(player_id, base_dir)
            if check_peer_uid:
                # KeyError here means "no such Linux user" -- fail loudly at
                # startup rather than rejecting every connection later.
                self._expected_uid[player_id] = pwd.getpwnam(player_id).pw_uid

        for player_id, path in self._paths.items():
            # A leftover socket file from a crashed run would make bind()
            # fail with "Address already in use".
            if os.path.exists(path):
                os.unlink(path)

            # Default-argument trick binds the *current* player_id into the
            # callback; a plain closure would see only the last loop value.
            async def on_connect(reader, writer, player_id=player_id):
                await self._accept_unix(player_id, reader, writer)

            listener = await asyncio.start_unix_server(on_connect, path=path)
            # rw for owner (modbot) and group (playerN, inherited from the
            # setgid directory), nothing for others. connect() on a UNIX
            # socket requires WRITE permission on the socket file.
            # (Between bind() and this chmod the file briefly has umask
            # permissions, but the 2750 directory already keeps everyone
            # except modbot and playerN out, so that window is harmless.)
            os.chmod(path, 0o660)
            self._listeners.append(listener)

    async def _start_tcp(self, host: str, port: int, check_peer_uid: bool,
                         tls_dir: str) -> None:
        if check_peer_uid:
            # SO_PEERCRED only exists for UNIX sockets: over TCP the kernel
            # has no idea which local user (if any) is on the far end.
            raise ValueError("check_peer_uid needs UNIX sockets; it can't work over TCP")
        # Built before listening, so a missing or unreadable key fails here,
        # at startup, instead of on every connection.
        self._tls = _tls_context(MODERATOR_ID, tls_dir, server_side=True)
        # The listener itself is plain TCP: _accept_tcp upgrades each
        # connection to TLS itself (see there for why).
        # reuse_address (SO_REUSEADDR): restarting the moderator right after
        # a game would otherwise fail with "Address already in use" while
        # the old connections sit in TIME_WAIT for a minute.
        listener = await asyncio.start_server(self._accept_tcp, host, port,
                                              reuse_address=True)
        self.port = listener.sockets[0].getsockname()[1]
        self._listeners.append(listener)

    async def _accept_unix(self, player_id, reader, writer) -> None:
        """Runs as its own task for each UNIX connection."""
        conn = Connection(reader, writer)

        # Defence in depth: file permissions decide who can reach the socket,
        # SO_PEERCRED confirms who actually did. This catches e.g. modbot or
        # root (both can get past the file permissions) sitting in a seat.
        expected = self._expected_uid.get(player_id)
        if expected is not None:
            uid = _peer_uid(writer)
            if uid != expected:
                reason = f"uid {uid} may not sit in {player_id}"
                await self._refuse(conn, reason)
                await self._inbox.put((player_id, {"type": "_rejected", "uid": uid,
                                                   "reason": reason}))
                return
        await self._serve_seat(player_id, conn)

    async def _accept_tcp(self, reader, writer) -> None:
        """Runs as its own task for each TCP connection: handshake, then the seat."""
        # Tracked until seated, so close() can cancel a handshake in progress.
        task = asyncio.current_task()
        self._handshaking.add(task)
        try:
            seated = await self._handshake_tcp(reader, writer)
        finally:
            self._handshaking.discard(task)
        if seated is not None:
            await self._serve_seat(*seated, welcome=True)

    async def _handshake_tcp(self, reader, writer) -> tuple[str, Connection] | None:
        """
        TLS handshake, then HELLO. Returns (seat, connection) to seat, or
        None if the connection was turned away (and already closed).

        All seats share one port, so the seat can't come from the socket as
        it does with UNIX. It comes from the player's certificate, which the
        TLS handshake has verified against our CA -- the same kind of
        unforgeable fact the socket path is for UNIX. The HELLO's "player"
        field is only *checked* against it, never trusted on its own.
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
        try:
            # Bounded: a connection that never sends HELLO must not hold a
            # task (and a file descriptor) open forever. Cancelling a recv
            # mid-frame is safe HERE only because we hang up straight after.
            hello = await asyncio.wait_for(conn.recv(), HELLO_TIMEOUT)
        except (TimeoutError, ConnectionError):
            await conn.close()
            return None
        claimed = hello.get("player")
        if hello.get("type") != "HELLO" or not isinstance(claimed, str):
            await self._refuse(conn, "expected HELLO first")
            return None

        if cert_name not in self._seats:
            # Signed by our CA, but not for any seat in this game (an old
            # player name, the moderator's own certificate, ...).
            reason = f"certificate is for {cert_name!r}, which is not a seat in this game"
        elif claimed != cert_name:
            # e.g. player2's certificate trying to sit in player1's seat.
            reason = f"certificate is for {cert_name!r}, not {claimed!r}"
        else:
            return cert_name, conn
        await self._refuse(conn, reason)
        # Not seated, so there's no player id to file it under.
        await self._inbox.put((None, {"type": "_rejected", "reason": reason}))
        return None

    async def _serve_seat(self, player_id: str, conn: Connection, welcome: bool = False) -> None:
        """Seat the connection, then pump its frames into the inbox until it ends."""
        if player_id in self._conns:
            # Only one live connection per seat.
            await self._refuse(conn, "seat already taken")
            await self._inbox.put((player_id, {"type": "_rejected",
                                               "reason": "seat already taken"}))
            return
        # No `await` between the check above and this line, so two
        # connections racing for the same seat can't both pass the check:
        # asyncio only switches tasks at an await.
        self._conns[player_id] = conn

        reason = "closed"
        heartbeat = None
        try:
            if welcome:
                # TCP only: tells the client its HELLO was accepted. Sent
                # before _connected is announced, so WELCOME is always the
                # first frame the client sees.
                await asyncio.wait_for(conn.send({"type": "WELCOME", "player": player_id}),
                                       self.send_timeout)
            # Runs beside this reader loop. If the player goes silent, it
            # aborts the link, conn.recv() below raises with its reason, and
            # the finally block posts _disconnected -- the one path every
            # kind of disconnect takes.
            heartbeat = asyncio.create_task(
                conn.heartbeat(self.heartbeat_interval, self.heartbeat_misses))
            await self._inbox.put((player_id, {"type": "_connected"}))
            if len(self._conns) == len(self._seats):
                self._all_connected.set()
            while True:
                msg = await conn.recv()
                await self._inbox.put((player_id, msg))
        except TimeoutError:
            reason = "WELCOME send timed out"
        except ConnectionClosed as e:
            reason = str(e)
        except Exception as e:
            # Anything else is a bug, but it must still end in _disconnected:
            # otherwise the seat looks connected forever and the game waits
            # on a player nobody is reading from.
            reason = f"reader failed: {e!r}"
        finally:
            # Tell the game logic this player is gone, through the same
            # inbox, so it learns about it in order with everything else.
            # put_nowait: the inbox is unbounded, and a finally block that
            # may be running under cancellation shouldn't await anything
            # it doesn't have to.
            if self._conns.get(player_id) is conn:
                del self._conns[player_id]
            self._inbox.put_nowait((player_id, {"type": "_disconnected", "reason": reason}))
            await conn.close()
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _refuse(self, conn: Connection, text: str) -> None:
        """Best-effort error message, then hang up. Never raises."""
        try:
            await asyncio.wait_for(conn.send({"type": "error", "text": text}), self.send_timeout)
        except (TimeoutError, ConnectionError):
            pass  # the peer is being turned away anyway
        await conn.close()

    async def wait_for_players(self, timeout: float | None = None) -> None:
        """Block until every seat has a connection (or raise TimeoutError)."""
        await asyncio.wait_for(self._all_connected.wait(), timeout)

    def connected(self) -> list[str]:
        return list(self._conns)

    # --- sending: 1:1, multicast, broadcast -------------------------------

    async def send_to(self, player_id: str, msg: dict) -> None:
        """
        1:1 -- send to exactly one player.

        Raises KeyError if the seat is empty, ConnectionError (usually
        ConnectionClosed) if the send fails or the player hasn't made room
        for it within send_timeout seconds.
        """
        conn = self._conns.get(player_id)
        if conn is None:
            raise KeyError(f"{player_id} is not connected")
        try:
            await asyncio.wait_for(conn.send(msg), self.send_timeout)
        except TimeoutError:
            # Cancelling here can't corrupt the stream: Connection.send()
            # hands the whole frame to write() in one synchronous call, so
            # the timeout can only interrupt the drain() *after* it. But the
            # player isn't reading, and every later send would pile up in
            # memory behind this one. Hang up instead: the reader task sees
            # the link drop and posts _disconnected, so the game learns about
            # it the same way as any other disconnect.
            reason = f"{player_id} stopped reading (send timed out after {self.send_timeout}s)"
            conn.abort(reason)
            raise ConnectionClosed(reason) from None

    async def multicast(self, player_ids, msg: dict) -> list[str]:
        """
        Send the same message to a subset of players, concurrently.
        Returns the ids that could NOT be reached (disconnected/broken).
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
        """Send to every connected player."""
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
        Orderly shutdown: stop accepting, hang up on every player, wait for
        each connection's reader task to finish, then remove the socket
        files. Anything already passed to send() is still delivered: closing
        a stream socket (UNIX or TCP) sends EOF/FIN *after* the bytes already
        queued, never instead of them.
        """
        for listener in self._listeners:
            listener.close()                 # no new connections
        await asyncio.gather(*(conn.close() for conn in list(self._conns.values())))
        # Connections still in the TLS/HELLO handshake aren't in _conns yet.
        # Cancel their tasks: asyncio then closes each one cleanly. (Closing
        # their transports from here instead hits a CPython quirk: a clean
        # close mid-TLS-handshake makes start_tls() *return* -- with no
        # transport -- instead of raising.)
        for task in list(self._handshaking):
            task.cancel()
        for listener in self._listeners:
            try:
                # Returns once every connection handler (_serve_seat) has
                # exited. Bounded, so one wedged peer can't hang shutdown.
                await asyncio.wait_for(listener.wait_closed(), 2)
            except TimeoutError:
                pass
        for path in self._paths.values():
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Client: player side of the star
# ---------------------------------------------------------------------------

class Client:
    """
    A player's single connection to the moderator.

    Like the Server, the Client reads the socket in a background task and
    puts complete messages into a queue. This matters for timeouts: if
    recv(timeout=...) cancelled a read *directly*, it might cancel after the
    4-byte header was consumed but before the body arrived -- the header
    would be lost and every later frame would be misread. Cancelling a
    queue.get() is always safe because frames enter the queue whole.
    """

    def __init__(self, heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
                 heartbeat_misses: int = DEFAULT_HEARTBEAT_MISSES):
        _check_heartbeat(heartbeat_interval, heartbeat_misses)
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_misses = heartbeat_misses
        self._conn: Connection | None = None
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._closed_reason = "moderator closed the connection"

    async def _read_loop(self) -> None:
        try:
            while True:
                await self._inbox.put(await self._conn.recv())
        except ConnectionClosed as e:
            self._closed_reason = str(e)
        except Exception as e:
            # A bug or an error we didn't foresee. Record it rather than
            # re-raise: nobody awaits this task, so a raised exception would
            # only surface as a log line at exit.
            self._closed_reason = f"reader failed: {e!r}"
        finally:
            # The sentinel goes in on EVERY exit path -- EOF, garbage frame,
            # unexpected exception, or cancellation by close(). Without it a
            # player blocked in recv() would wait forever on a dead link.
            self._inbox.put_nowait(None)

    async def connect(
        self,
        socket_path: str | None = None,
        retry_seconds: float = 10.0,
        *,
        host: str | None = None,
        port: int | None = None,
        player_id: str | None = None,
        tls_dir: str = DEFAULT_TLS_DIR,
    ) -> None:
        """
        Connect to the moderator: over UNIX at socket_path, or, if port is
        given, over TCP + mutual TLS to host:port as player_id, using that
        player's certificate and the CA from tls_dir.

        Retries briefly because players may be launched a moment before the
        moderator is listening. These are final and NOT retried:
          SeatRefused                 (TCP) the moderator turned us down
          ssl.SSLCertVerificationError  (TCP) the moderator's certificate
                                      isn't from our CA, or doesn't name
                                      `host` -- we may be talking to an
                                      impostor
        """
        if port is None and socket_path is None:
            raise ValueError("need socket_path (UNIX) or port (TCP)")
        if port is not None and not player_id:
            raise ValueError("TCP needs player_id for the HELLO")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + retry_seconds
        while True:
            try:
                if port is not None:
                    self._conn = await self._open_tcp(host or DEFAULT_TCP_HOST, port,
                                                      player_id, tls_dir)
                else:
                    reader, writer = await asyncio.open_unix_connection(socket_path)
                    self._conn = Connection(reader, writer)
                self._reader_task = asyncio.create_task(self._read_loop())
                # The player watches the moderator too: if it goes silent,
                # the link is aborted and recv() raises ConnectionClosed with
                # the reason, instead of the player waiting forever.
                self._heartbeat_task = asyncio.create_task(
                    self._conn.heartbeat(self.heartbeat_interval, self.heartbeat_misses))
                return
            except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
                # Nothing listening (yet), or the connect timed out.
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(0.2)

    async def _open_tcp(self, host: str, port: int, player_id: str, tls_dir: str) -> Connection:
        """TCP connect + TLS handshake, then HELLO -> WELCOME. Returns the link."""
        tls = _tls_context(player_id, tls_dir, server_side=False)
        # server_hostname=host: check_hostname compares the address we
        # dialled against the SANs in the moderator's certificate.
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=tls, server_hostname=host,
                                    ssl_handshake_timeout=CONNECT_TIMEOUT),
            CONNECT_TIMEOUT)
        _tune_tcp(writer)
        conn = Connection(reader, writer)
        try:
            await conn.send({"type": "HELLO", "player": player_id})
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
        return conn

    async def send(self, msg: dict) -> None:
        await self._conn.send(msg)

    async def recv(self, timeout: float | None = None) -> dict:
        """
        Next message from the moderator.
        Raises TimeoutError if `timeout` expires, ConnectionClosed if the
        moderator hung up.
        """
        msg = await asyncio.wait_for(self._inbox.get(), timeout)
        if msg is None:
            self._inbox.put_nowait(None)  # keep later recv() calls failing too
            raise ConnectionClosed(self._closed_reason)
        return msg

    async def close(self) -> None:
        tasks = [t for t in (self._reader_task, self._heartbeat_task) if t is not None]
        for task in tasks:
            task.cancel()
        # Wait for the cancellations to finish so no task is left dangling
        # ("Task was destroyed but it is pending!").
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._conn is not None:
            await self._conn.close()
