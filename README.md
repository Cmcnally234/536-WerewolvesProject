# Werewolf: a multi-process game over UNIX domain sockets (CSE 522 P1)

> ## Communications library: **`commslib.py`**
>
> - **Name:** `commslib.py`
> - **Location:** the project's top-level directory (next to this README). `setup_users.sh` installs it, with the rest of the code, to **`/opt/werewolf/commslib.py`**.
> - **What it is:** the **only** file in the project that touches sockets. `moderator.py` and `player.py` do all of their communication through it.
> - **IPC primitive:** UNIX domain **stream** sockets driven by **asyncio**. Project 2 adds a TCP + mutual TLS transport through the same API (see [DISTRIBUTED.md](DISTRIBUTED.md)).
> - **API:** see [Communications library API](#communications-library-api-commslibpy) below.

A Werewolf / Mafia social deduction game for 4–6 players:
- Every player is a **separate OS process running as a separate Linux user** (`player1` … `player6`).
- A central **moderator** process runs as the user `modbot`.
- Players connect only to the moderator (a star topology). The moderator relays messages one-to-one (1:1), to a group of players (multicast), or to everyone (broadcast).

## Contents

1. [Requirements](#requirements)
2. [Setup](#setup)
3. [Compiling](#compiling)
4. [Playing](#playing)
5. [Communications library API (`commslib.py`)](#communications-library-api-commslibpy)
6. [Files](#files)
7. [Testing](#testing)
8. [Troubleshooting](#troubleshooting)
9. [Uninstalling](#uninstalling)

`ARCHITECTURE.md` explains the design in depth: the topology diagram, framing, permissions, the concurrency design, and the full message table.

---

## Requirements

- **Linux**: UNIX domain sockets, `useradd`, `/run`. Developed and tested on Fedora 44 with SELinux enforcing.
- **Python 3.11 or newer** (on every machine, moderator and players). It was tested on Python 3.14. It needs 3.11 because it relies on `asyncio.wait_for` raising the built-in `TimeoutError`, and on `StreamWriter.start_tls()`.
- **Standard library only.** Nothing to `pip install`.
- **`sudo`**, to create the game's Linux users and its socket directory.
- **Standard system tools:** `useradd`, `groupadd`, `usermod`, `userdel`, `install`, `setsid` and `pkill`. These are present on Fedora, RHEL, Debian and Ubuntu by default.
- **A terminal for each human player.** Bot players need no terminal.

## Setup

1. **Put the project directory anywhere.** Your home directory is fine: the setup script copies the code out to `/opt/werewolf`, because the game users can't read files inside your home directory.

2. **Create the users, install the code, and build the socket directory:**

   ```bash
   cd werewolf-project-dir            # the directory containing this README
   sudo ./setup_users.sh              # 5 player seats (default)
   sudo N_PLAYERS=6 ./setup_users.sh  # or 4, 5 or 6 seats
   ```

   This creates the following. It's safe to re-run: existing users are kept, and permissions are re-applied.

   | Created | Details |
   |---|---|
   | Users `modbot`, `player1` … `playerN` | System accounts with no home directory and a `nologin` shell. They only exist to run game processes via `sudo -u`. If users with these names already exist, they are reused. |
   | Group `werewolf` | `modbot` plus every player. |
   | `/opt/werewolf/` | `root:werewolf`, mode `0750`. A copy of every `*.py` file. |
   | `/run/werewolf/` | `modbot:werewolf`, mode `0750`. |
   | `/run/werewolf/playerN/` | `modbot:playerN`, mode `2750` (setgid). The moderator creates player N's socket here. Only `playerN` (and `modbot`) can reach it. |

3. **Verify that each player can reach only its own socket** (optional, about 10s):

   ```bash
   sudo ./test_permissions.sh         # expect: 8 passed, 0 failed
   ```

> **Re-run `sudo ./setup_users.sh`:**
> - **after every code change.** The game runs the copy in `/opt/werewolf`, and `run_game.sh` refuses to start on a stale copy.
> - **after every reboot.** `/run` is a tmpfs, so `/run/werewolf` disappears at boot.

## Compiling

**No compilation or build step is needed.** Everything is plain Python 3 run directly by the interpreter, plus three bash scripts. There are no third-party dependencies and no virtual environment.

## Playing

### Quick start: one command

```bash
sudo ./run_game.sh                    # 5 seats, all bots: watch a whole game play out
sudo ./run_game.sh -H 1,2             # player1 and player2 are humans; seats 3-5 are bots
```

`run_game.sh`:
- starts the moderator **in the terminal you run it from**, as user `modbot`;
- starts each bot seat in the background, as its own Linux user;
- prints the command for each human seat.

For each human seat, open a **new terminal** and run the printed command:

```bash
sudo -u player1 python3 /opt/werewolf/player.py player1
```

**Keep the moderator's terminal hidden from the players: it prints every role.**

The moderator waits up to 120s for every seat to connect, then the game begins. Ctrl-C in the moderator's terminal ends the game cleanly for everyone. Logs are written to `/tmp/werewolf-run.XXXXXX/`.

| `run_game.sh` option | Meaning |
|---|---|
| `-n 4\|5\|6` | Number of seats (default 5; you need at least this many from `setup_users.sh`) |
| `-H 1,2` | Seats played by humans (default: none) |
| `-S 4` | Seats played by *silent* bots that never respond. Use this to see the moderator's timeouts. |
| `-- ARGS` | Everything after `--` is passed to `moderator.py`, e.g. `-- --night-timeout 20 --discussion 30 --join-timeout 300` |
| `-h` | Show the usage |

### Starting the processes by hand (equivalent to `run_game.sh`)

```bash
sudo -u modbot  python3 /opt/werewolf/moderator.py --peercred player1 player2 player3 player4 player5
sudo -u player1 python3 /opt/werewolf/player.py player1           # human, own terminal
sudo -u player3 python3 /opt/werewolf/player.py player3 --bot     # bot
```

The seat name must match the Linux user. With `--peercred`, the moderator rejects any process whose kernel-reported uid doesn't match its seat.

| `moderator.py` flag | Default | Meaning |
|---|---|---|
| `players...` | (required) | Seat names, 4–6 of them |
| `--dir` | `/run/werewolf` | Socket directory |
| `--port N` | off (UNIX) | Listen on TCP + TLS port N instead of UNIX sockets (`0` = any free port, printed at startup). Can't be combined with `--peercred` or `--local`. |
| `--host` | `127.0.0.1` | TCP address to listen on. Use `0.0.0.0` to accept players from other machines. |
| `--tls DIR` | `/etc/werewolf/tls` | Certificates from `make_certs.sh` (the moderator uses `DIR/ca.crt` and `DIR/moderator/`) |
| `--peercred` | off | Enforce the SO_PEERCRED uid check (seat names must be Linux users) |
| `--seed N` | random | Reproducible role deal |
| `--join-timeout` | 120 | Seconds to wait for all seats to connect |
| `--night-timeout` | 45 | Seconds for all night actions; anyone silent gets a no-op |
| `--discussion` | 60 | Seconds of day chat before voting |
| `--vote-timeout` | 30 | Seconds to vote; anyone silent abstains |
| `--max-rounds` | 20 | The game is a draw after this many night+day rounds |
| `--heartbeat` | 2 | Seconds between the moderator's heartbeats |
| `--heartbeat-misses` | 3 | Drop a player that has been silent for this many of *its* heartbeat intervals |
| `--local` | off | Development only: create the seat directories yourself (see below) |

| `player.py` flag | Meaning |
|---|---|
| `name` | Your seat, e.g. `player1` |
| `--dir` | Socket directory (default `/run/werewolf`) |
| `--port N` | Connect to the moderator over TCP + TLS instead, as seat `name` (your certificate must be for `name`) |
| `--host` | Moderator's address (default `127.0.0.1`). It must be one of the names or IPs in the moderator's certificate (`make_certs.sh -s`). |
| `--tls DIR` | Certificates from `make_certs.sh`: `DIR/ca.crt` and `DIR/<name>/` (default `/etc/werewolf/tls`) |
| `--bot` / `--bot silent` | No keyboard: play randomly, or never respond |
| `--log FILE` | Append every received message to FILE as JSON lines |
| `--wait` | Seconds to keep retrying the connection (default 30) |
| `--heartbeat` / `--heartbeat-misses` | Same as the moderator's, from the player's side: how often to send, and how many of the moderator's intervals to wait before giving up on it (defaults 2 and 3) |

### Trying it without creating users (development mode)

This runs every process as **your own user**, so it does **not** demonstrate the one-user-per-player isolation. It's useful for a quick look:

```bash
python3 moderator.py p1 p2 p3 p4 p5 --dir /tmp/ww --local
python3 player.py p1 --dir /tmp/ww                       # in another terminal (you)
python3 player.py p2 --dir /tmp/ww --bot &               # bots for the other seats
python3 player.py p3 --dir /tmp/ww --bot &
python3 player.py p4 --dir /tmp/ww --bot &
python3 player.py p5 --dir /tmp/ww --bot &
```

The same over TCP + TLS: make a throwaway set of certificates, then give every process `--port` and `--tls` instead of `--dir`.

```bash
./make_certs.sh -o /tmp/pki p1 p2 p3 p4 p5               # not root: nothing is chowned
python3 moderator.py p1 p2 p3 p4 p5 --port 5555 --tls /tmp/pki
python3 player.py p1 --port 5555 --tls /tmp/pki          # in another terminal (you)
for p in p2 p3 p4 p5; do python3 player.py $p --port 5555 --tls /tmp/pki --bot & done
```

For real users, `sudo ./make_certs.sh` (after `setup_users.sh`) writes to `/etc/werewolf/tls`, the default `--tls`, and gives each key to its own Linux user:

```bash
sudo ./make_certs.sh                                       # player1..player5; sudo N_PLAYERS=6 for 6
sudo -u modbot  python3 /opt/werewolf/moderator.py player1 player2 player3 player4 player5 --port 5555
sudo -u player1 python3 /opt/werewolf/player.py player1 --port 5555
```

`make_certs.sh` options: `-o DIR` (output), `-s SAN` (repeatable; the moderator's names and IPs, which replace the default `DNS:localhost`, `IP:127.0.0.1`, `DNS:$(hostname)`), `-f` (new CA), `-U` (don't chown). Re-running keeps the CA. The layout is in [DISTRIBUTED.md](DISTRIBUTED.md#certificates-make_certssh).

### Rules

| | |
|---|---|
| **Roles** | 2 Werewolves, 1 Seer, 1 Doctor; the rest are Villagers. Each player sees only their own role. The two wolves know each other. |
| **Night** | The wolves agree on a victim; they can whisper to each other. The Seer learns whether one player is a werewolf. The Doctor protects one player (themselves allowed), **never the same player two nights in a row**. Anyone who doesn't act before the timer ends takes no action. |
| **Dawn** | The victim dies unless the Doctor protected them. Roles are *not* revealed. |
| **Day** | Living players chat until the discussion timer ends, then vote. **The single most-voted player is eliminated; a tie (or no votes) eliminates nobody.** Votes are public. |
| **Dead players** | Become spectators: they see announcements and vote results, but can't chat, vote or act. |
| **Winning** | The **village** wins when no wolves are left. The **wolves** win when living wolves are at least as many as everyone else. At the end, every role is revealed. |

### Commands (in a player's terminal)

| Command | When | Effect |
|---|---|---|
| `/kill <name or number>` | night, wolves | Vote for the pack's victim (you can change it until both wolves agree) |
| `/peek <name or number>` | night, Seer | Learn whether that player is a werewolf |
| `/heal <name or number>` | night, Doctor | Protect that player tonight |
| `/w <message>` | night, wolves | Whisper to the other wolf |
| *(any text without `/`)* | day | Chat with the other living players |
| `/vote <name or number>` / `/vote none` | voting | Vote to eliminate, or abstain |
| `/role` | any time | Show your role again |
| `/help` | any time | List the commands |
| `/quit` | any time | Leave the game |

Messages from the moderator appear as soon as they arrive, even while you're typing. Your half-typed line is redrawn underneath them.

---

## Communications library API (`commslib.py`)

**File:** `commslib.py`, in the project's top-level directory; installed to `/opt/werewolf/commslib.py`.
**Dependencies:** the Python standard library only (`asyncio`, `socket`, `struct`, `json`, `os`, `pwd`).
**Used by:** `moderator.py` (the `Server` class) and `player.py` (the `Client` class). No other game file opens a socket.

### Transport

| | |
|---|---|
| Primitive | UNIX domain stream sockets (`AF_UNIX`, `SOCK_STREAM`) via `asyncio.start_unix_server` / `asyncio.open_unix_connection`, **or** TCP + mutual TLS 1.3 (`ssl`) via `asyncio.start_server` + `StreamWriter.start_tls` / `asyncio.open_connection(ssl=...)` when a `port` is given |
| Topology | Star. UNIX: the moderator owns **one listening socket per player seat**; each player connects only to its own. TCP: one listening port; a connection's seat is the CN of its verified client certificate, and its `HELLO` frame must name that same seat (handshake in [DISTRIBUTED.md](DISTRIBUTED.md)) |
| Socket path | `<base_dir>/<player_id>/sock`, e.g. `/run/werewolf/player1/sock`; built by `player_socket_path()` |
| Message | A Python `dict`, serialized as JSON |
| Concurrency model | Every call except `connected()` and `player_socket_path()` is a **coroutine** (`await` it inside an asyncio event loop) |

### Wire format (framing)

Each message on the wire is one frame:

```
+---------------------------+-------------------------------------+
| 4 bytes: unsigned length  |  <length> bytes: UTF-8 JSON object  |
| N, big-endian (">I")      |                                     |
+---------------------------+-------------------------------------+
```

- Stream sockets don't preserve message boundaries; the length prefix is what lets the receiver know where each message ends.
- The receiver reads exactly 4 bytes, then exactly N bytes.
- Frames larger than `MAX_MESSAGE_BYTES` (1 MiB) are refused.
- A frame whose body isn't UTF-8 JSON, or is JSON but not an object (e.g. `[1, 2]`), is a `ProtocolError`. The receiver hangs up on that peer.

### Module-level names

| Name | Description |
|---|---|
| `player_socket_path(player_id: str, base_dir: str = "/run/werewolf") -> str` | Returns `<base_dir>/<player_id>/sock`. Both sides use it, so they always agree on the path. |
| `DEFAULT_SOCKET_DIR = "/run/werewolf"` | Default `base_dir` |
| `DEFAULT_TCP_HOST = "127.0.0.1"` | Default TCP `host` for both sides |
| `DEFAULT_TLS_DIR = "/etc/werewolf/tls"`, `MODERATOR_ID = "moderator"` | Default `tls_dir`; the moderator's identity in it |
| `tls_paths(identity, tls_dir=DEFAULT_TLS_DIR) -> (ca, cert, key)` | `<tls_dir>/ca.crt`, `<tls_dir>/<identity>/cert.pem`, `<tls_dir>/<identity>/key.pem` (the layout `make_certs.sh` creates) |
| `HELLO_TIMEOUT = 5.0`, `CONNECT_TIMEOUT = 5.0` | TCP handshake and connect limits |
| `KEEPALIVE_IDLE/INTERVAL/COUNT = 10/5/3`, `TCP_USER_TIMEOUT_MS = 30000` | TCP keepalive settings applied to every TCP link (why: [DISTRIBUTED.md](DISTRIBUTED.md#socket-options-tcp-both-ends)) |
| `MAX_MESSAGE_BYTES = 1048576` | Largest accepted frame body |
| `class ConnectionClosed(ConnectionError)` | The link is gone: the other end hung up, the connection reset, a send timed out, or the peer broke the protocol. `str(e)` says which. |
| `class ProtocolError(ConnectionClosed)` | The peer sent an oversized or malformed frame |
| `class SeatRefused(ConnectionClosed)` | TCP: `Client.connect()` was turned away, by an `error` reply (`str(e)` is the moderator's reason) or by the moderator hanging up during the handshake (it rejected our certificate; its log says why) |
| `DEFAULT_SEND_TIMEOUT = 5.0` | Default for `Server(send_timeout=...)` |
| `CLOSE_TIMEOUT = 2.0` | How long `close()` waits for buffered data to flush before aborting |
| `DEFAULT_HEARTBEAT_INTERVAL = 2.0`, `DEFAULT_HEARTBEAT_MISSES = 3`, `MAX_HEARTBEAT_INTERVAL = 60.0` | Heartbeat defaults, and the largest interval a peer may announce |
| `HEARTBEAT = "HEARTBEAT"` | Reserved message type: commslib sends and consumes these itself, and they're never delivered to the game |

### `class Server`: the moderator's side

Create one with `server = Server(send_timeout=5.0, heartbeat_interval=2.0, heartbeat_misses=3)`. Every seated connection sends a heartbeat every `heartbeat_interval` seconds, and is dropped (`_disconnected`, reason `no heartbeat ...`) if nothing arrives from it for `heartbeat_misses` of the intervals *it* announced. `ValueError` if the interval isn't in (0, 60] or misses < 1. Methods:

| Method | Description |
|---|---|
| `await start(player_ids, base_dir="/run/werewolf", check_peer_uid=False, *, host=None, port=None, tls_dir="/etc/werewolf/tls")` | **With `port`:** one TCP + mutual-TLS listener on `host:port` (`host` defaults to `127.0.0.1`; `port=0` picks a free port, readable afterwards as `server.port`), using `tls_dir`'s CA and moderator certificate. It raises `OSError` if those can't be loaded. A connection is seated by the CN of its client certificate; certificates not signed by the CA, for no seat, or for a different seat than the `HELLO` names are refused. `base_dir` is ignored, and `check_peer_uid=True` raises `ValueError` (SO_PEERCRED is UNIX-only). **Without `port`:** opens a listening socket at `<base_dir>/<id>/sock` for each id and sets it to mode `0660`. A stale socket file left over from a crash is removed first. **The seat directories must already exist** (`setup_users.sh` creates them); raises `FileNotFoundError` if one is missing. With `check_peer_uid=True`, each `player_id` must be a Linux username (`KeyError` otherwise), and connections whose SO_PEERCRED uid doesn't match are rejected. |
| `await wait_for_players(timeout=None)` | Returns once every seat has a connection. Raises `TimeoutError` if `timeout` seconds pass first. |
| `connected() -> list[str]` | The ids currently connected (*not* a coroutine) |
| `await send_to(player_id, msg: dict)` | **1:1**: send to one player. Raises `KeyError` if that player isn't connected. If the player doesn't make room for the message within `send_timeout` seconds (a stopped or wedged process), the server drops that connection, the reader posts `_disconnected`, and this raises `ConnectionClosed`. |
| `await multicast(player_ids, msg: dict) -> list[str]` | **Multicast**: send the same message to a subset of players, **concurrently**. A failed send to one player doesn't stop the others. Returns the ids that could not be reached. |
| `await broadcast(msg: dict) -> list[str]` | **Broadcast**: `multicast` to every connected player. Returns the unreachable ids. |
| `await recv_any(timeout=None) -> (player_id, msg)` | Next message from **any** player, in arrival order. Raises `TimeoutError` if nothing arrives within `timeout` seconds. |
| `await close()` | Stops accepting, closes every connection, waits (2s limit) for the per-connection tasks to finish, and deletes the socket files. Messages already sent are still delivered before the connection closes. |

**Events the Server puts in the `recv_any` stream itself.** They are never sent over the wire, and their `type` starts with `_`:

| `msg["type"]` | Meaning |
|---|---|
| `"_connected"` | That player has taken its seat |
| `"_disconnected"` | That player's connection closed; `msg["reason"]` says why |
| `"_rejected"` | A connection was refused; `msg["reason"]` says why: SO_PEERCRED mismatch (`msg["uid"]` holds the uid), seat already taken (under that seat), or (TCP, under player id `None`) a failed TLS handshake (no certificate, wrong CA, not TLS) or a certificate for no seat / for a different seat than the `HELLO`. |

**What the server sends on its own** (the game never needs to): if a second connection arrives for an occupied seat, or a uid check fails, the server sends that connection `{"type": "error", "text": ...}` and closes it.

**How it works:** each accepted connection gets its own reader task. All the reader tasks put complete messages into one shared `asyncio.Queue`, and `recv_any(timeout)` is `asyncio.wait_for(queue.get(), timeout)`. The moderator therefore never blocks on any single player.

### `class Client`: a player's side

Create one with `client = Client(heartbeat_interval=2.0, heartbeat_misses=3)`. Heartbeats work as for the `Server`: if the moderator goes silent for too long, `recv()` raises `ConnectionClosed("no heartbeat ...")`. Methods:

| Method | Description |
|---|---|
| `await connect(socket_path=None, retry_seconds=10.0, *, host=None, port=None, player_id=None, tls_dir="/etc/werewolf/tls")` | Connect to the moderator: over UNIX at `socket_path`, or, with `port`, over TCP + TLS to `host:port` as `player_id`, presenting `tls_dir/<player_id>/` and trusting only `tls_dir/ca.crt`. Retries every 0.2s while nothing is listening yet (the moderator may still be starting), for up to `retry_seconds`. These are raised immediately: `PermissionError` (UNIX: wrong user for this seat; TCP: can't read our key), `SeatRefused` (TCP: the moderator refused us), `ssl.SSLCertVerificationError` (TCP: **we** refused the moderator: wrong CA, or `host` not in its certificate). |
| `await send(msg: dict)` | Send one message to the moderator |
| `await recv(timeout=None) -> dict` | Next message from the moderator. Raises `TimeoutError` if `timeout` expires, or `ConnectionClosed` if the link is gone for any reason (including a malformed frame). It never hangs on a dead link. |
| `await close()` | Stop the background reader and close the connection |

The Client also reads in a background task that feeds a queue. As a result, a `recv(timeout)` that times out can never cut a frame in half.

### `class Connection`: one framed link (used inside `Server` and `Client`)

| Method | Description |
|---|---|
| `await send(msg: dict)` | Frame and send one message, then wait until the OS has accepted the bytes (`drain()`). Raises `TypeError` if `msg` isn't a dict, and `ValueError` if it's over 1 MiB. A lock stops concurrent sends from interleaving their bytes. |
| `await recv() -> dict` | Return the next frame that isn't a `HEARTBEAT` (those are consumed here; every frame updates `last_heard`). Raises `ConnectionClosed` on EOF or any socket error, and `ProtocolError` on an oversized or malformed frame or a bad heartbeat interval. |
| `abort(reason=None)` | Drop the link immediately, discarding unsent data. `reason` becomes the message of the `ConnectionClosed` that the reader then gets. |
| `await heartbeat(interval, misses)` | Run as a task for the life of the link: send `HEARTBEAT` every `interval` s, and `abort()` if nothing arrives for `misses ×` the peer's interval. `Server` and `Client` start it for you. |
| `await close()` | Close the link, waiting up to `CLOSE_TIMEOUT` for unsent data to flush, then aborting |

### Minimal example

```python
# server side (moderator)
import asyncio
from commslib import Server

async def main():
    server = Server()
    await server.start(["player1", "player2"])            # /run/werewolf/player{1,2}/sock
    await server.wait_for_players(timeout=60)
    await server.send_to("player1", {"type": "hello"})                    # 1:1
    await server.multicast(["player1", "player2"], {"type": "group"})     # multicast
    await server.broadcast({"type": "everyone"})                          # broadcast
    while True:                                                           # from anyone
        pid, msg = await server.recv_any(timeout=10)
        if not msg["type"].startswith("_"):   # skip the server's own _connected/... events
            break
    print(pid, msg)                                                       # player1 {'type': 'reply'}
    await server.close()

asyncio.run(main())
```

```python
# client side (player)
import asyncio
from commslib import Client, player_socket_path

async def main():
    client = Client()
    await client.connect(player_socket_path("player1"))
    print(await client.recv())
    await client.send({"type": "reply"})
    await client.close()

asyncio.run(main())
```

The game's own message types (`ROLE_ASSIGN`, `NIGHT_PROMPT`, `CHAT`, `VOTE_TALLY`, `GAME_OVER`, …) are ordinary dicts sent through this API. `ARCHITECTURE.md` §4 lists each one with its delivery (1:1, multicast or broadcast) and why.

---

## Files

| File | What it is |
|---|---|
| **`commslib.py`** | **The communications library**: the only file that uses sockets (see the API above) |
| `moderator.py` | The moderator / game server. Owns all game state, runs the phases (night, day, vote) with timeouts, decides who receives each message, checks for a winner. Run as `modbot`. |
| `player.py` | The player client. Shows messages from the moderator, reads your commands, and reads the keyboard and the socket at the same time. `--bot` makes it play automatically. Run as `playerN`. |
| `roles.py` | The game rules: role definitions, legal night targets, resolving the night, vote counting, win conditions. No I/O. |
| `setup_users.sh` | **Setup / install.** Creates the users and groups, copies the code to `/opt/werewolf`, builds `/run/werewolf` with per-player permissions. Run with sudo. |
| `run_game.sh` | Starts a game as real users: the moderator, the bots, and printed commands for the human seats. Run with sudo. |
| `teardown_users.sh` | Uninstall: removes the users, groups, `/opt/werewolf`, `/run/werewolf` and `/etc/werewolf`. Run with sudo. |
| `make_certs.sh` | Creates the game CA, the moderator's certificate and one per player (keys 0600, owned by their Linux users when run as root) |
| `test_tls.sh` | Checks who gets a seat over TLS (valid, missing, foreign or stolen certificates; impostor moderators); with sudo, also the key ownership |
| `tls_server.py`, `tls_probe.py` | Helpers used by `test_tls.sh` |
| `test_permissions.sh` | Checks that each player can reach only its own socket, and that SO_PEERCRED rejects impostors. Run with sudo after setup. |
| `perm_server.py`, `perm_probe.py` | Helpers used by `test_permissions.sh` |
| `ping_server.py`, `ping_client.py` | Stand-alone commslib demo: 1:1, multicast, broadcast, a receive timeout, and a 300 KB message |
| `test_no_role_leak.py` | Automated: plays full games and checks that villagers never receive anyone else's role |
| `test_night.py` | Automated: night phase, including the timeout for silent players and the Doctor rule |
| `test_day.py` | Automated: chat relay, voting, ties, spectators |
| `test_game.py` | Automated: whole games, win conditions, clean shutdown on signals, a frozen (`SIGSTOP`) player dropped by heartbeat |
| `test_commslib.py` | Automated: commslib failure paths (malformed frames, a stuck reader, a dead link) on both transports, the TCP/TLS handshake and socket options, and heartbeats (idle link kept, silent peer dropped, black-holed link noticed by both ends) |
| `testnet.py` | Test helper: `WW_TRANSPORT=tcp` makes the process-launching tests use TCP |
| `DISTRIBUTED.md` | Project 2 protocol: transports, certificates, handshake, socket options, failure model |
| `ARCHITECTURE.md` | Design document: diagram, framing, permissions, message table, concurrency |
| `README.md` | This file |
| `CLAUDE.md` | Development notes, used by the AI assistant that helped write the code |

---

## Testing

Run these from the project directory. Each step builds on the one before, so a failure tells you which layer broke.

### 0. Automated tests (no sudo, about 1 minute)

These run every process as your own user, using temporary socket directories.

```bash
python3 test_commslib.py && python3 test_no_role_leak.py && python3 test_night.py && python3 test_day.py && python3 test_game.py
```

All five should end with `PASS` / `all ... tests passed`.

The same four game suites over localhost TCP + TLS, with a throwaway PKI made per run (`test_commslib.py` already covers both):

```bash
export WW_TRANSPORT=tcp
python3 test_no_role_leak.py && python3 test_night.py && python3 test_day.py && python3 test_game.py
unset WW_TRANSPORT
```

The commslib demo on its own (one slow client shows the timeout):

```bash
python3 ping_server.py & python3 ping_client.py p1 & python3 ping_client.py p2 & python3 ping_client.py p3 --delay 3 & wait
```

### 1. Set up users and confirm isolation

```bash
sudo ./setup_users.sh          # creates the users, copies the code to /opt/werewolf, builds /run/werewolf
sudo ./test_permissions.sh     # expect: 8 passed, 0 failed
```

If `test_permissions.sh` fails, fix that first (see [Troubleshooting](#troubleshooting)).

The TLS rules (no sudo needed for the protocol checks; with sudo it also checks that each key belongs to its own user and that the other users can't read it):

```bash
./test_tls.sh                  # expect: 14 passed, 0 failed
sudo ./test_tls.sh             # expect: 21 passed, 0 failed (needs setup_users.sh)
```

### 2. Bot game as real Linux users

```bash
sudo ./run_game.sh -- --night-timeout 10 --discussion 5 --vote-timeout 10
```

**What to check:**

- The moderator's output shows nights, chat, votes, then `GAME OVER: ...` and `all sockets closed`.
- The summary shows every player exiting with status `0` and `socket files left behind: none`.
- **See the game from one player's side:** `cat /tmp/werewolf-run.*/player1.out`
- **Each process runs as its own user** (run this in another terminal during a game): `ps -o user,pid,cmd -C python3`
- **Timeout path:** add `-S 4` to make player4 silent. Each night then waits out its timer, and player4 is told "Time's up".

### 3. Manual game: two humans plus three bots

```bash
sudo ./run_game.sh -H 1,2 -- --join-timeout 300
```

Then open two more terminals and run the commands it prints:

```bash
sudo -u player1 python3 /opt/werewolf/player.py player1
sudo -u player2 python3 /opt/werewolf/player.py player2
```

**Things to try:**

| Try this | What should happen |
|---|---|
| Start typing, then wait for a message to arrive | Your half-typed line is redrawn under the message |
| Don't act during a night | After the night timer you get "Time's up"; the game continues |
| Use the wrong command or an invalid target | You get a clear error; nothing breaks |
| Chat at night, or vote during the discussion | Rejected with an explanation |
| Be killed or voted out | You switch to spectating: you still see announcements, but not the chat |
| End of the game | Roles revealed, the player exits, and your terminal echoes normally again |

### 4. Breakage tests (optional)

- **Kill a player mid-game:** the moderator logs `player3 disconnected`, and the game continues.

  ```bash
  sudo pkill -TERM -u player3 -f player.py
  ```
- **Kill the moderator mid-game:** press Ctrl-C in its terminal. Every player should get "Game over, no winner: the moderator was shut down." and exit.
- **Connect to someone else's seat:**

  ```bash
  sudo -u player2 python3 /opt/werewolf/player.py player1
  ```

  This fails with `Permission denied`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `could not connect` / `Permission denied` on **your own** seat | Re-run `sudo ./setup_users.sh` (needed after a reboot or a code change). Make sure the `sudo -u` user matches the seat name. |
| `run_game.sh`: `/opt/werewolf is out of date` | You edited code; re-run `sudo ./setup_users.sh` |
| `run_game.sh`: `/run/werewolf/playerN missing` | You rebooted, or set up fewer seats than `-n`. Run `sudo N_PLAYERS=<n> ./setup_users.sh` |
| The moderator logs `rejected a connection for playerN: uid ... may not sit in playerN` | A player was started as the wrong user (e.g. `sudo -u player2 ... player1`) |
| Player: `the moderator hung up before WELCOME, most likely because it rejected our certificate` | Read the moderator's log for the reason: `PEER_DID_NOT_RETURN_A_CERTIFICATE`, `CERTIFICATE_VERIFY_FAILED` (certificate from another CA: re-run `make_certs.sh` with the same `-o` and copy the new files), or `certificate is for ...` |
| Player: `certificate verify failed: IP address mismatch` / `Hostname mismatch` | `--host` isn't in the moderator's certificate. Reissue with `sudo ./make_certs.sh -s DNS:<name> -s IP:<addr> ...` |
| `can't load TLS files ... Permission denied` | Run the player as the Linux user that owns `DIR/<name>/key.pem`, or run `make_certs.sh` without `-U` as root |
| `AF_UNIX path too long` in development mode | Use a short `--dir` such as `/tmp/ww`: Linux limits socket paths to 108 bytes |
| Terminal left without echo after a crash | Type `reset` and press Enter (you won't see it as you type) |
| Blocked only on an SELinux system | Check `sudo ausearch -m AVC,USER_AVC -ts recent` and `ls -Z /opt/werewolf /run/werewolf`; `sudo restorecon -Rv /opt/werewolf /run/werewolf` restores the default labels. Processes started with `sudo -u` from a shell normally run unconfined, so no denials are expected. |

## Uninstalling

```bash
sudo ./teardown_users.sh                 # or: sudo N_PLAYERS=6 ./teardown_users.sh if you set up 6 seats
```

This stops any running game processes, then removes the `modbot` and `player1…N` users, the `werewolf` group, `/opt/werewolf`, `/run/werewolf` and the certificates in `/etc/werewolf`.
