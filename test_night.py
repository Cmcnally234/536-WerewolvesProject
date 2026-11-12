"""
test_night.py -- the night phase, including the timeout path.

    python3 test_night.py

Uses real processes (moderator + bot players, no sudo). Because the
moderator deals roles with random.Random(seed), the test can re-deal with
the same seed to know which seat is the Seer, Doctor, etc.
"""

import json
import os
import random
import subprocess
import sys
import tempfile
import time

import roles
from testnet import net_args

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYERS = ["p1", "p2", "p3", "p4", "p5"]
TIMEOUT = 2.0


def seats(seed: int) -> dict[str, list[str]]:
    """role name -> seats, exactly as the moderator will deal them."""
    dealt = roles.deal_roles(PLAYERS, random.Random(seed))
    out: dict[str, list[str]] = {}
    for pid, role in dealt.items():
        out.setdefault(role.name, []).append(pid)
    return out


def run(seed: int, modes: dict[str, str], nights: int = 1):
    """Play `nights` nights. modes: seat -> "act" | "silent". Returns (mod log, seconds, logs)."""
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        mod_net, player_net = net_args()
        start = time.monotonic()
        mod = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "moderator.py"), *PLAYERS, *mod_net,
             "--seed", str(seed), "--night-timeout", str(TIMEOUT),
             # Zero-length day: everyone abstains, nobody is voted out, so
             # the night results aren't disturbed by a random day vote.
             "--max-rounds", str(nights), "--discussion", "0", "--vote-timeout", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logs = {p: os.path.join(tmp, f"{p}.jsonl") for p in PLAYERS}
        players = [subprocess.Popen(
            [sys.executable, os.path.join(HERE, "player.py"), p, *player_net,
             "--bot", modes.get(p, "act"), "--log", logs[p]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL) for p in PLAYERS]
        out, _ = mod.communicate(timeout=60)
        elapsed = time.monotonic() - start
        for proc in players:
            proc.wait(timeout=10)
        assert mod.returncode == 0, out
        received = {p: [json.loads(l) for l in open(path)] for p, path in logs.items()}
    return out, elapsed, received


def test_everyone_acts_ends_early():
    out, elapsed, _ = run(seed=1, modes={})
    assert "night 1: all actions in" in out, out
    assert "night 1: timed out" not in out, out
    assert elapsed < TIMEOUT + 1.5, f"night should end before the deadline, took {elapsed:.1f}s"
    print(f"PASS everyone acts -> night ends early ({elapsed:.1f}s, deadline {TIMEOUT}s)")


def test_silent_seer_gets_noop():
    s = seats(2)
    seer = s["seer"][0]
    out, elapsed, logs = run(seed=2, modes={seer: "silent"})
    assert f"no-op for ['{seer}']" in out, out
    assert "doctor" in out and "protects" in out, "doctor should still have acted:\n" + out
    assert elapsed >= TIMEOUT, f"should have waited for the deadline, took {elapsed:.1f}s"
    assert not any(m["type"] == "SEER_RESULT" for m in logs[seer])
    assert any(m["type"] == "INFO" and "Time's up" in m["text"] for m in logs[seer])
    assert any(m["type"] == "DAY_ANNOUNCE" for m in logs[seer]), "game must continue past the timeout"
    print(f"PASS silent seer {seer} -> no-op after {elapsed:.1f}s, others unaffected")


def test_everyone_silent():
    out, elapsed, _ = run(seed=3, modes={p: "silent" for p in PLAYERS})
    actors = sorted(s for r, ps in seats(3).items() if r != "villager" for s in ps)
    assert f"no-op for {actors}" in out, out
    assert "night 1 result: dead=[]" in out, out
    print(f"PASS all silent -> no-op for {actors}, nobody dies ({elapsed:.1f}s)")


def test_doctor_cannot_repeat():
    s = seats(4)
    doctor = s["doctor"][0]
    # Silent wolves: nobody dies, so the doctor is guaranteed a second night.
    out, _, logs = run(seed=4, modes={w: "silent" for w in s["werewolf"]}, nights=2)
    prompts = [m for m in logs[doctor] if m["type"] == "NIGHT_PROMPT"]
    healed = [m["text"].split()[-2] for m in logs[doctor]
              if m["type"] == "INFO" and m["text"].startswith("You are protecting")]
    assert len(prompts) == 2 and healed, out
    assert healed[0] in prompts[0]["targets"]
    assert healed[0] not in prompts[1]["targets"], (
        f"doctor protected {healed[0]} on night 1 but may pick them again: {prompts[1]['targets']}")
    print(f"PASS doctor {doctor} protected {healed[0]} on night 1; "
          f"night-2 options exclude them: {prompts[1]['targets']}")


if __name__ == "__main__":
    test_everyone_acts_ends_early()
    test_silent_seer_gets_noop()
    test_everyone_silent()
    test_doctor_cannot_repeat()
    print("all night tests passed")
