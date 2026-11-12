# Architecture

> **This document describes Project 1** (git tag `project1`): UNIX domain sockets on one machine. Project 2 added TCP + mutual TLS, heartbeats and resumable sessions; that design is in [DISTRIBUTED.md](DISTRIBUTED.md). Some internal names used below have changed since (`_serve_player` is now `_serve_link`, the client's `_read_loop` is now `_run`, and the send lock was replaced by synchronous `Connection.write()`).

A Werewolf / Mafia game in which **every player is a separate OS process running as a separate Linux user**. All communication goes through one file, `commslib.py`, which wraps **UNIX domain stream sockets driven by asyncio**. Python 3.14, standard library only.

## 1. Processes, users and the star topology

```
                         /run/werewolf/                (modbot:werewolf 0750)
                         ├── player1/sock   ◄──────────── player1 process  (Linux user player1)
                         ├── player2/sock   ◄──────────── player2 process  (Linux user player2)
  moderator process  ────┼── player3/sock   ◄──────────── player3 process  (Linux user player3)
  (Linux user modbot)    ├── player4/sock   ◄──────────── player4 process  (Linux user player4)
  listens on all N       └── player5/sock   ◄──────────── player5 process  (Linux user player5)

  Each playerN/ directory: modbot:playerN, mode 2750 (setgid).  Each sock: mode 0660.
  Players never connect to each other. Every message goes through the moderator.
```

- **Star topology.** The moderator is the hub. It is the only process that knows every role, and it decides who receives each message. Because every message passes through it, the hub can deliver to one player (1:1), to a chosen subset (multicast), or to everyone (broadcast).
- **One listening socket per seat**, rather than one shared socket. The moderator knows *who* a connection belongs to from *which socket it arrived on*. File permissions ensure only `playerN` can open `playerN/sock`, so a player cannot claim to be someone else by lying in a message.
- **Code location.** The code is installed to `/opt/werewolf` (`root:werewolf 0750`) by `setup_users.sh`, because the developer's home directory is mode 700 and the game users couldn't read it there.
- **Launching.** `run_game.sh` starts the moderator with `sudo -u modbot` and each bot seat with `sudo -u playerN`; human seats are started the same way, one terminal each.

## 2. `commslib.py`, the single communication file

`moderator.py` and `player.py` import only `Server`, `Client`, `ConnectionClosed` and `player_socket_path` from it. Neither touches a socket directly.

| API | Side | What it does |
|---|---|---|
| `Connection.send(msg)` / `recv()` / `close()` | both | One framed, bidirectional link over an asyncio `StreamReader`/`StreamWriter` pair |
| `Server.start(player_ids, base_dir, check_peer_uid)` | moderator | `asyncio.start_unix_server` on `<base_dir>/<id>/sock` for each seat; `chmod 0660` |
| `Server.send_to(id, msg)` | moderator | 1:1 |
| `Server.multicast(ids, msg)` | moderator | Sends to a subset concurrently (`asyncio.gather`); returns the ids it couldn't reach |
| `Server.broadcast(msg)` | moderator | `multicast` to every connected seat |
| `Server.recv_any(timeout)` | moderator | Next `(player_id, msg)` from **any** player; raises `TimeoutError` |
| `Server.wait_for_players(timeout)` | moderator | Waits until every seat is connected |
| `Client.connect(path)` / `send(msg)` / `recv(timeout)` / `close()` | player | `asyncio.open_unix_connection`, with a short retry while the moderator starts up |

### 2.1 Framing protocol

```
+----------------------+--------------------------------+
| 4 bytes, big-endian  |  N bytes: UTF-8 JSON object    |
| unsigned length N    |  (one message = one dict)      |
+----------------------+--------------------------------+
```

**Why a stream socket needs framing:** `SOCK_STREAM` delivers a byte stream with **no message boundaries**.
- Two quick sends can arrive in one read.
- A large send can arrive split across several reads. The test suite sends a 300 KB frame to force this.

The length prefix tells the receiver exactly where each message ends. `recv()` calls `readexactly(4)` for the header, then `readexactly(N)` for the body. `readexactly` keeps reading until it has exactly that many bytes, which is exactly what framing needs. Frames over 1 MiB are refused, so a corrupted header can't make the receiver try to buffer gigabytes.

Each `Connection` has a send lock. Two tasks can send on the same connection at the same time (for example, a broadcast and a private reply), and the lock keeps their header and body bytes from interleaving on the wire.

### 2.2 Reader tasks feeding a single inbox

**Server.** Every accepted connection gets its own task, `_serve_player`. Each task loops: `recv()` one frame, then put `(player_id, msg)` into **one shared `asyncio.Queue`**. `recv_any(timeout)` is simply `asyncio.wait_for(inbox.get(), timeout)`. Connection lifecycle events are delivered through the same queue, in order with everything else:
- `_connected`: a player took its seat
- `_disconnected`: a player's connection closed
- `_rejected`: an SO_PEERCRED uid mismatch (section 3)

**Client.** The client uses the same pattern: a background `_read_loop` task fills a queue, and `recv(timeout)` waits on the queue. This matters for timeouts. If `recv(timeout)` cancelled a socket read directly, the cancel could land after the 4-byte header had been consumed but before the body arrived. The header would be lost and every later frame misread. Cancelling a `queue.get()` is always safe, because frames only enter the queue whole.

### 2.3 Shutdown

`Server.close()` works in this order:
1. Stops accepting new connections.
2. Closes every connection.
3. Waits (2s limit per listener) for every `_serve_player` task to exit.
4. Deletes the socket files.

Data already passed to `send()` is still delivered. `drain()` has handed it to the kernel, and closing a UNIX stream socket delivers the queued bytes **before** the EOF.

## 3. Socket isolation and permissions

| Path | Owner:group | Mode | Effect |
|---|---|---|---|
| `/run/werewolf/` | `modbot:werewolf` | `0750` | Game members may enter; outsiders can't even list it |
| `/run/werewolf/playerN/` | `modbot:playerN` | `2750` | modbot creates the socket; only group `playerN` may enter |
| `/run/werewolf/playerN/sock` | `modbot:playerN` | `0660` | `connect()` needs **write** permission on the socket file: modbot and playerN only |

**What the permission bits mean here:**
- On a directory, **x** lets you pass through it and **r** lets you list it.
- Reaching a socket needs x on every directory along its path.
- Group `player2` has no rights on `player1/`, so player2's `connect()` fails with `EACCES` before it ever reaches the socket.

**Why setgid on the seat directory:**
- Normally a new file gets the creating process's *primary group*, so the moderator's sockets would be `modbot:modbot`, and playerN would fall into "other" with no access.
- With the setgid bit on a directory, files created inside it inherit the *directory's* group instead. `setup_users.sh`, running as root, decides once that `player1/` has group `player1`, and every socket the moderator later creates there is automatically `modbot:player1`.
- As a result, modbot doesn't need to be a member of any player group.
- `Server.start()` refuses to create missing seat directories, because a directory made by modbot would have the wrong group and the isolation would silently disappear.

**SO_PEERCRED (defence in depth, `--peercred`).** File permissions decide who *can* reach a socket. SO_PEERCRED tells the moderator who actually *did*. The kernel records the connecting process's `(pid, uid, gid)` at `connect()` time, and the client cannot forge it. `_serve_player` compares the uid with `pwd.getpwnam(player_id)` and rejects a mismatch. This catches modbot (which owns every seat directory) and root (which ignores permissions); neither would be stopped by the permission bits.

`test_permissions.sh` checks all of this as real users:
- player1 and player2 can each connect to their own socket;
- neither can connect to the other's socket;
- `nobody` can't connect to any socket, or even list `/run/werewolf`;
- modbot reaches player1's socket but is rejected by SO_PEERCRED.

Two operational notes:
- **`sun_path` limit:** Linux caps a UNIX socket path at **108 bytes** (`sun_path` in `struct sockaddr_un`). `/run/werewolf/playerN/sock` is well under that.
- **`/run` is a tmpfs:** it's emptied at every boot, so `setup_users.sh` must be re-run after a reboot.

## 4. Message catalogue: 1:1, multicast and broadcast

| Type | Direction | Delivery | Why this delivery |
|---|---|---|---|
| `ROLE_ASSIGN` | mod → player | **1:1** | Each player may learn only their own role. Wolves also get a `teammates` field. |
| `NIGHT_PROMPT` | mod → acting roles | **multicast** to wolves; **1:1** to Seer and Doctor | The wolves act as a team and see who their partner is (`pack`). The Seer's and Doctor's prompts are private. |
| `NIGHT_ACTION` | player → mod | **1:1** | Kill, peek or heal target |
| `PACK_VOTES` | mod → wolves | **multicast** | Current kill choices, so the wolves can coordinate |
| `WEREWOLF_CHAT` | wolf → mod → wolves | **multicast** relay | Private wolf channel, at night only |
| `SEER_RESULT` | mod → seer | **1:1** | The Seer's vision is secret |
| `PHASE` | mod → all | **broadcast** | "Night 2 falls" / "Day 2" / "Voting open". Public, and contains no secrets. |
| `DAY_ANNOUNCE` | mod → all | **broadcast** | Who died overnight: names only, never roles |
| `CHAT` | player → mod → living | **multicast** relay | Public discussion among living players. Dead players are spectators. |
| `VOTE` | player → mod | **1:1** | A vote (`null` = abstain) |
| `VOTE_TALLY` | mod → all | **broadcast** | Every vote and the result. Votes are public, as in the tabletop game. |
| `GAME_OVER` | mod → all | **broadcast** | Winner, and **every** role. The only message that contains roles. |
| `INFO` / `error` | mod → player(s) | **1:1** (multicast for "the pack agrees") | Acknowledgements, "time's up", rejected actions |

`PHASE`, `PACK_VOTES` and `INFO` were added to the original message list:
- `PHASE` is how villagers, who get no night prompt, know a phase started.
- `PACK_VOTES` gives the wolves something concrete to coordinate on.
- `INFO` carries the moderator's notices.

**Roles never leak.** `test_no_role_leak.py` plays full games and checks every message each villager received. Apart from their own `ROLE_ASSIGN` and the final `GAME_OVER`, nothing may contain a role name or a wolf-only or seer-only message type. The test first proves its checker catches three fabricated leaks, so it cannot pass by accident.

## 5. Concurrency

There are two concurrency points required by the assignment: the moderator and each player.

### 5.1 Moderator: all players at once, with timeouts

The moderator is a single-threaded asyncio program. Three mechanisms work together.

**(a) One reader task per connection** (commslib `_serve_player`, section 2.2). With five players, five tasks are waiting on five sockets at the same time. The game logic never reads a socket itself; it waits on the shared inbox.

**(b) One deadline-bounded loop per phase:** `Game.message_loop(seconds, handle, is_done)`.

```python
deadline = loop.time() + seconds
while not is_done():
    remaining = deadline - loop.time()
    if remaining <= 0:
        return False                      # deadline ended the phase
    try:
        pid, msg = await server.recv_any(timeout=remaining)
    except TimeoutError:
        return False
    if not await self.handle_common(pid, msg):
        await handle(pid, msg)
return True                               # finished early
```

- **One deadline for the whole phase.** Each wait gets only the time *left*. If it reset on every message, a chatty player could stretch a phase forever.
- **Whoever speaks next is handled next.** `recv_any` returns the next message from any player, so the Seer, the Doctor and both wolves can all answer at once, and each message is validated, relayed or recorded as it arrives.
- **The same loop drives every timed phase.** Only the end condition differs:

| Phase | `is_done` | Ends when | Silent players |
|---|---|---|---|
| Night (`--night-timeout`, 45s) | nobody pending | everyone acted, **or** the deadline | get a **no-op** plus an `INFO` "time's up"; the game continues |
| Discussion (`--discussion`, 60s) | never | always the full timer; chat relayed live throughout | n/a |
| Vote (`--vote-timeout`, 30s) | nobody pending | everyone voted, **or** the deadline | count as **abstaining** |

The discussion timer is not a separate task or a `sleep()`. It is the `timeout=remaining` on each `recv_any`. A `sleep(60)` would let the timer run, but nobody would be reading messages during it, so chat would pile up and be relayed all at once at the end.

**(c) Concurrent sends with `asyncio.gather`.** `ROLE_ASSIGN` to every player, the night prompts, and every `multicast` all run their sends at the same time. Each send awaits `drain()`, which pauses while that player's socket buffer is full; sending one at a time would let one slow reader delay everyone behind it. `multicast` uses `return_exceptions=True`, so one disconnected player can't cancel delivery to the others.

### 5.2 Player: keyboard and socket at the same time

`player.py` runs two asyncio tasks:

```
socket task:    msg = await client.recv()        -> print it immediately
keyboard task:  line = await terminal.readline() -> send an action / chat / vote
```

```python
done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
for t in pending:
    t.cancel()
```

- **Neither task waits for the other.** A `NIGHT_PROMPT` or another player's chat appears while you're halfway through typing.
- **`asyncio.wait(FIRST_COMPLETED)` rather than `gather`.** `gather` waits for *both* tasks. When the moderator sends `GAME_OVER` or hangs up, the socket task ends, but the keyboard task would wait for a line forever. `FIRST_COMPLETED` returns as soon as either one ends (the game finishing, or `/quit`), then cancels the other.
- **Keyboard input uses no threads.** `Terminal` calls `loop.add_reader(stdin_fd, ...)`, which registers stdin with the same epoll-based event loop that watches the socket. A thread blocked in `input()` couldn't be cancelled, so the player couldn't exit at game end until Enter was pressed.
- **Half-typed lines survive.** The terminal is put in **cbreak mode**: keystrokes arrive one at a time, without echo, and `Terminal` keeps the line buffer itself. Before printing an incoming message it clears the input line (`\r\x1b[K`), prints the message, then redraws the prompt and your partial input. In the terminal's normal mode the kernel holds the half-typed line, where the program can't read or redraw it.
- **Bots** (`--bot`) replace the keyboard task. Each reaction runs as its own task, so a bot's simulated thinking delay never stops the socket from being read.

### 5.3 What breaks with blocking I/O

| Blocking design | Failure |
|---|---|
| Moderator reads players in turn (`recv(p1)`, `recv(p2)`, ...) | A quiet or absent player blocks the whole game. Wolves' coordination and chat sit unread behind them. |
| Blocking reads without a timeout | One hung player (still connected, never sending) stalls the game forever. |
| Discussion timer as `time.sleep()` | Chat freezes for the whole discussion. The event loop is blocked too, so commslib's reader tasks can't drain sockets, buffers fill, and players' sends stall. |
| Player uses `input()` | Moderator messages (including the 45s `NIGHT_PROMPT`) stay unread until Enter is pressed, so a player can miss their turn without ever seeing the prompt. After `GAME_OVER` the process can't exit until Enter. |
| Player does a blocking socket `recv()` | Typed commands aren't processed until the moderator happens to send something. |

### 5.4 Signals and clean shutdown

- **Moderator.** The lobby and game run as one task, `session()`. `loop.add_signal_handler(SIGINT/SIGTERM, task.cancel)` turns Ctrl-C or `kill` into a `CancelledError` at whatever `await` the game is paused on, usually `recv_any`, so game state is never half-updated. `main()` then broadcasts an aborted `GAME_OVER` (3s limit) and calls `Server.close()`. Without this, SIGTERM would end the process immediately, with no `GAME_OVER` and with socket files left behind.
- **Player.** It exits when `GAME_OVER` arrives. SIGTERM/SIGHUP cancel its tasks, and a `finally` block restores the terminal mode and closes the connection.
- **`run_game.sh`.** It starts bots under `setsid`, so a Ctrl-C in the moderator's terminal reaches only the moderator. The bots learn of the shutdown the proper way, through `GAME_OVER`.

## 6. Game flow and rules

```
 lobby ──► ROLE_ASSIGN (1:1 each) ──► ┌─► NIGHT: NIGHT_PROMPT ─► collect actions ─► DAY_ANNOUNCE ─► win? ──yes──► GAME_OVER
 (wait_for_players)                   │        (multicast/1:1)   (message_loop, deadline)         │no
                                      │                                                           ▼
                                      └── win? ◄── VOTE_TALLY ◄── VOTE: collect votes ◄── DAY: discussion (CHAT relay)
                                          │yes     (broadcast)     (ends early)              (full timer)
                                          ▼
                                       GAME_OVER (broadcast, reveals all roles)
```

The rules live in `roles.py`, which does no I/O, separately from the state machine in `moderator.py`.

| Rule | Implementation |
|---|---|
| 4–6 players: 2 Werewolves, 1 Seer, 1 Doctor, the rest Villagers | `deal_roles()`; `--seed` makes the deal reproducible |
| Wolves can't target wolves; the Seer can't peek at themselves | `Werewolf.valid_targets`, `Seer.valid_targets` |
| **Doctor may not protect the same player two nights running** | `Doctor.valid_targets` excludes `last_healed`. A Doctor who didn't act leaves no restriction. Self-protection is allowed. |
| Wolves must agree | Votes may change until all living wolves pick the same target (`pack_agrees`); agreement ends the wolves' part of the night. |
| **Wolf disagreement at the deadline** | `pack_decision()`: the most-voted target, with ties broken by `rng.choice`. No wolf votes means no kill. |
| Seer and Doctor | The first valid choice is final. The Seer gets `SEER_RESULT` immediately. |
| Night result | The wolves' victim dies unless the Doctor protected them (`resolve_night`). Roles are not revealed on death. |
| **Day vote** | The most votes is eliminated (a plurality). **A tie, or no votes, eliminates nobody** (`tally_votes`). Votes are final, self-votes are rejected, silence counts as abstaining. |
| Spectators | Dead players receive broadcasts (`PHASE`, `DAY_ANNOUNCE`, `VOTE_TALLY`, `GAME_OVER`) but not `CHAT`. Anything they send gets an error. |
| **Win check** after every night and every day | The village wins when no wolves are alive. The wolves win when living wolves ≥ everyone else (`check_winner`). `--max-rounds` (20) or everyone disconnecting ends the game as a draw. |

## 7. Module boundaries

| File | Knows about | Doesn't know about |
|---|---|---|
| `commslib.py` | sockets, framing, asyncio streams, SO_PEERCRED | the game |
| `roles.py` | roles and rules | networking, timing |
| `moderator.py` | game state, phases, routing decisions | raw sockets (uses only `Server`) |
| `player.py` | display, keyboard, bot moves | raw sockets (uses only `Client`), other players' roles |

`test_day.py` runs the real `Game` class against a `FakeServer`. The fake has the same methods as `commslib.Server` but no sockets. This shows the game logic depends only on commslib's interface, not on how it is implemented.

## 8. Testing

| Test | What it shows |
|---|---|
| `ping_server.py` / `ping_client.py` | commslib alone: 1:1, multicast, broadcast, a `recv_any` timeout, and a 300 KB frame arriving intact |
| `test_permissions.sh` (sudo) | Socket isolation between real users, and SO_PEERCRED rejection |
| `test_no_role_leak.py` | No role information ever reaches a villager, across full 6-player games |
| `test_night.py` | A night ends early when everyone acts; a silent Seer gets a no-op at the deadline without affecting the others; the Doctor no-repeat rule |
| `test_day.py` | Majority vote, ties, abstaining at the vote deadline, spectators' restrictions, chat relayed *during* the discussion timer |
| `test_game.py` | Full games reach a winner consistent with `check_winner`; everyone gets `GAME_OVER` and exits 0; no socket files are left; SIGTERM/SIGINT to the moderator and SIGTERM to a player mid-game all shut down cleanly |
| `README.md` § Testing | Manual procedure: a bot game and a multi-terminal human game as real Linux users, plus breakage tests |

## 9. Known limitations

- **Disconnected players stay "alive"** for win checks and voting. There is no reconnect support.
- **The terminal input has no line editing or history.** Arrow keys are ignored.
- **The moderator's console prints every role** (the "god view"). It must not be shown to players.
- **A Ctrl-C during the moderator's first moment of startup**, before its signal handler is installed, ends it with a plain `KeyboardInterrupt`. `run_game.sh` then stops the waiting bots after 10s.
- **Bots play randomly.** They exist to exercise the protocol, not to play well.
