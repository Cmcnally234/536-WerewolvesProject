"""
test_game.py -- whole games: win conditions, GAME_OVER, clean shutdown.

    python3 test_game.py

Real processes (moderator + bot players, no sudo). Checks that:
  - games run to a winner, and the winner matches the rules
  - every player, dead or alive, gets GAME_OVER and exits with status 0
  - all socket files are gone afterwards
  - SIGTERM / Ctrl-C on the moderator still ends the game cleanly
  - a player killed mid-game doesn't break anything for the others
  - a player frozen mid-game (SIGSTOP) is dropped by heartbeat within the limit
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

import roles
from testnet import net_args

HERE = os.path.dirname(os.path.abspath(__file__))
FAST = ["--night-timeout", "5", "--discussion", "0.5", "--vote-timeout", "3"]


class Run:
    """One moderator + N bot players in a temp dir."""

    def __init__(self, tmp, players, seed, extra=(), modes=None, player_extra=()):
        self.players = players
        self.sock_dir = os.path.join(tmp, "s")
        self.logs = {p: os.path.join(tmp, f"{p}.jsonl") for p in players}
        mod_net, player_net = net_args(self.sock_dir)
        self.mod = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "moderator.py"), *players,
             *mod_net, "--seed", str(seed), *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs = {p: subprocess.Popen(
            [sys.executable, os.path.join(HERE, "player.py"), p, *player_net,
             "--bot", (modes or {}).get(p, "act"), "--log", self.logs[p], *player_extra],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            for p in players}
        # Read the moderator's output as it arrives, stamping each line, so a
        # test can ask WHEN something was logged (see wait_for_log).
        self.mod_lines: list[tuple[float, str]] = []
        self._reader = threading.Thread(target=self._read_mod, daemon=True)
        self._reader.start()

    def _read_mod(self):
        for line in self.mod.stdout:
            self.mod_lines.append((time.monotonic(), line))

    def wait_for_log(self, text: str, timeout=10) -> float:
        """Block until the moderator logs a line containing `text`; return its time."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for stamp, line in list(self.mod_lines):
                if text in line:
                    return stamp
            time.sleep(0.05)
        raise AssertionError(f"moderator never logged {text!r}:\n" + self.mod_out())

    def mod_out(self) -> str:
        return "".join(line for _, line in self.mod_lines)

    def wait_for(self, kind: str, who: str, timeout=10):
        """Block until player `who` has logged a message of type `kind`."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if os.path.exists(self.logs[who]) and kind in open(self.logs[who]).read():
                return
            time.sleep(0.05)
        raise AssertionError(f"{who} never got {kind}")

    def finish(self, timeout=120):
        self.mod.wait(timeout=timeout)
        self._reader.join(timeout=5)
        out = self.mod_out()
        codes = {}
        for p, proc in self.procs.items():
            _, err = proc.communicate(timeout=10)
            codes[p] = (proc.returncode, err)
        received = {p: [json.loads(l) for l in open(path)] if os.path.exists(path) else []
                    for p, path in self.logs.items()}
        return out, codes, received

    def leftover_sockets(self):
        return [p for p in self.players
                if os.path.exists(os.path.join(self.sock_dir, p, "sock"))]


def test_winner_rules():
    W, V, S, D = roles.WEREWOLF, roles.VILLAGER, roles.SEER, roles.DOCTOR
    r = {"a": W, "b": W, "c": S, "d": D, "e": V}
    assert roles.check_winner(["a", "b", "c", "d", "e"], r) is None      # 2 v 3
    assert roles.check_winner(["a", "b", "c", "d"], r) == roles.WEREWOLVES  # 2 v 2: parity
    assert roles.check_winner(["a", "c", "d"], r) is None                # 1 v 2
    assert roles.check_winner(["a", "c"], r) == roles.WEREWOLVES          # 1 v 1
    assert roles.check_winner(["c", "d", "e"], r) == roles.VILLAGE        # no wolves
    print("PASS check_winner rules")


def test_full_games():
    results = []
    for players, seed in [(["p1", "p2", "p3", "p4"], 0)] + \
                         [([f"p{i}" for i in range(1, 7)], s) for s in range(4)]:
        with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
            run = Run(tmp, players, seed, FAST)
            out, codes, received = run.finish()
            assert run.mod.returncode == 0, out
            for p, (code, err) in codes.items():
                assert code == 0, f"{p} exited {code}:\n{err}"
            assert not run.leftover_sockets(), f"socket files left behind: {run.leftover_sockets()}"
            assert "all sockets closed" in out

        overs = [msgs[-1] for msgs in received.values()]
        assert all(m["type"] == "GAME_OVER" for m in overs), "someone missed GAME_OVER"
        final = overs[0]
        assert all(m == final for m in overs), "players got different GAME_OVERs"
        # The announced winner must match the rules applied to the final state.
        dealt = {p: roles.ROLES[r] for p, r in final["roles"].items()}
        assert final["winner"] == roles.check_winner(final["alive"], dealt), final
        dead_players = [p for p in players if p not in final["alive"]]
        results.append(f"{len(players)}p seed {seed}: {final['winner'] or 'draw'} "
                       f"({len(dead_players)} dead, all got GAME_OVER)")
    print("PASS full games, all exit 0, no socket files left:\n  " + "\n  ".join(results))


def test_moderator_signal(sig, name):
    players = ["p1", "p2", "p3", "p4", "p5"]
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        # Everyone silent + long night: the moderator is sitting in recv_any()
        # when the signal lands.
        run = Run(tmp, players, 0, ["--night-timeout", "60"],
                  modes={p: "silent" for p in players})
        run.wait_for("PHASE", "p1")   # night 1 has started
        time.sleep(0.3)
        run.mod.send_signal(sig)
        out, codes, received = run.finish(timeout=10)
        leftovers = run.leftover_sockets()

    assert run.mod.returncode == 1, out
    assert "shutdown requested" in out and "all sockets closed" in out, out
    assert not leftovers, f"socket files left behind: {leftovers}"
    for p, (code, err) in codes.items():
        assert code == 0, f"{p} exited {code}:\n{err}"
        last = received[p][-1]
        assert last["type"] == "GAME_OVER" and last["winner"] is None, last
    print(f"PASS {name} to moderator mid-night: every player got GAME_OVER "
          f"('{last['text']}'), all exit 0, no socket files left")


def test_player_killed_midgame():
    players = [f"p{i}" for i in range(1, 7)]
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        run = Run(tmp, players, 1, FAST)
        run.wait_for("PHASE", "p3")
        run.procs["p3"].send_signal(signal.SIGTERM)
        out, codes, received = run.finish()
        leftovers = run.leftover_sockets()

    assert run.mod.returncode == 0, out
    assert "p3 disconnected" in out, out
    assert codes["p3"][0] == 0, f"killed player should still exit cleanly: {codes['p3']}"
    assert not leftovers
    others = [p for p in players if p != "p3"]
    for p in others:
        assert received[p][-1]["type"] == "GAME_OVER", f"{p} never saw the end"
    print(f"PASS player p3 SIGTERM'd mid-game: it exited 0, the game carried on to "
          f"'{received[others[0]][-1]['text']}'")


def test_player_frozen_midgame():
    """
    SIGSTOP freezes p3's process but not its kernel: the TCP connection stays
    up, keepalive probes are answered, nothing is ever reset. Only the
    missing heartbeats show that the program is gone.
    """
    players = [f"p{i}" for i in range(1, 7)]
    beat, misses = 0.5, 3
    limit = beat * misses
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        run = Run(tmp, players, 1, FAST + ["--heartbeat", str(beat)],
                  player_extra=["--heartbeat", str(beat), "--heartbeat-misses", str(misses)])
        run.wait_for("PHASE", "p3")
        run.procs["p3"].send_signal(signal.SIGSTOP)
        frozen_at = time.monotonic()
        dropped_at = run.wait_for_log("p3 disconnected")
        run.procs["p3"].send_signal(signal.SIGCONT)
        out, codes, received = run.finish()

    detected = dropped_at - frozen_at
    line = next(l for l in out.splitlines() if "p3 disconnected" in l)
    assert run.mod.returncode == 0, out
    assert "no heartbeat" in line, line
    # The heartbeat bound, plus a little for process scheduling.
    assert detected < limit + 0.5, f"took {detected:.2f}s to notice (limit {limit}s)"
    others = [p for p in players if p != "p3"]
    for p in others:
        assert received[p][-1]["type"] == "GAME_OVER", f"{p} never saw the end"
    # Thawed, p3 finds its link already dropped and exits cleanly by itself.
    assert codes["p3"][0] == 0, f"p3 exited {codes['p3']}"
    print(f"PASS player p3 frozen (SIGSTOP) mid-game: dropped after {detected:.2f}s "
          f"(limit {limit}s), logged as '{line.split('] ', 1)[1]}'; the game finished; "
          f"thawed p3 exited 0")


if __name__ == "__main__":
    test_winner_rules()
    test_full_games()
    test_moderator_signal(signal.SIGTERM, "SIGTERM")
    test_moderator_signal(signal.SIGINT, "SIGINT (Ctrl-C)")
    test_player_killed_midgame()
    test_player_frozen_midgame()
    print("all game tests passed")
