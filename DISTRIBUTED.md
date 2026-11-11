# Werewolf, distributed (Project 2)

This document describes the wire protocol between the moderator and the players once they run on different machines. It grows with each stage; sections for later stages are marked *not yet implemented*.

All networking lives in `commslib.py`. `moderator.py` and `player.py` see the same API as in Project 1. The only differences are three keyword arguments (`host`, `port`, `tls_dir`) and three command-line flags (`--host`, `--port`, `--tls`).

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

## Handshake (TCP)

```
player                                         moderator
  | -- TCP connect -------------------------------> |   CONNECT_TIMEOUT (5s)
  | == TLS 1.3 handshake, both send certificates == |   HELLO_TIMEOUT (5s)
  |    player checks: moderator cert from our CA,   |   moderator checks: player cert
  |    EKU serverAuth, SAN matches the address      |   from our CA, EKU clientAuth
  |    we dialled                                   |
  | -- {"type":"HELLO","player":"p3"} ------------> |   HELLO_TIMEOUT (5s)
  |                                                 |   seat = CN of the verified cert;
  |                                                 |   HELLO "player" must equal it
  | <--- {"type":"WELCOME","player":"p3"} --------- |
  |                 ... game traffic ...            |
```

**Where the seat comes from:** the CN of the player's verified certificate. It is never taken from a message field. That makes it the same kind of fact the socket path is for UNIX: something the peer can't forge. The `HELLO` still names a seat, but only as a check. A player whose certificate doesn't match the seat it asks for is refused, so a copied certificate can't be used to sit in someone else's seat. Stage 4 will also put the session token in the `HELLO`.

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
| Seat already has a live connection | `seat already taken` (under that seat) | `error` frame |
| Silent before or after TLS | TLS timeout / (not posted) | hang-up after 5 s |

For TLS-level rejections the player sees only a hang-up, never the TLS alert that says why. In TLS 1.3 the client's half of the handshake finishes *before* the server checks the client's certificate. The server then sends an alert, but asyncio closes the connection without flushing it (`SSLProtocol._on_handshake_complete` calls `_fatal_error`, which force-closes). `Client.connect()` therefore reports "the moderator hung up before WELCOME, most likely because it rejected our certificate", and the moderator's log has the exact reason. Every refusal raises `SeatRefused`, which is not retried.

**The player refuses the moderator** (raising `ssl.SSLCertVerificationError`, also not retried) if:
- its certificate is from another CA;
- it is a player's certificate (wrong EKU, and players' certificates have no SANs);
- its SANs don't include the host the player dialled.

So an impostor moderator can't collect votes or whispers. `test_tls.sh` checks all of these.

**Ordering guarantee:** the moderator sends `WELCOME` *before* it posts `_connected` to the game. The game can't send to a seat until it has been told the seat exists, so `WELCOME` is always the first frame a player receives.

**Seat race:** two connections claiming the same seat at once can't both win. The "is it taken?" check and the "take it" assignment in `Server._serve_seat` have no `await` between them, and asyncio only switches tasks at an `await`.

**Shutdown during a handshake:** `Server.close()` cancels the tasks of connections that aren't seated yet; it doesn't close their transports directly. Closing a transport cleanly in the middle of a TLS handshake hits a CPython quirk: `SSLProtocol.connection_lost(None)` wakes the handshake waiter with no exception, so `start_tls()` *returns*, with no transport, instead of raising. Cancelling the task takes asyncio's own cleanup path instead.

## Socket options (TCP, both ends)

| Option | Value | Why |
|---|---|---|
| `TCP_NODELAY` | on | Turns off Nagle's algorithm. Our messages are small and interactive; Nagle would hold a vote or chat line back until the previous segment is acked, which can take a round trip plus a delayed ACK (~40 ms). |
| `SO_KEEPALIVE` + `TCP_KEEPIDLE=10`, `TCP_KEEPINTVL=5`, `TCP_KEEPCNT=3` | ~25 s | On an *idle* link whose peer vanished (host crash, cable pulled, NAT forgot us), the kernel probes and resets the link instead of leaving it open forever. |
| `TCP_USER_TIMEOUT` | 30 s | Keepalive doesn't probe while we have *unacked data in flight*. TCP then retransmits instead, for ~15 minutes by default. This caps that: data unacked for 30 s kills the link. |
| `SO_REUSEADDR` (listener) | on | Restarting the moderator right after a game would otherwise fail with "Address already in use" while old connections sit in `TIME_WAIT`. |

These are kernel-level and coarse. Stage 3 adds application heartbeats, which also catch a peer whose kernel is alive but whose process is hung.

## Failure model (so far)

**What TLS buys us:** confidentiality (nobody on the network can read roles or wolf whispers), integrity (nobody can alter a vote in flight), and authentication of *both* ends. What it doesn't cover: a player who leaks their own key, and a compromised moderator host (it holds every role and the CA key).

| Failure | What notices | Result |
|---|---|---|
| Peer closes or resets | `recv()` gets EOF or `ECONNRESET` | `_disconnected` (moderator) / `ConnectionClosed` (player) |
| Peer sends a bad frame | `Connection.recv` | `ProtocolError`; that link is dropped; other players are unaffected |
| Player stops reading (process stopped or wedged) | `Server.send_to` times out after `send_timeout` (5 s) | Link aborted, `_disconnected`; a `broadcast` is delayed by at most 5 s |
| Peer's host vanishes, link idle | TCP keepalive | Link reset after ~25 s, then `_disconnected` |
| Peer's host vanishes, data in flight | `TCP_USER_TIMEOUT` | Link reset after ~30 s, then `_disconnected` |
| Connection that never finishes TLS or never says `HELLO` | `HELLO_TIMEOUT` (TLS: `ssl_handshake_timeout`) | Dropped after 5 s, never takes a seat |
| Bad, missing, or foreign certificate; certificate for another seat | TLS handshake / seat check | Refused, `_rejected` with the reason (table above) |
| Impostor moderator | the player's certificate and hostname check | `ssl.SSLCertVerificationError`; the player never sends a byte of game data |
| Moderator's own key missing or unreadable | `Server.start()` | `OSError` at startup; `moderator.py` prints it and exits 2 |
| Moderator shuts down | `Server.close()` | Pending sends flushed (2 s limit), then TLS close_notify and FIN; handshakes in progress are cancelled |

**What happens to the game on a disconnect** is unchanged from Project 1. The player stays alive and a timed phase treats them as silent. Stage 4 adds reconnecting.

## Heartbeat (stage 3, *not yet implemented*)

## Message envelope: seq / ack (stage 4, *not yet implemented*)

## Resume (stage 4, *not yet implemented*)
