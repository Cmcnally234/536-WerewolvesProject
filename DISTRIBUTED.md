# Werewolf, distributed (Project 2)

This document describes the wire protocol between the moderator and the players once they run on different machines. It grows with each stage; sections for later stages are marked *not yet implemented*.

All networking lives in `commslib.py`. `moderator.py` and `player.py` see the same API as in Project 1. The only differences are a few keyword arguments (`host`, `port`, `tls_dir`; `heartbeat_interval`, `heartbeat_misses` and the server's `resume_timeout` on the constructors), the matching command-line flags (`--host`, `--port`, `--tls`, `--heartbeat`, `--heartbeat-misses`, `--resume-timeout`), one call (`Server.lock_roster()`), and four inbox event types (`_away` and `_resumed` on the moderator, `_reconnecting` and `_resumed` on the player).

## Transports

| | UNIX (Project 1) | TCP + mutual TLS (Project 2) |
|---|---|---|
| Listening sockets | one per seat, `/run/werewolf/<seat>/sock` | one, `host:port` |
| Who is this connection? | the socket it arrived on (file permissions; optionally SO_PEERCRED) | the CN of the player's TLS certificate, verified against the game CA |
| Selected by | no `port` | `Server.start(..., port=N, tls_dir=D)`, `Client.connect(port=N, player_id=..., tls_dir=D)` |

Both transports use the same framing: a 4-byte big-endian length, then that many bytes of UTF-8 JSON. The body must be a JSON object, and frames over 1 MiB are refused. A frame that breaks these rules is a `ProtocolError`, and the receiver hangs up. Over TCP the frames travel inside TLS. There is no plaintext TCP mode.

## Certificates (`make_certs.sh`)

```
/etc/werewolf/tls/                 root      0755
├── ca.crt                         root      0644   the game CA; both sides trust ONLY this
├── ca.key                         root      0600   signs certificates; never copied to players
├── moderator/                     modbot    0700
│   ├── cert.pem                             0644   CN=moderator, EKU serverAuth, SANs = moderator's names/IPs
│   └── key.pem                    modbot    0600
└── player1/                       player1   0700   (one directory per player)
    ├── cert.pem                             0644   CN=player1, EKU clientAuth
    └── key.pem                    player1   0600
```

`commslib.tls_paths(identity, tls_dir)` encodes this layout. A player needs only `ca.crt` and its own directory, which is what Stage 5's deploy script will copy to its host.

- **Keys:** EC P-256. Each key is created under `umask 077`, so it is never readable by anyone else, even for a moment.
- **Serials:** random 128-bit.
- **Re-running** keeps the CA and reissues the named certificates. `-f` makes a new CA, which invalidates every old certificate.
- **Strict checks:** the certificates pass `openssl verify -x509_strict`, which Python 3.13+'s default `VERIFY_X509_STRICT` requires.

## Handshake

Every connection, on both transports, starts the same way once the peer is identified:

```
player                                         moderator
  | -- connect (TCP: + TLS 1.3, see below) -------> |   CONNECT_TIMEOUT (5s)
  |                                                 |   identity: UNIX = which socket,
  |                                                 |   TCP = CN of the verified cert
  | -- HELLO {"player": "p3",                       |   HELLO_TIMEOUT (5s)
  |           "token": null | "<session token>",    |
  |           "ack": <last seq I delivered>} -----> |
  | <-- WELCOME {"player": "p3", "token": "...",    |
  |              "ack": <last seq it delivered>,    |
  |              "resumed": false | true,           |
  |              "replaying": n,                    |
  |              "resume_timeout": 120} ----------- |
  | <-- replayed DATA (if resuming), then live ---- |
  | --- replayed DATA (if resuming), then live ---> |
```

The TCP part, before the `HELLO`:

```
  | -- TCP connect -------------------------------> |
  | == TLS 1.3 handshake, both send certificates == |   HELLO_TIMEOUT (5s)
  |    player checks: moderator cert from our CA,   |   moderator checks: player cert
  |    EKU serverAuth, SAN matches the address      |   from our CA, EKU clientAuth
  |    we dialled                                   |
```

**Where the seat comes from:** the socket it arrived on (UNIX), or the CN of the player's verified certificate (TCP). It is never taken from a message field, so it's something the peer can't forge. The `HELLO`'s `player` is only checked against it. A certificate that doesn't match the seat it asks for is refused, so a copied certificate can't be used to sit in someone else's seat. On UNIX `player` may be left out.

**Why the moderator runs the TLS handshake itself:** it calls `StreamWriter.start_tls()` per connection rather than passing `ssl=` to `start_server`. With `start_server(ssl=)`, a failed handshake is only logged by asyncio and never reaches our code. Running it ourselves turns every failure into a `_rejected` event, which the lobby prints.

**Refusals** (each also posts `_rejected` with the reason to the moderator's inbox):

| Case | Moderator's reason (`_rejected`) | What the player sees |
|---|---|---|
| No client certificate | `TLS handshake failed: PEER_DID_NOT_RETURN_A_CERTIFICATE` | the moderator hangs up |
| Certificate not from our CA | `TLS handshake failed: CERTIFICATE_VERIFY_FAILED` | the moderator hangs up |
| Not TLS at all | `TLS handshake failed: WRONG_VERSION_NUMBER` | the moderator hangs up |
| Valid certificate, CN not a seat in this game | `certificate is for 'x', which is not a seat in this game` | `error` frame with the same text |
| Certificate for p2, `HELLO` for p1 | `certificate is for 'p2', not 'p1'` | `error` frame |
| First frame isn't a `HELLO` | (not posted) | `error`: `expected HELLO first` |
| Seat already has a session, and no token given | `seat already taken (a reconnect must present its session token)` (under that seat) | `error` frame |
| Wrong token, or the session already expired | `unknown or expired session token` (under that seat) | `error` frame |
| No token, the seat's session has ended, and the game has started | `this seat's session has ended and the game has started` | `error` frame |
| Silent before or after TLS | TLS timeout / (not posted) | hang-up after 5 s |

For TLS-level rejections the player sees only a hang-up, never the TLS alert that says why. In TLS 1.3 the client's half of the handshake finishes *before* the server checks the client's certificate. The server then sends an alert, but asyncio closes the connection without flushing it (`SSLProtocol._on_handshake_complete` calls `_fatal_error`, which force-closes). `Client.connect()` therefore reports "the moderator hung up before WELCOME, most likely because it rejected our certificate", and the moderator's log has the exact reason. Every refusal raises `SeatRefused`, which is not retried.

**The player refuses the moderator** (raising `ssl.SSLCertVerificationError`, also not retried) if:
- its certificate is from another CA;
- it is a player's certificate (wrong EKU, and players' certificates have no SANs);
- its SANs don't include the host the player dialled.

So an impostor moderator can't collect votes or whispers. `test_tls.sh` checks all of these.

**Ordering guarantee:** `WELCOME`, and then any replay, are written *before* the game is told (`_connected` / `_resumed`). The game can't send to a seat until it has been told the seat exists, so `WELCOME` is always the first frame a player receives on a link, and replayed messages always come before new ones.

**Seat race:** two connections claiming the same seat (or resuming the same session) at once can't both win. In `Server._hello`, everything from looking up the session to attaching the new link is synchronous, with no `await` in between, and asyncio only switches tasks at an `await`. For the same reason the link can't die partway through: it's checked once at the start of that section.

**Shutdown during a handshake:** `Server.close()` cancels the tasks of connections that aren't seated yet; it doesn't close their transports directly. Closing a transport cleanly in the middle of a TLS handshake hits a CPython quirk: `SSLProtocol.connection_lost(None)` wakes the handshake waiter with no exception, so `start_tls()` *returns*, with no transport, instead of raising. Cancelling the task takes asyncio's own cleanup path instead.

## Deployment (multiple machines)

```
 admin machine = moderator machine (holds the CA)        player machines
 +------------------------------------------+            +---------------------------+
 | setup_users.sh   -> modbot, player1..N   |  ssh/scp   | deploy_remote.sh puts:    |
 | make_certs.sh -s <its names/IPs>         | ---------> |  users player3, player4   |
 |   /etc/werewolf/tls/{ca.crt,ca.key,      |  (bundle:  |  /opt/werewolf/*.py       |
 |      moderator/, player1/ .. playerN/}   |  code +    |  /etc/werewolf/tls/       |
 | run_game.sh --host <addr> --port P -R .. |  ca.crt +  |    ca.crt, player3/,      |
 |   moderator (modbot) listens on addr:P   |  only the  |    player4/ (own user,    |
 |   local seats as player1, player2        |  host's    |    0700 / key 0600)       |
 +------------------------------------------+  seats)    | run_players.sh --host ..  |
                ^                                         +---------------------------+
                |        TCP + mutual TLS, port P (firewalld: open on the moderator only)
                +-----------------------------------------------------------+
```

- **What goes where.** A player machine receives the game code, `ca.crt` (to check the moderator), and the certificate and key of *its own seats only*. The CA key stays on the machine that ran `make_certs.sh`, and so do the moderator's key and every other seat's key. So a compromised player machine can impersonate only the seats deployed to it.
- **The bundle in transit.** `deploy_remote.sh` builds it locally under `umask 077` and copies it with `scp -p` (encrypted, modes kept) into a fresh `mktemp -d` directory on the target. It runs the install step there with `ssh -t … sudo` (`-t` so sudo can ask for a password). The bundle is deleted afterwards whether or not the install succeeded, so no copy of a key is left in `/tmp`.
- **Ownership on the target:** each `/etc/werewolf/tls/<seat>/` is `<seat>:<seat> 0700`, its `key.pem` is `0600`, and `/opt/werewolf` is `root:werewolf 0750`. This is the same layout `make_certs.sh` produces as root, so `commslib.tls_paths()` finds the files on every machine.
- **One address for everything.** `run_game.sh --host ADDR` is where the moderator listens, where its local seats connect, and what remote players dial, so `ADDR` must be in the moderator certificate's SANs. `run_game.sh` checks this with `openssl x509 -checkip/-checkhost` before starting. It also refuses remote seats with a loopback `--host`.
- **Versions.** The wire protocol has no version number, so every machine must run the same `commslib.py`. `deploy_remote.sh` prints the checksum on both ends; re-run it after any code change.
- **Firewall.** Only the moderator's machine accepts connections: one TCP port. Player machines connect out and need no rule. The firewalld commands are in the README, and `run_game.sh --open-firewall` adds a runtime-only rule for the length of one game.
- **Tested** by `test_deploy.sh` on three podman containers, each with its own network namespace, users and sshd. It uses real `ssh`/`scp`/`sudo`, checks ownership, modes and that no stray keys landed, then plays a game with the moderator and two seats on one "machine" and three seats on two others.

## Socket options (TCP, both ends)

| Option | Value | Why |
|---|---|---|
| `TCP_NODELAY` | on | Turns off Nagle's algorithm. Our messages are small and interactive; Nagle would hold a vote or chat line back until the previous segment is acked, which can take a round trip plus a delayed ACK (~40 ms). |
| `SO_KEEPALIVE` + `TCP_KEEPIDLE=10`, `TCP_KEEPINTVL=5`, `TCP_KEEPCNT=3` | ~25 s | On an *idle* link whose peer vanished (host crash, cable pulled, NAT forgot us), the kernel probes and resets the link instead of leaving it open forever. |
| `TCP_USER_TIMEOUT` | 30 s | Keepalive doesn't probe while we have *unacked data in flight*. TCP then retransmits instead, for ~15 minutes by default. This caps that: data unacked for 30 s kills the link. |
| `SO_REUSEADDR` (listener) | on | Restarting the moderator right after a game would otherwise fail with "Address already in use" while old connections sit in `TIME_WAIT`. |

These are kernel-level and coarse. The application heartbeats below catch the cases they can't.

## Failure model (so far)

**What TLS buys us:** confidentiality (nobody on the network can read roles or wolf whispers), integrity (nobody can alter a vote in flight), and authentication of *both* ends. What it doesn't cover: a player who leaks their own key, and a compromised moderator host (it holds every role and the CA key).

| Failure | What notices | Result |
|---|---|---|
A **link** failure and a **session** ending are now different things. A broken link makes the player *away*; it reconnects and resumes, losing nothing. A session ends only on purpose (`BYE`), on a protocol error, or when the player doesn't come back within `resume_timeout`.

| Failure | What notices | Result |
|---|---|---|
| Peer closes or resets the link | `recv()` gets EOF or `ECONNRESET` | Moderator: `_away`. Player: `_reconnecting`, then it redials; on success both sides `_resumed` |
| Peer process frozen or hung (kernel still alive) | heartbeat | Link aborted after `misses × interval` (6 s), then away / reconnecting as above, reason `no heartbeat ...` |
| Network silently drops everything (partition, black-holing middlebox) | heartbeat, at both ends | Both give up on the link within 6 s; the player keeps redialling (backoff 0.2 s to 2 s) until the network heals or `resume_timeout` passes |
| Peer's host vanishes | heartbeat (6 s); TCP keepalive (~25 s idle) and `TCP_USER_TIMEOUT` (30 s in flight) as backups | Away; ends after `resume_timeout` unless it comes back |
| Player stops reading while we send a lot | `Server.send_to` waits at most `send_timeout` (5 s) | Link aborted (`... stopped reading ...`), player away, messages kept; a `broadcast` is delayed by at most 5 s |
| Player doesn't come back within `resume_timeout` (120 s) | the moderator's expiry timer | `_disconnected` (`did not come back within ...`); a later resume gets `unknown or expired session token` |
| Away player falls `MAX_OUTBOX` (1000) messages behind | `Server.send_to` | Session ended (`_disconnected`), so memory stays bounded |
| Player quits (`/quit`, `GAME_OVER`, SIGTERM) | its `BYE` | `_disconnected` (`left the game`) at once, with no waiting period |
| Peer sends a bad frame (malformed, sequence gap, impossible ack) | `Connection.recv` / `_Session.on_frame` | `ProtocolError`: the session ends (no resume), and other players are unaffected |
| Connection that never finishes TLS or never says `HELLO` | `HELLO_TIMEOUT` (TLS: `ssl_handshake_timeout`) | Dropped after 5 s, never takes a seat |
| Bad, missing, or foreign certificate; certificate for another seat | TLS handshake / seat check | Refused, `_rejected` with the reason (table above) |
| Impostor moderator | the player's certificate and hostname check | `ssl.SSLCertVerificationError`; the player never sends a byte of game data |
| Moderator's own key missing or unreadable | `Server.start()` | `OSError` at startup; `moderator.py` prints it and exits 2 |
| Moderator shuts down | `Server.close()` | `BYE` to every live player (so they don't try to reconnect), pending sends flushed (2 s limit), then TLS close_notify and FIN; handshakes in progress are cancelled |
| Moderator crashes (no `BYE`) | each player's heartbeat or EOF | Players redial for `resume_timeout`, then give up: `could not reconnect within ...` |

## Heartbeat

```
{"type": "HEARTBEAT", "interval": 2.0}      both directions, both transports
```

- **Both ends** send one as soon as the link is up (after `WELCOME` on TCP), then every `interval` seconds. They're sent unconditionally, not only when idle, which keeps the logic to one loop.
- **The receiver's `Connection.recv()` swallows them,** so the game never sees one. `HEARTBEAT` is therefore a reserved message type.
- **Any frame counts as a sign of life, heartbeat or game message.** Each received frame updates `Connection.last_heard`.
- **Dead peer:** if nothing at all arrives for `misses × peer_interval` seconds, `Connection.heartbeat()` aborts the link with the reason `no heartbeat for 6.0s (limit 3 x 2s)`. The reader then fails with that reason:
  - the moderator gets `_disconnected` with it;
  - the player's `recv()` raises `ConnectionClosed` with it.

  It's the same path as every other disconnect.

**Who sets what:** the interval belongs to the **sender**, which announces it in every heartbeat (until the first one arrives, the receiver assumes its own). The miss limit belongs to the **detector**. So two ends configured differently still agree on when a heartbeat is late. For example, a player beating every 0.5 s isn't dropped by a moderator that beats every 0.1 s.

| Setting | Default | Flag (both programs) |
|---|---|---|
| `heartbeat_interval` | 2 s | `--heartbeat` |
| `heartbeat_misses` | 3 | `--heartbeat-misses` |

Announced intervals must be in (0, 60] s; anything else is a `ProtocolError`. The cap keeps the detection bound finite even if a peer is misconfigured.

**The bound:** a peer is declared dead **at most `misses × peer_interval` seconds after the last frame received from it**. With the defaults that is 6 s. The watchdog sleeps until exactly the moment the peer would become overdue (or until its own next send, whichever is sooner), so it doesn't add a polling delay on top.

**Why heartbeats, when TCP has keepalive:** keepalive probes are answered by the peer's *kernel*, and TCP itself only sees whether bytes are ACKed. That leaves cases TCP can't notice:

| Case | TCP's view | Heartbeat's view |
|---|---|---|
| Peer process frozen (`SIGSTOP`), deadlocked, or stuck in a long GC pause | healthy: the kernel ACKs and answers probes | silent, so dropped after the limit (`test_game.py`: 1.5 s limit, detected in 1.45 s) |
| A middlebox or proxy that accepts traffic but delivers nothing | healthy: our segments are ACKed by the middlebox | silent, so **both** ends drop the link (`test_commslib.py` black-hole proxy) |
| Network silently drops packets, link idle | keepalive notices after ~25 s | noticed after 6 s |
| Network silently drops packets, data in flight | `TCP_USER_TIMEOUT` notices after 30 s | noticed after 6 s |

**Interaction with large frames:** liveness is judged per *complete frame*. A frame must therefore arrive within the limit; with a 1 MiB cap and the default 6 s, that's about 175 KB/s. Game messages are under 1 KB.

**Sending a heartbeat** is a single synchronous `Connection.write()`, like every frame (see the envelope below), so it never interleaves with a game frame. Each heartbeat also carries an `ack`, so acknowledgements keep flowing when no game messages are. Waiting for buffer space is bounded by `interval`. A heartbeat that can't be sent (the peer isn't reading) is simply dropped; the silence check decides whether the peer is dead.

## Message envelope: seq / ack

Every game message, in both directions, travels inside an envelope:

```
{"type": "DATA", "seq": 7, "ack": 12, "msg": { ...the game message... }}
```

Both ends keep a `_Session` (the moderator one per seat, the player one for itself). It holds:

| Field | Meaning |
|---|---|
| `out_seq` | the number of the last message *we* sent. Each new message gets `out_seq + 1`. Numbering is per seat and per direction, and continues across links. |
| `outbox` | `(seq, msg)` for every message sent but not yet acknowledged |
| `in_seq` | the number of the last message we *delivered* to the game. Doubles as our cumulative ack. |

**Acks are piggybacked.** Every frame we send, `DATA` or `HEARTBEAT`, carries `ack = in_seq`, meaning "I have everything up to here". The receiver then drops those messages from its outbox. With heartbeats every 2 s, an outbox holds at most about 2 s of unacknowledged messages while the link is up. There is no separate ACK message.

**Receiving** (`_Session.on_frame`):

| Incoming `seq` | Action |
|---|---|
| `in_seq + 1` | deliver; `in_seq += 1` |
| `≤ in_seq` | drop silently: we already have it (a replay overlapping what had arrived) |
| `> in_seq + 1` | `ProtocolError`. On a stream socket with correct replay this can't happen; delivering it would silently lose the messages in between. |

An `ack` that isn't a non-negative integer, or that is larger than anything we sent, is also a `ProtocolError`.

**Wire order equals seq order.** `Connection.write()` is a plain function, not a coroutine. `_Session.send()` numbers a message and hands the whole frame to the transport in one synchronous step, and no other task can run in between. So frames from different tasks can't interleave or be reordered, and no send lock is needed. A cancelled `drain()` (a send timeout) can only interrupt the wait *after* the frame is fully buffered, so it can never leave a gap.

`HELLO`, `WELCOME`, `HEARTBEAT`, `BYE` and `error` are link-level control frames. They are not numbered and are never delivered to the game.

## Resume

**Starting:** a player's first `HELLO` has `"token": null`. The moderator creates a session with a random token (`secrets.token_urlsafe(16)`, 128 bits) and sends it in `WELCOME`. The token lives only in the player process's memory, inside the TLS-encrypted link.

**When a link breaks:**

1. The player posts `{"type": "_reconnecting", "reason", "resume_timeout"}` to its own game loop, which prints it. Then it redials, with backoff from 0.2 s doubling up to 2 s. Each attempt is a full connect (and TLS handshake), followed by `HELLO {"token": <its token>, "ack": <its in_seq>}`.
2. The moderator posts `_away` to the game and starts a `resume_timeout` timer (default 120 s). Messages to the seat keep going into its outbox.
3. On a `HELLO` with the right token (compared in constant time, `hmac.compare_digest`), the moderator:
   - forgets the outbox entries up to the player's `ack`;
   - writes `WELCOME {"ack": <its in_seq>, "resumed": true, "replaying": n}`;
   - writes the remaining `n` messages, oldest first;
   - posts `_resumed {"away", "replayed"}` to the game.
4. The player does the same with the moderator's `ack`: it forgets what the moderator already has, replays its own outbox (for example, a vote typed while reconnecting), and posts `_resumed {"away", "replaying"}`.

**Why that gives in order with no duplicates:**
- *Nothing is lost:* a message leaves the outbox only once the peer has acknowledged delivering it. Anything lost in flight is still there and is replayed.
- *Nothing is delivered twice:* replay starts after the peer's stated `ack`. Anything that arrived but whose ack was lost is dropped by the receiver's `seq ≤ in_seq` check.
- *Order:* replay goes oldest first and before any new message. New messages are numbered after the replayed ones, and the receiver delivers only `in_seq + 1`.

**Takeover:** the player may notice a dead link before the moderator does. The moderator might still think the old link is fine, for example after a partition that it hasn't timed out yet. A `HELLO` with the right token then wins: the old link is aborted ("replaced by a resumed connection"), and its reader sees that it is no longer the session's link, so it posts nothing. The game gets `_resumed` without ever seeing `_away`.

**Ending on purpose (`BYE`):** `Client.close()` sends `BYE`. This covers `/quit`, exit after `GAME_OVER`, and SIGTERM/SIGHUP. The moderator then ends the session at once (`_disconnected`, `left the game`) instead of holding the seat. `Server.close()` sends `BYE` to every live player, which then stops instead of redialling.

**What can't be resumed:** a player *process* that restarts has lost its token, so its `HELLO` without a token is refused with `seat already taken`. The session survives network failures, not process restarts. Once the game has started (`Server.lock_roster()`), a seat whose session ended can't be taken by a fresh session either: that session never got the seat's role. In the lobby it can, so a player who left the lobby can just start again.

### The game while a player is away

- **The game doesn't pause.** Phase timers keep running. One absent player can't hold the other five hostage, and Project 1's timeouts already define what silence means.
- **The away player stays in the game.** They stay alive, still count for win checks, still appear in pending lists (night actors, voters) and vote candidates, and are still sent everything. Those messages queue in the outbox.
- **If they're back before the phase deadline,** they get the replay (the phase's prompt included) and can still act normally.
- **If they aren't,** the deadline treats them as silent: a no-op at night, an abstention in the vote. This is exactly what a silent connected player gets.
- **On return they catch up on everything,** in order: prompts for phases already over, the results of those phases, chat. Acting on an already-finished prompt just gets the usual "can't do that now" error.
- **If they don't return within `resume_timeout`,** the session ends (`_disconnected`). From then on it's Project 1's disconnect: still alive, silent for the rest of the game, and no longer sent anything.
- **The moderator logs it:**
  - `p3 is away (no heartbeat for 6.0s (limit 3 x 2s)); holding the seat for 120s`
  - `p3 is back after 14.2s; replayed 7 messages`
- **The player prints:**
  - `* Lost the link to the moderator (...). Reconnecting for up to 120s; anything you type is sent when we're back.`
  - `* Reconnected after 14.2s. Catching up on 7 missed message(s):`

`--resume-timeout 0` turns all of this off: a broken link ends the session at once, as in Project 1.
