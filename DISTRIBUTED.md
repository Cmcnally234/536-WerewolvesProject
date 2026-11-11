# Werewolf, distributed (Project 2)

This document describes the wire protocol between the moderator and the players once they run on different machines. It grows with each stage; sections for later stages are marked *not yet implemented*.

All networking lives in `commslib.py`. `moderator.py` and `player.py` see the same API as in Project 1. The only differences are two keyword arguments (`host`, `port`) and two command-line flags (`--host`, `--port`).

## Transports

| | UNIX (Project 1) | TCP (Project 2) |
|---|---|---|
| Listening sockets | one per seat, `/run/werewolf/<seat>/sock` | one, `host:port` |
| Who is this connection? | the socket it arrived on (file permissions; optionally SO_PEERCRED) | the `HELLO` frame (stage 1), then the TLS client certificate (stage 2) |
| Selected by | no `port` | `Server.start(..., port=N)`, `Client.connect(port=N, player_id=...)` |

Both transports use the same framing: a 4-byte big-endian length, then that many bytes of UTF-8 JSON. The body must be a JSON object, and frames over 1 MiB are refused. A frame that breaks these rules is a `ProtocolError`, and the receiver hangs up.

## Handshake (TCP)

```
player                                   moderator
  | -- TCP connect (5s limit) ------------> |
  | -- {"type":"HELLO","player":"p3"} ----> |   must arrive within HELLO_TIMEOUT (5s)
  | <--- {"type":"WELCOME","player":"p3"} - |   seat taken; game may now send to p3
  |              ... game traffic ...       |
```

The moderator refuses the connection with `{"type":"error","text":...}` and hangs up if:

| Case | `text` | Inbox event |
|---|---|---|
| First frame isn't a `HELLO` with a string `player` | `expected HELLO first` | none |
| `player` isn't one of the seats | `no seat named '<x>'` | `_rejected` under player id `None` |
| The seat already has a live connection | `seat already taken` | `_rejected` under that seat |
| Nothing arrives within `HELLO_TIMEOUT` | (no message) | none |

The client turns an error reply into a `SeatRefused` exception from `Client.connect()`, and does not retry it. "Connection refused" and connect timeouts are retried until `retry_seconds` is used up, because a player may be started before the moderator.

**Ordering guarantee:** the moderator sends `WELCOME` *before* it posts `_connected` to the game. The game can't send to a seat until it has been told the seat exists, so `WELCOME` is always the first frame a player receives.

**Stage 1 trust model:** the `player` field is believed as-is, so anyone who can reach the port can take any free seat. That's why the moderator listens on `127.0.0.1` unless given `--host`. Stage 2 removes the trust: the seat comes from the verified client certificate.

**Seat race:** two connections claiming the same seat at once can't both win. The "is it taken?" check and the "take it" assignment in `Server._serve_seat` have no `await` between them, and asyncio only switches tasks at an `await`.

## Socket options (TCP, both ends)

| Option | Value | Why |
|---|---|---|
| `TCP_NODELAY` | on | Turns off Nagle's algorithm. Our messages are small and interactive; Nagle would hold a vote or chat line back until the previous segment is acked, which can take a round trip plus a delayed ACK (~40 ms). |
| `SO_KEEPALIVE` + `TCP_KEEPIDLE=10`, `TCP_KEEPINTVL=5`, `TCP_KEEPCNT=3` | ~25 s | On an *idle* link whose peer vanished (host crash, cable pulled, NAT forgot us), the kernel probes and resets the link instead of leaving it open forever. |
| `TCP_USER_TIMEOUT` | 30 s | Keepalive doesn't probe while we have *unacked data in flight*. TCP then retransmits instead, for ~15 minutes by default. This caps that: data unacked for 30 s kills the link. |
| `SO_REUSEADDR` (listener) | on | Restarting the moderator right after a game would otherwise fail with "Address already in use" while old connections sit in `TIME_WAIT`. |

These are kernel-level and coarse. Stage 3 adds application heartbeats, which also catch a peer whose kernel is alive but whose process is hung.

## Failure model (so far)

| Failure | What notices | Result |
|---|---|---|
| Peer closes or resets | `recv()` gets EOF or `ECONNRESET` | `_disconnected` (moderator) / `ConnectionClosed` (player) |
| Peer sends a bad frame | `Connection.recv` | `ProtocolError`; that link is dropped; other players are unaffected |
| Player stops reading (process stopped or wedged) | `Server.send_to` times out after `send_timeout` (5 s) | Link aborted, `_disconnected`; a `broadcast` is delayed by at most 5 s |
| Peer's host vanishes, link idle | TCP keepalive | Link reset after ~25 s, then `_disconnected` |
| Peer's host vanishes, data in flight | `TCP_USER_TIMEOUT` | Link reset after ~30 s, then `_disconnected` |
| Connection that never says `HELLO` | `HELLO_TIMEOUT` | Dropped after 5 s, never takes a seat |
| Moderator shuts down | `Server.close()` | Pending sends flushed (2 s limit), then FIN; mid-handshake connections closed too |

**What happens to the game on a disconnect** is unchanged from Project 1. The player stays alive and a timed phase treats them as silent. Stage 4 adds reconnecting.

## Heartbeat (stage 3, *not yet implemented*)

## Message envelope: seq / ack (stage 4, *not yet implemented*)

## Resume (stage 4, *not yet implemented*)
