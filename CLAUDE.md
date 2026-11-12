# Werewolf — multi-process social deduction game (CSE 522 P1)

Python 3.14, Fedora (SELinux enforcing). Standard library only.

## Assignment constraints (graded, non-negotiable)

- Each player is a **separate OS process running as a separate Linux user** (`player1..playerN`); the moderator runs as `modbot`.
- **All IPC goes through `commslib.py`**. `moderator.py` and `player.py` never touch sockets directly.
- Primitive: **UNIX domain stream sockets + asyncio**. No named pipes, no RPC frameworks.
- **Star topology**: each player connects only to the moderator, which relays. Traffic must include **1:1, multicast and broadcast**.
- **Real concurrency**: the moderator handles all players at once and times out night actions; each player reads stdin and the socket at the same time.
- Code must be explainable in office hours: clarity over cleverness, comments explain *why*.
- 4–6 players: 2 Werewolves, 1 Seer, 1 Doctor, the rest Villagers.

## File map

| File | Purpose |
|---|---|
| `commslib.py` | The only networking code. `Connection` (length-prefixed JSON framing), `Server` (moderator side), `Client` (player side), `player_socket_path()`, `tls_paths()`, `ConnectionClosed` / `ProtocolError` / `SeatRefused`. Two transports: UNIX (no `port`) and TCP + mutual TLS (`port`, `tls_dir`). |
| `roles.py` | Role classes and their `valid_targets()`, `deal_roles()`, `pack_agrees()`, `pack_decision()`, `resolve_night()`, `tally_votes()`, `check_winner()`. No I/O. |
| `moderator.py` | Game state machine: lobby → roles → (night → day) loop → `GAME_OVER`. Its docstring has the message routing table. |
| `player.py` | Thin client. `Terminal` (cbreak-mode keyboard input via `loop.add_reader`), `handle()` (renders messages), `Bot` (automated play), socket task + keyboard task. |
| `setup_users.sh` | Creates users and groups, **installs the code to `/opt/werewolf`**, builds the `/run/werewolf` socket tree. Idempotent. |
| `teardown_users.sh` | Kills the game users' processes; removes the users, groups, `/run/werewolf`, `/opt/werewolf` and `/etc/werewolf`. |
| `make_certs.sh` | Game PKI: CA + `moderator` (EKU serverAuth, SANs via `-s`) + one cert per player (CN = seat, EKU clientAuth). Default `/etc/werewolf/tls`. As root, chowns each identity dir/key to its user (`-U` to skip); reuses the CA unless `-f`. Layout must match `commslib.tls_paths()`. |
| `test_tls.sh` | TLS seat rules via `tls_server.py` + `tls_probe.py` with throwaway PKIs: 14 checks as any user, 21 with sudo (adds key ownership as real users). |
| `tls_server.py`, `tls_probe.py` | Helpers for `test_tls.sh`. Probe exit codes: 0 accepted, 3 refused by the moderator, 4 other, 5 the player refused the moderator's cert. |
| `test_permissions.sh` | Proves socket isolation between real users (needs sudo + setup). |
| `perm_server.py`, `perm_probe.py` | Helpers for `test_permissions.sh`. Probe exit codes: 0 accepted, 2 denied by the filesystem, 3 rejected by SO_PEERCRED (now arrives as `SeatRefused` from `connect()`), 4 other. |
| `ping_server.py`, `ping_client.py` | Step-1 commslib smoke test (1:1, multicast, broadcast, timeout, a 300 KB frame). |
| `test_no_role_leak.py` | Full 6-player bot games; asserts villagers never receive role info outside `GAME_OVER`. Includes a self-test proving the checker can fail. |
| `test_night.py` | Night phase: early finish, a silent Seer gets a no-op, everyone silent, the Doctor no-repeat rule. |
| `test_day.py` | In-process `FakeServer` tests (majority, tie, timeout, spectators, early vote, self-vote) plus one real-process live chat relay test. |
| `test_commslib.py` | In-process commslib tests. Failure paths on UNIX **and** TCP: malformed / non-dict / non-UTF-8 / oversized frames, `send_to` timeout on a stuck reader, `close()` with a stuck peer. Client sentinel on garbage and on a reader bug. TCP: star over one port, socket options on both ends, refusals (unknown seat, taken seat, non-HELLO, silent peer), connect retry, close mid-handshake. Heartbeats: idle link kept, silent player/moderator dropped, `BlackHoleProxy` (`cut()` black-holes current links incl. not forwarding closes; `refuse_new`), peer's interval respected, bad intervals rejected. Sessions: `_Session` unit test, partition replay both ways, takeover + dedupe, token refusals/expiry, BYE both ways, UNIX resume, lobby rejoin vs `lock_roster`, outbox overflow. Older tests use `resume_timeout=0` via `Net.server`. |
| `testnet.py` | `WW_TRANSPORT=unix|tcp` (default unix) → `net_args(sock_dir)` gives the moderator/player flags; for tcp it adds `--tls` with a per-process PKI from `shared_pki()` (seats p1..p6). Used by the four process-launching tests and `test_commslib.py`. |
| `DISTRIBUTED.md` | Project 2 protocol doc: transports, handshake, socket options, failure model; later stages fill in heartbeat / seq-ack / resume. Keep in sync with commslib. |
| `test_game.py` | Whole games (winner matches the rules, everyone exits 0, no socket files left), SIGTERM/SIGINT to the moderator, SIGTERM to a player mid-game, SIGSTOP a player (away within the heartbeat limit, then resumes with exactly the broadcasts everyone got), SIGSTOP past `--resume-timeout` (dropped, resume refused). `Run` timestamps moderator lines (`wait_for_log`) and reads logs live (`received`). |
| `README.md` | Grader-facing (someone else runs this on another machine): commslib name/location callout at the top, requirements, setup, "no compilation needed", how to play, the full **commslib API reference** (with a tested example), file list, testing guide, troubleshooting, uninstall. Keep it in sync when the API or flags change. |
| `run_game.sh` | Launches one game as real users: the moderator (foreground, as modbot) and the bot seats (background, `setsid sudo -u playerN`). Prints the command for each human seat. Refuses stale `/opt/werewolf` code. `--port/--host/--tls` for TCP+TLS (checks `--host` is in the moderator cert's SANs), `-R` remote seats, `--open-firewall` (runtime firewalld rule, removed at exit). |
| `deploy_remote.sh` | `USER@HOST SEAT...`: bundles code + `ca.crt` + only those seats' cert/key (umask 077; `sudo tar` locally if the keys aren't readable), `scp -p` to a remote `mktemp -d`, then `ssh -t sudo bash install.sh` (creates users, `/opt/werewolf` root:werewolf 0750, `/etc/werewolf/tls/<seat>` seat 0700 / key 0600, restorecon, prints commslib checksum). Always removes the remote bundle. `moderator` → user modbot. |
| `run_players.sh` | Player-host launcher (installed to `/opt/werewolf` by deploy_remote): `--host --port [--tls] [-H/-S names] SEAT... [-- player args]`; bots in background as their users, waits, summary; Ctrl-C → bots leave with BYE. |
| `cluster.sh`, `test_deploy.sh` | Podman "LAN" (ww-mod, ww-pa, ww-pb on network wwnet, sshd, admin with NOPASSWD sudo, image `localhost/werewolf-host:4`); `cluster_up/down`, `cx`, `cx_admin`. `test_deploy.sh`: 19 checks (deploy, ownership/modes, no stray keys, run_game refusals, a game across 3 hosts). `KEEP=1` keeps containers. Also the base for stage 6. |
| `test_faults.sh` | Podman cluster, 6 seats (`cluster_provision 6`: p3-4 on ww-pa, p5-6 on ww-pb), seed 19 (player3 = seer), `--heartbeat 1`, remote bots `--bot-think 2`. A: netem 150±50ms + 10% loss on all hosts. B: iptables DROP ww-pa↔mod at `--- night 1 ---` → away within 3s, night times out with no-op for player3, heal, resume, Time's up replayed. C: same at `--- vote 1 ---` on the host with voters → vote timeout/abstain, resume. All: game finishes, same broadcasts for all 6. 18 checks. |
| `ARCHITECTURE.md` | Writeup material: topology diagram, commslib/framing, permissions, message table, concurrency, rules, tests, limitations. |

`setup_users.sh` is the deploy step for this machine; `deploy_remote.sh` deploys to other machines over ssh.

## Commands

### Local development (no sudo; everything runs as your user)

Use short socket dirs: Linux caps an AF_UNIX path at 108 bytes.

```bash
python3 moderator.py p1 p2 p3 p4 p5 --dir /tmp/ww --local [--seed N]
python3 player.py p1 --dir /tmp/ww            # human, in its own terminal
python3 player.py p3 --dir /tmp/ww --bot      # bot that plays randomly
python3 player.py p4 --dir /tmp/ww --bot silent   # bot that never responds (exercises timeouts)
```

`--local` makes the moderator create the per-seat directories. Never use it with `/run/werewolf`.

### Real Linux users

```bash
sudo ./setup_users.sh                 # also the "deploy" step; sudo N_PLAYERS=4 ./setup_users.sh for 4 seats
sudo ./test_permissions.sh            # expect "8 passed, 0 failed"
sudo ./run_game.sh                    # whole game, all bots (see "run_game.sh" below)
sudo -u modbot  python3 /opt/werewolf/moderator.py --peercred player1 player2 player3 player4 player5
sudo -u player1 python3 /opt/werewolf/player.py player1          # one terminal per human
sudo -u player3 python3 /opt/werewolf/player.py player3 --bot    # or bots
sudo ./teardown_users.sh              # remove everything
```

`--dir` defaults to `/run/werewolf` for both the moderator and players.

### run_game.sh

```bash
sudo ./run_game.sh [-n 4|5|6] [-H 1,2] [-S 4] [-- moderator flags...]
#   -n seats (default 5)   -H human seats (you start them; commands are printed)
#   -S silent-bot seats (timeout demo)   everything after -- goes to moderator.py
sudo ./run_game.sh -H 1,2 -- --join-timeout 300 --discussion 30
```

Exit status = the moderator's (0 finished, 1 aborted or nobody joined). Logs go to `/tmp/werewolf-run.XXXXXX/` (`moderator.out`, `playerN.out`, `playerN.jsonl`), chowned to `$SUDO_USER`.

### Flags

- **moderator.py**:
  - `--dir`
  - `--port` (TCP + TLS instead of UNIX; 0 = any free port) / `--host` (default 127.0.0.1) / `--tls DIR` (default `/etc/werewolf/tls`); not with `--peercred`/`--local`
  - `--peercred` (enforce the uid check; player ids must be Linux usernames)
  - `--seed` (reproducible role deal)
  - `--join-timeout` (120)
  - `--night-timeout` (45)
  - `--discussion` (60)
  - `--vote-timeout` (30)
  - `--max-rounds` (20; a draw after that many rounds)
  - `--heartbeat` (2) / `--heartbeat-misses` (3)
  - `--resume-timeout` (120; 0 = no reconnecting)
  - `--linger` (30; after the game, wait for every player to acknowledge the result)
  - `--local`
- **player.py**:
  - `--dir`
  - `--port` / `--host` / `--tls DIR` (connect over TCP + TLS as the seat name; `--host` must be in the moderator cert's SANs)
  - `--bot [act|silent]` (no keyboard)
  - `--log FILE` (append every received message as JSON lines; used by the tests)
  - `--wait` (connect retry seconds, 30)
  - `--bot-think SECONDS` (extra delay before each bot action; used by test_faults.sh)
  - `--heartbeat` (2) / `--heartbeat-misses` (3)

In-game commands: `/kill`, `/peek` or `/heal <name|number>`; `/w <msg>` (wolf whisper, night only); `/vote <name|number|none>`; `/role`; `/help`; `/quit`. Any line without a leading `/` is day chat.

### Tests (run from the project dir as your normal user; each takes 5–20s)

```bash
python3 test_commslib.py
python3 test_no_role_leak.py
python3 test_night.py
python3 test_day.py
python3 test_game.py
WW_TRANSPORT=tcp python3 test_game.py   # any of the 4 process tests, over localhost TCP
python3 ping_server.py & for p in p1 p2 p3; do python3 ping_client.py $p & done; wait   # optional --delay 3 on one client
sudo ./test_permissions.sh      # real users only
./test_tls.sh                   # 14 checks; sudo ./test_tls.sh adds key ownership (21)
./test_deploy.sh                # podman, no sudo: 19 checks, deploy + a game across 3 container "hosts"
./test_faults.sh                # podman, no sudo: 18 checks, netem latency/loss + iptables partitions mid-night / mid-vote
```

Across machines: `sudo ./make_certs.sh -s IP:<mod addr> ...`, `./deploy_remote.sh user@host player3 player4`, `sudo ./run_game.sh --host <mod addr> --port 5555 -R 3,4 [--open-firewall]`, then on the player host `sudo /opt/werewolf/run_players.sh --host <mod addr> --port 5555 player3 player4`.

TCP + TLS by hand: `./make_certs.sh -o /tmp/pki p1 p2 p3 p4 p5`, then add `--port 5555 --tls /tmp/pki` to the moderator and every player.

## Gotchas

- **Redeploy after every edit.** Real-user runs use the copy in `/opt/werewolf`, not this directory (the home dir is mode 700, so the game users can't read it). Re-run `sudo ./setup_users.sh`. It copies every `*.py`, tests included.
- **`/run` is tmpfs.** `/run/werewolf` disappears on reboot, so re-run `setup_users.sh` after booting.
- **Permission layout:**
  - `/run/werewolf` is `modbot:werewolf 0750`.
  - `/run/werewolf/playerN` is `modbot:playerN 2750`. The setgid bit makes the socket inherit group `playerN`.
  - The socket is `0660`. `Server.start()` refuses to create missing seat directories, because a directory made by modbot would have the wrong group.
- **SO_PEERCRED** (`--peercred`): the kernel-reported uid of the connecting process must match `pwd.getpwnam(player_id)`. This catches modbot and root, which can get past the file permissions. With `--peercred`, player ids must be real usernames, so don't use it with `p1..p5` in local mode.
- **SELinux:** processes started with `sudo -u` from a shell run as `unconfined_t`, so no denials are expected. If something is blocked: `sudo ausearch -m AVC,USER_AVC -ts recent`, `ls -Z /opt/werewolf /run/werewolf` (expect `usr_t` / `var_run_t`), `sudo restorecon -Rv ...`. A SELinux denial looks identical to a normal EACCES. The warning sign is player1 being denied its *own* socket.
- **TLS rejections look like a hang-up to the player.** asyncio force-closes a failed TLS handshake without flushing the alert, so `Client.connect()` raises `SeatRefused("the moderator hung up before WELCOME...")`; the precise reason is only in the moderator's `_rejected` log line. `test_tls.sh` checks those reasons in the server log for that reason.
- **Heartbeats change Project 1 behaviour:** a player suspended (Ctrl-Z) for more than ~6s is now dropped. Use `--heartbeat`/`--heartbeat-misses` to loosen it.
- **`pkill -f <pattern>` can kill your own shell** if the pattern appears in the command line running it; use a script or exact PIDs.
- **Don't close transports mid-TLS-handshake.** A clean close makes `start_tls()` return with a `None` transport instead of raising (CPython quirk). `Server.close()` cancels the `_handshaking` tasks instead.
- **`$?` after `$(...)` in the same command is the substitution's status.** run_game.sh's summary printed every bot as exit 0 for that reason (fixed: save `code=$?` right after `wait`).
- **`umask 077` + `mkdir -p` makes missing parents 0700 too.** make_certs.sh creates `$OUT` under `umask 022` so `/etc/werewolf` stays traversable (found by test_deploy.sh; test_tls.sh's temp dirs hid it).
- **Cluster containers:** sshd there needs `PerSourcePenalties no`, an unlocked account (`usermod -p '*'`), `pam_loginuid` optional, and `podman exec -u` needs `-e HOME=...`. `firewalld` isn't in the containers, so `--open-firewall` is untested there.
- **`asyncio.wait_for(q.get(), 0)` raises TimeoutError even if the queue has an item** (Python 3.14). Use a small positive timeout to drain.
- **test_faults/test_deploy copy the working tree's `*.py`/`*.sh` into the containers at start** — don't edit them while those tests run.
- In this dev shell, `ls` is aliased to a different tool. Use `/usr/bin/ls` when exit codes or exact output matter.

## Messages

| Type | Direction | Delivery |
|---|---|---|
| `ROLE_ASSIGN` | mod → player | 1:1 (wolves also get `teammates`) |
| `PHASE` | mod → all | broadcast (night / day / vote started; the vote phase carries `candidates`) |
| `NIGHT_PROMPT` | mod → wolves / seer / doctor | multicast to wolves (includes `pack`); 1:1 to seer and doctor |
| `NIGHT_ACTION` | player → mod | 1:1 (`target`) |
| `PACK_VOTES` | mod → wolves | multicast (current kill votes) |
| `WEREWOLF_CHAT` | wolf → mod → wolves | multicast relay, night only |
| `SEER_RESULT` | mod → seer | 1:1 (`is_werewolf`) |
| `DAY_ANNOUNCE` | mod → all | broadcast (names of the dead, never roles) |
| `CHAT` | player → mod → living players | multicast relay, day and vote phases only |
| `VOTE` | player → mod | 1:1 (`target`, or `null` to abstain) |
| `VOTE_TALLY` | mod → all | broadcast (every vote, the counts, the eliminated player) |
| `GAME_OVER` | mod → all | broadcast; the **only** message containing every role |
| `INFO` / `error` | mod → player(s) | 1:1 (or multicast to wolves for "pack agrees") |
| `_connected` / `_away` / `_resumed` / `_disconnected` / `_rejected` | commslib → moderator inbox | internal events, never sent on the wire |
| `_reconnecting` / `_resumed` | commslib → player's `recv()` | internal events; `player.py` prints them |
| `HELLO` / `WELCOME` / `DATA` / `HEARTBEAT` / `BYE` | commslib ↔ commslib | link-level frames (DISTRIBUTED.md); every game message is inside a `DATA` envelope |

`PHASE`, `PACK_VOTES` and `INFO` were added beyond the original spec.

## Design decisions

- **Framing:** a 4-byte big-endian length followed by a UTF-8 JSON body; frames over 1 MiB are refused. Stream sockets have no message boundaries, so the length prefix is what separates messages. `readexactly()` reads exactly one frame.
- **A reader task per connection feeding one queue.** commslib's `Server` runs a `_serve_player` task per connection, and they all push into one inbox; `recv_any(timeout)` = `wait_for(inbox.get())`. `Client` uses the same pattern so a timeout can never cancel a half-read frame, which would desync the stream.
- **One socket per seat (UNIX).** The moderator knows who a connection belongs to from *which socket it arrived on*, which file permissions enforce; it doesn't trust message contents for identity.
- **Seat = certificate CN (TCP).** The moderator runs `start_tls()` itself in `_handshake_tcp` (so failures become `_rejected`), takes the seat from the verified peer cert, and only *checks* the HELLO's `player` against it. Both sides trust only `ca.crt`; TLS 1.3 minimum; the player verifies the moderator's SAN against `--host`. No plaintext TCP mode.
- **Sessions** (`_Session`, both ends, both transports): every game message is `DATA {seq, ack, msg}`; acks piggyback on every frame (DATA and HEARTBEAT); the outbox keeps unacked messages; receiver delivers only `in_seq+1`, drops `≤ in_seq`, gap = ProtocolError. Every connection does HELLO{player, token, ack} → WELCOME{token, ack, resumed, replaying, resume_timeout} → replay. `Connection.write()` is synchronous (numbering + write in one step = wire order is seq order; no send lock). Server `_hello` is await-free from session lookup to `attach()` (race-free). Link loss → `_away` + expiry timer; token resume → `_resumed` (takeover if the old link still looked alive); `BYE`/ProtocolError/expiry → `_disconnected`. Client reconnects itself (backoff 0.2→2s) for the server's resume_timeout. `Server.connected()` = sessions (live or away); `live()` = live links. `lock_roster()` after the lobby.
- **Game while away:** no pause; away player stays alive and pending; messages queue and replay; silence at a deadline = no-op/abstain as before.
- **End of game:** `session()` calls `server.wait_for_delivery(--linger)` (outboxes empty = everyone acked or left with BYE) before `close()`, then logs the `_resumed`/`_away` events of the wait (normal BYE leaves aren't logged). Found by test_faults.sh: an away player, or a lossy link still retransmitting when `close()`'s 2s flush limit aborted the socket, lost GAME_OVER. Signals skip the wait (`holder["over"]` also stops a second GAME_OVER).
- **Heartbeats** (`Connection.heartbeat`, one task per link on both sides, both transports): `HEARTBEAT {interval}` sent at once then every interval; `recv()` swallows them and stamps `last_heard` on every frame; silence ≥ misses × *peer's announced* interval → `abort(reason)` → the normal reader/`_disconnected` path. Interval belongs to the sender, misses to the detector. `abort(reason)` makes the reader report that reason instead of "peer closed".
- **Lobby** (`moderator.lobby()`) reads the inbox itself rather than `wait_for_players`, so joins and rejections are logged as they happen; it tracks joins from events, not `server.connected()` (which runs ahead of the inbox).
- **`Game.message_loop(seconds, handle, is_done)`** is the one concurrency loop. It gives each phase a single overall deadline; each `recv_any` waits only for the time remaining. Night and vote end early when nobody is pending; discussion always runs its full time.
- **Night timeout:** anyone still pending at the deadline gets a no-op and an `INFO` "time's up". The game continues.
- **Seer and Doctor:** the first valid choice is final. The Seer gets `SEER_RESULT` immediately.
- **Doctor no-repeat:** `Doctor.valid_targets` excludes `last_healed`. A Doctor who didn't act leaves no restriction. The Doctor may protect themselves.
- **Wolves:**
  - Votes can change until all living wolves pick the same target. Agreement ends the wolves' part of the night, and a later change puts them back to pending.
  - At the deadline without agreement, `pack_decision()` takes the most-voted target, with ties broken by `rng.choice`. No votes means no kill.
  - Bot wolves avoid endless vote-swapping by following the alphabetically-first wolf. This is bot behaviour only, not a game rule.
- **Day vote:**
  - The most votes wins (a plurality); a tie or no votes eliminates nobody.
  - Votes are final, self-votes are rejected, and silence counts as abstaining.
  - Votes are public in `VOTE_TALLY`.
- **Win check** after every night and every day: the village wins when no wolves are alive; the wolves win when living wolves ≥ everyone else. `--max-rounds` and "everyone disconnected" end the game as a draw.
- **Spectators:** dead players receive broadcasts (`PHASE`, `DAY_ANNOUNCE`, `VOTE_TALLY`, `GAME_OVER`), not `CHAT`. Their messages get an error. Roles are not revealed on death.
- **Shutdown:**
  - SIGINT/SIGTERM on the moderator cancel the session task. It then sends an "aborted" `GAME_OVER` (limited to 3s) and closes the server.
  - `Server.close()` waits (limited to 2s per listener) for the handler tasks, then deletes the socket files.
  - A player exits on `GAME_OVER`; SIGTERM/SIGHUP cancel its tasks, and its `finally` block restores the terminal.
- **Player keyboard input:** cbreak mode plus `add_reader` (no threads), so incoming messages can be printed above the half-typed line and that line redrawn under them. `asyncio.wait(FIRST_COMPLETED)` rather than `gather`, so either task finishing ends the program.

## Current status

**Written:** steps 1–2 (commslib, users/permissions), game stages A–D, `run_game.sh`, `ARCHITECTURE.md`.

**Tested:**
- **By Claude, locally as the dev user:** all four `test_*.py` files and the ping test pass. `run_game.sh` was exercised through a copy with `sudo -u` removed and a temp socket dir:
  - a full bot game;
  - a silent-bot timeout;
  - Ctrl-C mid-game (bots got the aborted `GAME_OVER`, everything exited 0, no socket files left);
  - argument validation;
  - stale-code detection.
- **By the user, 2026-10-17:** ran the `README.md` testing guide as real Linux users (automated tests, `test_permissions.sh`, bot game, multi-terminal human game, breakage tests) and reported that it worked.

- **By the user, 2026-10-17:** ran `sudo ./run_game.sh` as root and reported that it works.

Everything has now been run on the real multi-user setup. Handover archive: `~/werewolf.tar.gz` (built with `git archive` from commit `97d71d7`).

## Known issues and shortcuts

- **A player whose session ended** (didn't come back within `--resume-timeout`, or quit) stays "alive": they count toward win checks and remain vote targets, but get nothing more. Sessions survive network failures, not player *process* restarts: the token is only in memory, so a restarted player is refused (`seat already taken`).
- **The keyboard input has no line editing or history.** Arrow keys and other escape sequences are ignored, and backspace over wide (double-width) characters may redraw slightly wrong.
- **The moderator's stdout prints every role** (the "god view"). Its terminal must not be shown to players.
- **`setup_users.sh` copies the test and helper scripts into `/opt/werewolf`** along with the game code.
- **`--local` and the `ping_server.py` per-seat `makedirs`** exist for development only.
- **Bots are random**, not strategic.
- **Ctrl-C in the moderator's first moment of startup** (before `asyncio.run` installs signal handlers) gives a plain `KeyboardInterrupt` / exit 130. `run_game.sh` then kills the still-waiting bots after 10s.

## Remaining work

1. The PDF writeup. `ARCHITECTURE.md` has the text, the diagram (ASCII), and the message table. Still needed:
   - screenshots: `test_permissions.sh` output, a multi-terminal game, the timeout path, `GAME_OVER`, the `run_game.sh` summary, `ps -o user,pid,cmd -C python3` showing one user per process;
   - optionally, a nicer rendering of the topology diagram.
