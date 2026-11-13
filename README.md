# Werewolf over TCP + mutual TLS (CSE 522 Project 2)

A text-based Werewolf (Mafia) game for 4–6 players. A moderator process runs the game; every player is a separate process running as its own Linux user, on the same machine or on other machines. Every connection is TCP with mutual TLS 1.3 and certificates from a private CA; players resume after network failures without losing or duplicating messages. Python standard library only. Project 1, the single-machine version over UNIX domain sockets, is the git tag `project1`.

## Game rules

- Roles: 2 Werewolves, 1 Seer, 1 Doctor, the rest Villagers. Each player sees only their own role (wolves also learn their partner).
- **Night:** the wolves agree on one non-wolf to kill (at the deadline the most-voted target is taken, ties broken at random). The Seer learns whether one other player is a wolf. The Doctor protects one player, possibly themselves, but **not the same player two nights in a row**.
- **Day:** a timed discussion, then a vote. The most votes eliminates that player; a tie or no votes eliminates nobody. Votes are final and public; you can't vote for yourself; silence counts as abstaining.
- Anyone who doesn't act before a deadline gets a no-op (night) or abstains (vote); the game never waits forever.
- The dead keep watching (announcements, not chat). Roles are revealed only at the end.
- **Village wins** when no wolf is left; **wolves win** when they are at least as many as everyone else. After 20 rounds the game is a draw.

## Architecture

```
          <MODERATOR_HOST>                         <PLAYER_HOST_A>
  +-----------------------------+   TLS 1.3   +----------------------+
  | moderator.py  (user modbot) |<===========>| player.py  (player3) |
  |   listens on :5555          |<===========>| player.py  (player4) |
  | player.py (player1)  --TLS--|             +----------------------+
  | player.py (player2)  --TLS--|   TLS 1.3   +----------------------+
  +-----------------------------+<===========>| player.py  (player5) |
   star: players only ever talk to the         +----------------------+
   moderator, which relays                       <PLAYER_HOST_B>
```

| File | What it does |
|---|---|
| `commslib.py` | **All networking**: framing, TLS, heartbeats, sequenced resumable sessions. Nothing else opens a socket. |
| `moderator.py` | The game: lobby, roles, night/day/vote phases with timeouts, win checks. Run as `modbot`. |
| `player.py` | A player: renders messages, reads the keyboard and the socket at once; `--bot` plays automatically. |
| `roles.py` | The rules (roles, legal targets, vote counting, win check). No I/O. |
| `setup_users.sh` / `teardown_users.sh` | Create / remove the Linux users and `/opt/werewolf` (the installed code). |
| `make_certs.sh` | The private CA, the moderator's certificate and one per player, each key owned by its user. |
| `run_game.sh` | One command for a whole game on this machine (moderator + bots; prints commands for humans). |
| `deploy_remote.sh`, `run_players.sh` | Install a seat's code and key on another machine over ssh; start that machine's seats. |
| `test_*.py`, `test_*.sh`, `testnet.py`, `cluster.sh`, `tls_*.py`, `ping_*.py` | Tests and test helpers (see Testing). |
| `ARCHITECTURE.md`, `DISTRIBUTED.md` | The design: an overview (topology, the communication primitive, identity, messages, concurrency, rules), and the wire protocol in full detail. |

The public API of `commslib.py`:

| Class | Method | Purpose | Kind |
|---|---|---|---|
| `Server` | `start(players, host=, port=, tls_dir=)` | Listen on one TLS port for these seats | setup |
| `Server` | `wait_for_players(timeout)` | Wait until every seat has joined | setup |
| `Server` | `send_to(player, msg)` | Send to one player | 1:1 |
| `Server` | `multicast(players, msg)` | Send to a subset, concurrently | multicast |
| `Server` | `broadcast(msg)` | Send to every player still in the game | broadcast |
| `Server` | `recv_any(timeout)` | Next message (or event such as `_away`) from any player | receive |
| `Server` | `wait_for_delivery(timeout)`, `close()` | Wait until everyone has acknowledged; shut down | teardown |
| `Client` | `connect(player_id=, host=, port=, tls_dir=)` | Join as a seat; reconnects by itself later | setup |
| `Client` | `send(msg)` | Send to the moderator | 1:1 |
| `Client` | `recv(timeout)` | Next message from the moderator | receive |
| `Client` | `close()` | Leave the game | teardown |

## Requirements

- Linux. Developed and tested on Fedora 44 (SELinux enforcing).
- Python 3.11 or newer on every machine (tested on 3.14). No pip packages; nothing to compile.
- `openssl` (for `make_certs.sh`) and `sudo` access.
- Several machines: `ssh`/`scp` from the moderator's machine to each player machine, and the game port open in the moderator's firewall (firewalld on Fedora).
- Fault-injection and multi-host tests: `podman` (rootless). `tc` and `iptables` run *inside* the test containers; your machine doesn't need them.

## Quick start (one machine)

```bash
tar xzf werewolf.tar.gz && cd werewolf        # made with: git archive --prefix=werewolf/ -o werewolf.tar.gz HEAD
sudo ./setup_users.sh            # users modbot, player1..player5; code -> /opt/werewolf
sudo ./make_certs.sh             # CA + certificates in /etc/werewolf/tls (each key owned by its user)
sudo ./run_game.sh -- --night-timeout 10 --discussion 5 --vote-timeout 10   # a whole game, all bots
```

The moderator runs in that terminal and prints every role, so keep it hidden from players. The summary ends with each player's exit status and `port 5555: released`. For 4 or 6 seats: `sudo N_PLAYERS=6 ./setup_users.sh`, `sudo N_PLAYERS=6 ./make_certs.sh`, then `sudo ./run_game.sh -n 6`.

To play yourself, let the bots fill the other seats and start your seat in a second terminal:

```bash
sudo ./run_game.sh -H 1 -- --join-timeout 300                   # terminal 1: moderator + bots for seats 2-5
sudo -u player1 python3 /opt/werewolf/player.py player1          # terminal 2: you, as Linux user player1
```

Commands in the game: `/kill`, `/peek`, `/heal <name|number>` (night), `/w <msg>` (wolf whisper), `/vote <name|number|none>`, `/role`, `/help`, `/quit`; any other line is day chat. `run_game.sh -S 4` makes seat 4 a bot that never answers, to show the timeouts. After any code change, re-run `sudo ./setup_users.sh`; `run_game.sh` refuses to start on stale code in `/opt/werewolf`.

## Multi-host setup

*Verified only between podman containers (`test_deploy.sh`), not on separate physical machines.*

```bash
# On the moderator's machine. The certificate must list every name/IP players will dial.
sudo ./setup_users.sh
sudo ./make_certs.sh -s DNS:localhost -s IP:127.0.0.1 -s DNS:<MODERATOR_HOST> -s IP:<MODERATOR_IP>
./deploy_remote.sh <USER>@<PLAYER_HOST_A> player3 player4   # code + only these seats' keys, via scp/ssh
./deploy_remote.sh <USER>@<PLAYER_HOST_B> player5
sudo ./run_game.sh --host <MODERATOR_HOST> --port 5555 -R 3,4,5

# On each player machine, once the moderator is waiting:
sudo /opt/werewolf/run_players.sh --host <MODERATOR_HOST> --port 5555 player3 player4
# or play a seat yourself:  sudo -u player3 python3 /opt/werewolf/player.py player3 --host <MODERATOR_HOST>
```

`deploy_remote.sh` needs an account on the target that can `sudo`; it creates the seat users there, installs `/opt/werewolf`, and puts each key in `/etc/werewolf/tls/<seat>/` (owner `<seat>`, mode 0600). It never copies `ca.key` or other seats' keys, and prints `commslib.py` checksums for both ends; re-run it after every code change. `run_game.sh` refuses to start if `--host` isn't in the moderator's certificate.

Open the port on the moderator's machine only (players connect out). *Not verified (no firewalld in the test containers):*

```bash
sudo firewall-cmd --get-active-zones                         # which zone your interface is in
sudo firewall-cmd --add-port=5555/tcp                        # until reboot; add --zone=<ZONE> if needed
sudo firewall-cmd --permanent --add-port=5555/tcp && sudo firewall-cmd --reload   # permanent
```

`sudo ./run_game.sh ... --open-firewall` opens the port for one game and closes it afterwards (*not verified*).

## Security design

- **TLS 1.3 only, mutual.** Both sides trust only the game's CA (`ca.crt`), present their own certificate, and require the other's. The player also checks the moderator's certificate against the address it dialled (SAN), so an impostor moderator is refused.
- **Identity comes from the verified certificate**, never from a message. The seat is the certificate's CN; the `HELLO` naming a different seat is refused. Each key is readable only by its own Linux user, so only that user can take the seat.
- **Session tokens.** A seat's session gets a random 128-bit token (`secrets`), compared in constant time; only that token resumes it after a network failure.
- **Before authentication:** the TLS handshake and the `HELLO` each get 5 s; frames over 1 MiB, malformed JSON and protocol violations close the connection. There is no limit on the number of concurrent unauthenticated connections.
- **Game logs are private.** `run_game.sh` and `run_players.sh` keep their logs in `/tmp/werewolf-run.*/` and `/tmp/werewolf-players.*/`, which every local user can reach, so the directory is `0711` (nobody can list it) and every file is `0600`, readable only by its owner. The moderator's log holds every role, and each player's log holds that player's secrets. After the game the logs are handed to whoever ran `sudo`, still private.
- There is no plaintext mode and no flag or variable that turns TLS or verification off. Threat model, failure model and protocol details: `DISTRIBUTED.md` ("Handshake", "Failure model"). There is no separate `SECURITY.md`.

## Testing

| Command | Proves | Needs | Expect |
|---|---|---|---|
| `python3 test_commslib.py` | framing; malformed / oversized frames rejected; handshake refusals; heartbeats; replay after partitions with no duplicates | — | `all commslib tests passed` |
| `python3 test_no_role_leak.py` | villagers never receive anyone's role before `GAME_OVER` (with a self-test of the checker) | — | `PASS: 12 villager logs, no role leaks` |
| `python3 test_night.py` | night timeouts, early finish, Doctor no-repeat | — | `all night tests passed` |
| `python3 test_day.py` | votes, ties, abstention, spectators, chat relay | — | `all day tests passed` |
| `python3 test_game.py` | whole games; signals; a frozen player resumes; a player gone too long is dropped | — | `all game tests passed` |
| `./test_tls.sh` | who gets a seat: missing, foreign, stolen or wrong-seat certificates refused; impostor moderators refused | — | `14 passed, 0 failed` |
| `sudo ./test_tls.sh` | the same, plus key ownership as the real users | root, after `setup_users.sh` | `21 passed, 0 failed` |
| `./test_deploy.sh` | `deploy_remote.sh` over real ssh/scp/sudo to other "hosts"; private game logs; a game across three hosts | podman | `24 passed, 0 failed` |
| `./test_faults.sh` | `tc netem` latency + loss; `iptables DROP` partitions mid-night and mid-vote: timeouts fire, players resume, game finishes | podman | `18 passed, 0 failed` |

The tests without "root" run as your own user over localhost. The two podman tests start three containers as separate hosts (the first run builds a Fedora image, so it needs network access), and no real second machine is needed. There is no `test_fuzz.py`; malformed-input handling is covered in `test_commslib.py`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Remote player: `could not connect: [Errno 113] Connect call failed ('<ADDR>', 5555)` at once | firewalld rejects the port | Open 5555/tcp on the moderator's machine (see Multi-host) |
| `could not connect: no answer from <ADDR>:5555 within the connect timeout ...` | Wrong address, or a firewall silently dropping packets | Check `--host`; check the firewall on and between the machines |
| `certificate verify failed: IP address mismatch` / `Hostname mismatch`; or `run_game.sh`: `the moderator's certificate doesn't list '<ADDR>'` | The address players dial isn't in the moderator's certificate (SAN) | Re-run `sudo ./make_certs.sh -s ...` with every name/IP, then `deploy_remote.sh` again |
| `certificate verify failed: certificate is not yet valid` | The player machine's clock is behind the machine that made the certificates | Sync the clocks (`timedatectl`; chronyd), or reissue the certificates |
| `could not connect: can't load TLS files for 'player1' ... Permission denied` | Started as the wrong Linux user (e.g. `sudo -u player2 ... player1`), or certificates made with `-U` | Run as the user that owns `/etc/werewolf/tls/<seat>/key.pem`; re-run `sudo ./make_certs.sh` |
| `run_game.sh`: `/opt/werewolf is out of date (...)` | Code changed since the last install | `sudo ./setup_users.sh` (and `deploy_remote.sh` for other machines) |
| `run_game.sh`: `no certificate for player6 ...` | More seats than certificates | `sudo N_PLAYERS=6 ./setup_users.sh && sudo N_PLAYERS=6 ./make_certs.sh` |
| Player: `the moderator hung up before WELCOME, most likely because it rejected our certificate` | Certificate from an old CA (e.g. after `make_certs.sh -f`); the moderator's log says why | Redeploy the new certificates to every machine |
| `Permission denied` that the file modes don't explain | SELinux (looks identical) | `sudo ausearch -m AVC,USER_AVC -ts recent`; `sudo restorecon -Rv /opt/werewolf /etc/werewolf` (*not verified*) |

## Cleanup and limitations

```bash
sudo ./teardown_users.sh                   # users, group, /opt/werewolf, /etc/werewolf (N_PLAYERS=6 if you made 6)
sudo firewall-cmd --remove-port=5555/tcp   # if you opened it (add --permanent and --reload for a permanent rule; not verified)
```

On each player machine (*verified between containers only*):

```bash
scp teardown_users.sh <USER>@<PLAYER_HOST>: && ssh -t <USER>@<PLAYER_HOST> sudo ./teardown_users.sh
```

**Files not to commit:** certificates and keys (`certs/`, `*.key`, `*.pem`, `*.csr`, `*.srl`; already in `.gitignore`). `make_certs.sh` writes to `/etc/werewolf/tls`, outside the repository, unless given `-o`.

**Known limitations:**
- Identity is possession of the key: root, or anyone holding a copied key, can take that seat. (Project 1 also checked the kernel-reported uid; that's impossible over TCP.)
- A session survives network failures, not a player *process* restart: the token is in memory only, so a restarted player is refused until the game ends.
- A player gone longer than `--resume-timeout` (120 s) stays "alive" but silent for the rest of the game.
- No limit on concurrent unauthenticated connections; the CA key lives on the moderator's machine.
- No protocol version check: all machines must run the same `commslib.py` (`deploy_remote.sh` prints checksums).
- The moderator's terminal shows every role. Bots play randomly. Player input has no line editing.
- Setting `SSLKEYLOGFILE` makes both ends write TLS secrets to that file (Python's `ssl` default).
