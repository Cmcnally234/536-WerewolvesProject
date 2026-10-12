"""
commslib.py -- the ONLY file in this project that touches sockets.

IPC primitive: UNIX domain *stream* sockets, driven by asyncio.

Topology: star. The moderator owns one listening socket per player
(e.g. /run/werewolf/player1/sock). Each player connects only to its own
socket. Because file permissions decide who can open which socket, the
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

Public API:
    Connection  -- one framed, bidirectional link (used by Server and Client)
    Server      -- moderator side: start, send_to, multicast, broadcast, recv_any
    Client      -- player side:    connect, send, recv
"""

import asyncio
import json
import os
import struct

# ">I" = big-endian ("network order") unsigned 32-bit int -> the length prefix.
_HEADER = struct.Struct(">I")

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
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._all_connected = asyncio.Event()

    async def start(self, socket_paths: dict[str, str], mode: int = 0o660) -> None:
        """Create a listening socket for each player_id -> path."""
        self._paths = dict(socket_paths)
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
            # Owner + group may connect; "other" may not. Which group owns
            # the file is decided by the directory (see setup_users.sh).
            os.chmod(path, mode)
            self._listeners.append(listener)

    async def _serve_player(self, player_id, reader, writer) -> None:
        """Runs as its own task for each connected player."""
        conn = Connection(reader, writer)
        if player_id in self._conns:
            # Only one live connection per seat.
            await conn.send({"type": "error", "text": "seat already taken"})
            await conn.close()
            return

        self._conns[player_id] = conn
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
        for listener in self._listeners:
            listener.close()
        for conn in list(self._conns.values()):
            await conn.close()
        for path in self._paths.values():
            if os.path.exists(path):
                os.unlink(path)


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
        if self._conn is not None:
            await self._conn.close()
