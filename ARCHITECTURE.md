# Architecture

A Werewolf / Mafia game in which **every player is a separate OS process running as a separate Linux user**, on one machine or several. All communication goes through one file, `commslib.py`: **TCP stream sockets with mutual TLS 1.3, driven by asyncio**. Python 3.14, standard library only. (Project 1 used UNIX domain sockets on one machine; it is the git tag `project1`.) This document is the overview; `DISTRIBUTED.md` has the wire protocol in full detail.

## 1. Processes, users and the star topology

```
          <MODERATOR_HOST>                              <PLAYER_HOST_A>
  +----------------------------------+   TCP + TLS   +-----------------------------+
  | moderator.py   (Linux user       |<=============>| player.py  (Linux user      |
  |   modbot), listens on :5555      |<=============>| player.py   player3/player4)|
  | player.py (player1) --TLS--+     |               +-----------------------------+
  | player.py (player2) --TLS--+     |   TCP + TLS   +-----------------------------+
  +----------------------------------+<=============>| player.py  (player5)        |
                                                     +-----------------------------+
  Every player connects only to the moderator (also the ones on its own machine,
  over 127.0.0.1). Players never connect to each other.
```

- **Star topology.** The moderator is the hub. It is the only process that knows every role, and it decides who receives each message. Because every message passes through it, it can deliver to one player (1:1), to a chosen subset (multicast), or to everyone (broadcast).
- **One listening port for everybody** (`host:port`, default `127.0.0.1:5555`). The socket can't tell the moderator who is on a connection. The player's **TLS certificate** does: its CN is the seat name, and the certificate is verified against the game's own CA (section 3).
- **Code location.** `setup_users.sh` installs the code to `/opt/werewolf` (`root:werewolf 0750`), because the developer's home directory is mode 700 and the game users couldn't read it there. `deploy_remote.sh` does the same on other machines.
- **Launching.** `run_game.sh` starts the moderator with `sudo -u modbot` and each bot seat with `sudo -u playerN`. Human seats are started the same way, one terminal each; seats on other machines are started there with `run_players.sh`.

## 2. `commslib.py`, the single communication file

### 2.1 The primitive

- **TCP (`AF_INET`, `SOCK_STREAM`)** gives a reliable, ordered byte stream between two processes on any two machines. The kernel retransmits lost packets and delivers the bytes in order, but it delivers *bytes*, not messages, and it can't say who is on the other end.
- **TLS 1.3** runs on top of that stream. It encrypts and integrity-protects every byte, and, because both sides present certificates (*mutual* TLS), it authenticates both ends.
- **asyncio streams** (`asyncio.start_server`, `asyncio.open_connection`, `StreamWriter.start_tls`) drive both from one event loop, so one process can serve many connections without threads.

**Why this primitive:**
- Players must run on other machines, which rules out UNIX domain sockets (one machine only) and pipes.
- Plain TCP would send roles and wolf whispers in clear, and would let anyone who reaches the port claim any seat. TLS fixes both.

`commslib.py` builds three layers on the TLS stream, each covered below:

| Layer | Class | Job |
|---|---|---|
| Framing | `Connection` | Cut the byte stream into messages (2.2); detect dead links (heartbeats, 2.5) |
| Session | `_Session` | Number messages, keep them until acknowledged, replay after a reconnect (2.5) |
| Endpoints | `Server`, `Client` | Listening, TLS handshake, seat assignment, 1:1/multicast/broadcast, reconnecting |

`moderator.py` imports only `Server` (plus default constants) from it, and `player.py` only `Client` and `ConnectionClosed`. Neither touches a socket.

| API | Side | What it does |
|---|---|---|
| `Server.start(players, host=, port=, tls_dir=)` | moderator | One TCP listener; each accepted connection is upgraded to TLS before anything else is read |
| `Server.wait_for_players(timeout)` | moderator | Waits until every seat has joined |
| `Server.send_to(id, msg)` | moderator | **1:1** |
| `Server.multicast(ids, msg)` | moderator | **Multicast**: sends to a subset concurrently (`asyncio.gather`); returns the ids that have no session |
| `Server.broadcast(msg)` | moderator | **Broadcast**: `multicast` to every player still in the game |
| `Server.recv_any(timeout)` | moderator | Next `(player_id, msg)` from **any** player, or a lifecycle event; raises `TimeoutError` |
| `Server.wait_for_delivery(timeout)`, `close()` | moderator | Wait until every player has acknowledged everything; then shut down |
| `Client.connect(player_id=, host=, port=, tls_dir=)` | player | TLS connect + handshake; retries briefly while the moderator starts |
| `Client.send(msg)` / `recv(timeout)` / `close()` | player | Send to the moderator; next message from it; leave the game |

### 2.2 Framing protocol

```
+----------------------+--------------------------------+
| 4 bytes, big-endian  |  N bytes: UTF-8 JSON object    |
| unsigned length N    |  (one message = one dict)      |
+----------------------+--------------------------------+
```

**Why a stream socket needs framing:** `SOCK_STREAM` delivers a byte stream with **no message boundaries**, and TLS doesn't add any.
- Two quick sends can arrive in one read.
- A large send can arrive split across several reads. The tests send a 300 KB frame to force this.

The length prefix tells the receiver exactly where each message ends. `recv()` calls `readexactly(4)` for the header, then `readexactly(N)` for the body. `readexactly` keeps reading until it has exactly that many bytes, which is exactly what framing needs.
- Frames over 1 MiB are refused, so a corrupted header can't make the receiver buffer gigabytes.
- A body that isn't UTF-8, isn't JSON, or isn't a JSON object is a `ProtocolError`. The receiver hangs up on that peer; nothing it sends afterwards can be trusted.

**Sending is one synchronous `write()`.** `Connection.write()` is a plain function, not a coroutine: header and body go to the transport in one call, and no other task can run in the middle of it. So frames from different tasks can never interleave (no lock is needed), and a frame is either entirely queued or not at all. `drain()` then waits, bounded, until the kernel has room.

Every game message travels inside an envelope, `{"type": "DATA", "seq": n, "ack": m, "msg": {...}}`. Control frames (`HELLO`, `WELCOME`, `HEARTBEAT`, `BYE`, `error`) are never delivered to the game (2.5).

### 2.3 Reader tasks feeding a single inbox

**Server.** Every accepted connection gets its own task:
1. the TLS handshake (`_handshake_tls`, at most 5s);
2. the `HELLO` (`_hello`, at most 5s);
3. then `_serve_link`, which loops: `recv()` one frame, and put `(player_id, msg)` into **one shared `asyncio.Queue`**, the inbox.

`recv_any(timeout)` is simply `asyncio.wait_for(inbox.get(), timeout)`. Lifecycle events arrive through the same queue, in order with everything else:

| Event | Meaning |
|---|---|
| `_connected` | a player took its seat (a new session) |
| `_away` | its link broke; the seat is held for `--resume-timeout` (120s) and messages to it are kept |
| `_resumed` | it reconnected and got what it missed |
| `_disconnected` | its session is over for good: it left, didn't come back in time, or broke the protocol |
| `_rejected` | a connection was refused (bad certificate, wrong seat, seat taken, bad token) |

**Client.** The client uses the same pattern. One background task reads the link into a queue, and `recv(timeout)` waits on the queue; the same task also reconnects when the link breaks (2.5). This matters for timeouts: if `recv(timeout)` cancelled a socket read directly, the cancel could land after the 4-byte header was consumed but before the body arrived. The header would be lost and every later frame misread. Cancelling a `queue.get()` is always safe, because frames only enter the queue whole.

### 2.4 Shutdown

At the end of a game the moderator first calls `wait_for_delivery()`: it waits (up to `--linger`, 30s) until every player has acknowledged everything, including `GAME_OVER`. Players leave with `BYE` as soon as they have it, so on a healthy network this takes milliseconds. Over a lossy link, or with a player mid-reconnect, it's what makes sure they still get the result. Then `Server.close()`:
1. stops accepting new connections, and cancels connections still in their handshake;
2. sends `BYE` to every live player, so they don't try to reconnect;
3. closes every connection, waiting up to 2s for unsent data, then aborting;
4. waits (2s limit per listener) for every connection task to exit.

### 2.5 Surviving a bad network

TCP alone can't tell a *frozen* peer or a *silent partition* from a quiet one: the kernel keeps the connection open. Two mechanisms in `commslib` handle that, both described in full in `DISTRIBUTED.md`:

- **Heartbeats** (`Connection.heartbeat`): both ends send `HEARTBEAT` every 2s. A peer that sends nothing for 3 of its intervals (6s) is declared dead, and its link is aborted.
- **Resumable sessions** (`_Session`):
  - each direction numbers its messages and keeps them until the other side acknowledges them (`ack` rides on every frame);
  - a player whose link broke reconnects with its secret session token, and each side replays what the other missed;
  - the receiver delivers only the next number in sequence and drops anything it already has, so nothing is lost or delivered twice.

## 3. Identity and isolation

| File (on each machine) | Owner | Mode | Purpose |
|---|---|---|---|
| `/etc/werewolf/tls/ca.crt` | root | `0644` | The game's CA. Both sides trust **only** this. |
| `/etc/werewolf/tls/ca.key` | root | `0600` | Signs certificates. Only on the machine that ran `make_certs.sh`; never copied. |
| `/etc/werewolf/tls/moderator/` | `modbot` | `0700` | The moderator's certificate (EKU serverAuth, SANs = its names/IPs) and key (`0600`) |
| `/etc/werewolf/tls/playerN/` | `playerN` | `0700` | Player N's certificate (CN = `playerN`, EKU clientAuth) and key (`0600`) |

**How a seat is taken:**
1. TCP connect, then a TLS 1.3 handshake in which **both** sides present certificates. The moderator requires one (`CERT_REQUIRED`) signed by the game CA. The player requires the moderator's to be signed by the same CA **and** to list the address it dialled (hostname check against the SANs), so an impostor moderator is refused.
2. The seat is the **CN of the verified player certificate**. It never comes from a message field. The player's `HELLO` must name that same seat, or it's refused; a copied certificate can't sit in another seat.
3. The moderator replies `WELCOME` with a random 128-bit session token, used only to resume this session later.

**Why that ties seats to Linux users:** `playerN`'s key is readable only by user `playerN` (mode `0600`, in a `0700` directory). So only that user, and root, can present that certificate. `sudo -u player2 ... player1` fails with `Permission denied` before it sends a byte. (Project 1 also asked the kernel for the connecting uid with SO_PEERCRED, which caught root too. Over TCP the kernel can't vouch for a remote user, so possession of the key is the proof.)

**Before authentication** the moderator gives each connection 5s for the TLS handshake and 5s for the `HELLO`. Anything that fails is closed and logged as `_rejected`. `test_tls.sh` checks every case:
- a valid certificate is accepted;
- no certificate, a certificate from another CA, plain TCP, a certificate for no seat, and someone else's certificate are all refused;
- a taken seat is refused;
- three impostor moderators are refused by the player;
- with sudo, the key ownership as real users.

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

**Roles never leak.** `test_no_role_leak.py` plays full games and checks every message each villager received. Apart from their own `ROLE_ASSIGN` and the final `GAME_OVER`, nothing may contain a role name or a wolf-only or seer-only message type. The test first proves its checker catches three fabricated leaks, so it cannot pass by accident. And because every link is TLS, nobody else on the network can read any of these messages either.

## 5. Concurrency

There are two concurrency points required by the assignment: the moderator and each player.

### 5.1 Moderator: all players at once, with timeouts

The moderator is a single-threaded asyncio program. Three mechanisms work together.

**(a) One reader task per connection** (section 2.3), plus one heartbeat task per connection. With five players, five tasks are waiting on five sockets at the same time. The game logic never reads a socket itself; it waits on the shared inbox.

**(b) One deadline-bounded loop per phase:** `Game.message_loop(seconds, handle, is_done)`.

```python
deadline = self.loop.time() + seconds
while not is_done():
    remaining = deadline - self.loop.time()
    if remaining <= 0:
        return False                      # deadline ended the phase
    try:
        pid, msg = await self.server.recv_any(timeout=remaining)
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

**A player who is away** (link broken, session held) stays alive and stays in the pending lists. Messages to them queue and are replayed when they return. If they're back before the deadline they can still act; if not, the deadline treats them like any silent player. `handle_common` consumes `_away` and `_resumed`. Only `_disconnected` (gone for good) removes a player from a phase's pending list.

**(c) Concurrent sends with `asyncio.gather`.** `ROLE_ASSIGN` to every player, the night prompts, and every `multicast` all run their sends at the same time. Each send awaits `drain()`, which pauses while that player's buffer is full. That wait is bounded: a player who doesn't make room within 5s has its link dropped (it becomes away, and keeps its messages). Sending one at a time would let one slow reader delay everyone behind it. `multicast` uses `return_exceptions=True`, so one missing player can't cancel delivery to the others.

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
- **Reconnecting is invisible to these tasks.** If the link breaks, `client.recv()` just waits while commslib reconnects, and `client.send()` queues. The player prints "Lost the link … Reconnecting" and then "Reconnected … Catching up", followed by the missed messages.
- **Bots** (`--bot`) replace the keyboard task. Each reaction runs as its own task, so a bot's simulated thinking delay never stops the socket from being read.

### 5.3 What breaks with blocking I/O

| Blocking design | Failure |
|---|---|
| Moderator reads players in turn (`recv(p1)`, `recv(p2)`, ...) | A quiet or absent player blocks the whole game. Wolves' coordination and chat sit unread behind them. |
| Blocking reads without a timeout | One hung player (still connected, never sending) stalls the game forever. |
| Discussion timer as `time.sleep()` | Chat freezes for the whole discussion. The event loop is blocked too, so commslib's reader and heartbeat tasks stop; buffers fill, and peers declare the moderator dead. |
| Player uses `input()` | Moderator messages (including the 45s `NIGHT_PROMPT`) stay unread until Enter is pressed, so a player can miss their turn without ever seeing the prompt. After `GAME_OVER` the process can't exit until Enter. |
| Player does a blocking socket `recv()` | Typed commands aren't processed until the moderator happens to send something. |

### 5.4 Signals and clean shutdown

- **Moderator.** The lobby and game run as one task, `session()`. `loop.add_signal_handler(SIGINT/SIGTERM, task.cancel)` turns Ctrl-C or `kill` into a `CancelledError` at whatever `await` the game is paused on, usually `recv_any`, so game state is never half-updated. `main()` then broadcasts an aborted `GAME_OVER` (3s limit) and calls `Server.close()`. Without this, SIGTERM would end the process immediately: no `GAME_OVER`, and every player would spend two minutes trying to reconnect.
- **Player.** It exits when `GAME_OVER` arrives. SIGTERM/SIGHUP cancel its tasks, and a `finally` block restores the terminal mode and closes the connection with `BYE`, so the moderator knows at once that the player left rather than holding the seat.
- **`run_game.sh`.** It starts bots under `setsid`, so a Ctrl-C in the moderator's terminal reaches only the moderator. The bots learn of the shutdown the proper way, through `GAME_OVER`.

## 6. Game flow and rules

```
 lobby ──► ROLE_ASSIGN (1:1 each) ──► ┌─► NIGHT: NIGHT_PROMPT ─► collect actions ─► DAY_ANNOUNCE ─► win? ──yes──► GAME_OVER
 (logs joins, rejections)             │        (multicast/1:1)   (message_loop, deadline)         │no
                                      │                                                           ▼
                                      └── win? ◄── VOTE_TALLY ◄── VOTE: collect votes ◄── DAY: discussion (CHAT relay)
                                          │yes     (broadcast)     (ends early)              (full timer)
                                          ▼
                                       GAME_OVER (broadcast, reveals all roles) ──► wait for delivery ──► close
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
| **Win check** after every night and every day | The village wins when no wolves are alive. The wolves win when living wolves ≥ everyone else (`check_winner`). `--max-rounds` (20) or everyone leaving ends the game as a draw. |

## 7. Module boundaries

| File | Knows about | Doesn't know about |
|---|---|---|
| `commslib.py` | TCP, TLS, framing, heartbeats, sessions, asyncio streams | the game |
| `roles.py` | roles and rules | networking, timing |
| `moderator.py` | game state, phases, routing decisions | sockets, TLS, reconnects (uses only `Server`) |
| `player.py` | display, keyboard, bot moves | sockets, TLS, reconnects (uses only `Client`), other players' roles |

`test_day.py` runs the real `Game` class against a `FakeServer`. The fake has the same methods as `commslib.Server` but no sockets. This shows the game logic depends only on commslib's interface, not on how it is implemented. That's also why Project 2 could replace UNIX sockets with TCP + TLS, heartbeats and reconnection with only small changes to `moderator.py` and `player.py`.

## 8. Testing

| Test | What it shows |
|---|---|
| `test_commslib.py` | commslib alone, over TLS: framing; malformed and oversized frames rejected; handshake refusals; socket options; heartbeats; replay after a partition in both directions with no duplicates; takeover; expiry |
| `test_tls.sh` (sudo for the full 21 checks) | Who gets a seat: missing, foreign, stolen or wrong-seat certificates refused; impostor moderators refused; key ownership as real users |
| `test_no_role_leak.py` | No role information ever reaches a villager, across full 6-player games |
| `test_night.py` | A night ends early when everyone acts; a silent Seer gets a no-op at the deadline without affecting the others; the Doctor no-repeat rule |
| `test_day.py` | Majority vote, ties, abstaining at the vote deadline, spectators' restrictions, chat relayed *during* the discussion timer |
| `test_game.py` | Full games reach a winner consistent with `check_winner`; everyone gets `GAME_OVER` and exits 0; the port is released; signals shut down cleanly; a frozen player resumes with exactly the messages everyone else got |
| `test_deploy.sh` (podman) | `deploy_remote.sh` over real ssh/scp/sudo to other "hosts", and a game across three hosts |
| `test_faults.sh` (podman) | Latency and loss (`tc netem`), and partitions mid-night and mid-vote (`iptables DROP`): timeouts fire, players resume, the game finishes |
| `ping_server.py` / `ping_client.py` | commslib alone: 1:1, multicast, broadcast, a `recv_any` timeout, and a 300 KB frame arriving intact |

## 9. Known limitations

- **Identity is possession of the key.** Root, or anyone holding a copied key, can take that seat.
- **A session survives network failures, not a player process restart:** the token is only in memory. A player gone for longer than `--resume-timeout` stays "alive" but silent for the rest of the game.
- **No limit on concurrent unauthenticated connections** (each is bounded to 5s + 5s).
- **The terminal input has no line editing or history.** Arrow keys are ignored.
- **The moderator's console prints every role** (the "god view"). It must not be shown to players.
- **A Ctrl-C during the moderator's first moment of startup**, before its signal handler is installed, ends it with a plain `KeyboardInterrupt`. `run_game.sh` then stops the waiting bots after 10s.
- **Bots play randomly.** They exist to exercise the protocol, not to play well.
