"""
commslib.py -- the ONLY file in this project that touches sockets.

IPC primitive: UNIX domain *stream* sockets, driven by asyncio.

Topology: star. The moderator owns one listening socket per player
(/run/werewolf/<player_id>/sock, see player_socket_path()). Each player
connects only to its own socket. Because file permissions decide who can open which socket, the
moderator knows *who* is on a connection by *which socket it arrived on* --
a player cannot impersonate another player by lying in a message.

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
    _disconnected  a player's connection closed
    _rejected      SO_PEERCRED uid check failed (check_peer_uid=True)

Public API:
    player_socket_path(player_id, base_dir) -> the socket path for a seat
    Connection  -- one framed, bidirectional link (used by Server and Client)
    Server      -- moderator side: start, send_to, multicast, broadcast, recv_any
    Client      -- player side:    connect, send, recv
"""

import asyncio
import json
import os
import pwd
import socket
import struct

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


class ConnectionClosed(Exception):
    """Raised by recv() when the other side has hung up."""


# ---------------------------------------------------------------------------
# Connection: framing on top of an asyncio (reader, writer) pair
# ---------------------------------------------------------------------------

class Connection:
    """A single framed link. Wraps asyncio's StreamReader/StreamWriter."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        # Two different tasks could call send() at the same time (e.g. a
        # broadcast and a private reply). Each send is two logical pieces
        # (header + body); the lock guarantees they are never interleaved
        # with another message's bytes on the wire.
        self._send_lock = asyncio.Lock()

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
        try:
            # readexactly() keeps reading until it has exactly n bytes, which
            # is precisely what framing needs: first the 4-byte header, then
            # exactly `length` bytes of body -- no more, no less.
            header = await self._reader.readexactly(_HEADER.size)
            (length,) = _HEADER.unpack(header)
            if length > MAX_MESSAGE_BYTES:
                raise ConnectionClosed(f"peer sent oversized frame ({length} bytes)")
            body = await self._reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionResetError) as e:
            # IncompleteReadError = EOF arrived mid-message (or before one).
            raise ConnectionClosed("peer closed the connection") from e
        return json.loads(body.decode("utf-8"))

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass  # already gone; nothing to clean up


# ---------------------------------------------------------------------------
# Server: moderator side of the star
# ---------------------------------------------------------------------------

class Server:
    """
    Listens on one UNIX socket per player and relays messages.

    Concurrency model: every accepted connection gets its own reader task
    (_serve_player). Each reader task loops forever pulling frames off its
    socket and dropping them, tagged with the player id, into ONE shared
    asyncio.Queue (the "inbox"). The moderator's game logic then only has
    to await recv_any() -- it never blocks on any single player.
    """

    def __init__(self):
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
    ) -> None:
        """
        Create a listening socket at <base_dir>/<player_id>/sock for each
        player.

        The per-player directories must already exist. In the real setup,
        setup_users.sh creates them owned modbot:playerN, mode 2750. We do
        NOT create them here: a directory made by the moderator would have
        the moderator's group, and the isolation would silently disappear.

        check_peer_uid=True additionally requires each player_id to be a
        Linux username and rejects any connection whose kernel-reported uid
        (SO_PEERCRED) isn't that user's.
        """
        for player_id in player_ids:
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
                await self._serve_player(player_id, reader, writer)

            listener = await asyncio.start_unix_server(on_connect, path=path)
            # rw for owner (modbot) and group (playerN, inherited from the
            # setgid directory), nothing for others. connect() on a UNIX
            # socket requires WRITE permission on the socket file.
            # (Between bind() and this chmod the file briefly has umask
            # permissions, but the 2750 directory already keeps everyone
            # except modbot and playerN out, so that window is harmless.)
            os.chmod(path, 0o660)
            self._listeners.append(listener)

    async def _serve_player(self, player_id, reader, writer) -> None:
        """Runs as its own task for each connected player."""
        conn = Connection(reader, writer)

        # Defence in depth: file permissions decide who can reach the socket,
        # SO_PEERCRED confirms who actually did. This catches e.g. modbot or
        # root (both can get past the file permissions) sitting in a seat.
        expected = self._expected_uid.get(player_id)
        if expected is not None:
            uid = _peer_uid(writer)
            if uid != expected:
                await conn.send({"type": "error", "text": f"uid {uid} may not sit in {player_id}"})
                await conn.close()
                await self._inbox.put((player_id, {"type": "_rejected", "uid": uid}))
                return

        if player_id in self._conns:
            # Only one live connection per seat.
            await conn.send({"type": "error", "text": "seat already taken"})
            await conn.close()
            return

        self._conns[player_id] = conn
        await self._inbox.put((player_id, {"type": "_connected"}))
        if len(self._conns) == len(self._paths):
            self._all_connected.set()

        try:
            while True:
                msg = await conn.recv()
                await self._inbox.put((player_id, msg))
        except ConnectionClosed:
            pass
        finally:
            # Tell the game logic this player is gone, through the same
            # inbox, so it learns about it in order with everything else.
            self._conns.pop(player_id, None)
            await self._inbox.put((player_id, {"type": "_disconnected"}))
            await conn.close()

    async def wait_for_players(self, timeout: float | None = None) -> None:
        """Block until every seat has a connection (or raise TimeoutError)."""
        await asyncio.wait_for(self._all_connected.wait(), timeout)

    def connected(self) -> list[str]:
        return list(self._conns)

    # --- sending: 1:1, multicast, broadcast -------------------------------

    async def send_to(self, player_id: str, msg: dict) -> None:
        """1:1 -- send to exactly one player."""
        conn = self._conns.get(player_id)
        if conn is None:
            raise KeyError(f"{player_id} is not connected")
        await conn.send(msg)

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
        a UNIX stream socket sends EOF *after* the bytes already queued in
        the kernel, never instead of them.
        """
        for listener in self._listeners:
            listener.close()                 # no new connections
        await asyncio.gather(*(conn.close() for conn in list(self._conns.values())))
        for listener in self._listeners:
            try:
                # Returns once every connection handler (_serve_player) has
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

    def __init__(self):
        self._conn: Connection | None = None
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None

    async def _read_loop(self) -> None:
        try:
            while True:
                await self._inbox.put(await self._conn.recv())
        except ConnectionClosed:
            # Sentinel so a waiting recv() wakes up instead of hanging.
            await self._inbox.put(None)

    async def connect(self, socket_path: str, retry_seconds: float = 10.0) -> None:
        """
        Connect to the moderator. Retries briefly because players may be
        launched a moment before the moderator has created the socket.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + retry_seconds
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                self._conn = Connection(reader, writer)
                self._reader_task = asyncio.create_task(self._read_loop())
                return
            except (FileNotFoundError, ConnectionRefusedError):
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(0.2)

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
            raise ConnectionClosed("moderator closed the connection")
        return msg

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            # Wait for the cancellation to finish so the task isn't left
            # dangling ("Task was destroyed but it is pending!").
            await asyncio.gather(self._reader_task, return_exceptions=True)
        if self._conn is not None:
            await self._conn.close()
