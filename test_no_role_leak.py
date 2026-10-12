"""
test_no_role_leak.py -- a villager must never learn anyone else's role.

Runs real games: one moderator process plus one player process per seat,
all as the current user (no sudo needed), with every player logging each
message it receives. Then it inspects the villagers' logs.

    python3 test_no_role_leak.py

Rule checked: apart from its own ROLE_ASSIGN and the final GAME_OVER (which
reveals everything by design), nothing a villager receives may contain a
role name or a wolf/seer-only message type.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# 6 seats = 2 villagers per game, and games last long enough to have days.
PLAYERS = ["p1", "p2", "p3", "p4", "p5", "p6"]
SEEDS = range(6)

# Words that would reveal a role if they reached a villager.
ROLE_WORD = re.compile(r"\b(werewolf|werewolves|seer|doctor)\b", re.IGNORECASE)
# Message types a villager should never receive at all.
PRIVATE_TYPES = {"WEREWOLF_CHAT", "SEER_RESULT", "NIGHT_PROMPT", "PACK_VOTES"}


def strings_in(obj):
    """Every string anywhere inside a message (keys and values)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from strings_in(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings_in(v)


def run_game(seed: int, sock_dir: str, log_dir: str) -> dict[str, list[dict]]:
    mod = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "moderator.py"), *PLAYERS,
         "--dir", sock_dir, "--local", "--seed", str(seed), "--join-timeout", "15",
         "--night-timeout", "5", "--discussion", "1", "--vote-timeout", "3"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    logs = {p: os.path.join(log_dir, f"{seed}-{p}.jsonl") for p in PLAYERS}
    players = [
        subprocess.Popen(
            [sys.executable, os.path.join(HERE, "player.py"), p,
             "--dir", sock_dir, "--bot", "--log", logs[p]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        for p in PLAYERS
    ]
    mod_out, _ = mod.communicate(timeout=120)
    for proc in players:
        _, err = proc.communicate(timeout=10)
        assert proc.returncode == 0, f"player crashed:\n{err}"
    assert mod.returncode == 0, f"moderator failed:\n{mod_out}"

    received = {}
    for p, path in logs.items():
        with open(path) as f:
            received[p] = [json.loads(line) for line in f]
    return received


def check_villager(pid: str, msgs: list[dict]) -> None:
    own = [m for m in msgs if m["type"] == "ROLE_ASSIGN"]
    assert len(own) == 1, f"{pid}: expected 1 ROLE_ASSIGN, got {len(own)}"
    assert own[0]["you"] == pid and own[0]["role"] == "villager"
    assert "teammates" not in own[0], f"{pid}: villager was told about wolves"

    for m in msgs:
        if m is own[0] or m["type"] == "GAME_OVER":
            continue
        assert m["type"] not in PRIVATE_TYPES, f"{pid}: villager got {m['type']}: {m}"
        for s in strings_in(m):
            assert not ROLE_WORD.search(s), f"{pid}: role word {s!r} leaked in {m}"


def check_everyone(pid: str, msgs: list[dict]) -> None:
    """Rules that hold for every seat, whatever its role."""
    assigns = [m for m in msgs if m["type"] == "ROLE_ASSIGN"]
    assert len(assigns) == 1 and assigns[0]["you"] == pid, f"{pid}: bad ROLE_ASSIGN {assigns}"
    # Everyone -- dead or alive -- gets exactly one GAME_OVER, as the last message.
    assert msgs[-1]["type"] == "GAME_OVER", f"{pid}: last message was {msgs[-1]}"
    assert sum(m["type"] == "GAME_OVER" for m in msgs) == 1
    assert msgs[-1]["roles"][pid] == assigns[0]["role"], f"{pid}: reveal disagrees with ROLE_ASSIGN"
    for m in msgs:
        if m["type"] != "GAME_OVER":
            assert "roles" not in m, f"{pid}: role table outside GAME_OVER: {m}"


def self_test() -> None:
    """Make sure the checker would actually catch a leak."""
    assign = {"type": "ROLE_ASSIGN", "you": "p1", "role": "villager",
              "team": "village", "description": "", "players": PLAYERS}
    leaks = [
        {"type": "DAY_ANNOUNCE", "text": "p3 (the Seer) was killed"},
        {"type": "WEREWOLF_CHAT", "from": "p2", "text": "hi"},
        {"type": "INFO", "dead": {"p3": "doctor"}},
    ]
    for leak in leaks:
        try:
            check_villager("p1", [assign, leak])
        except AssertionError:
            continue
        raise SystemExit(f"self-test FAILED: checker missed {leak}")


def main() -> None:
    self_test()
    villagers_checked = 0
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:   # short path: AF_UNIX limit
        for seed in SEEDS:
            sock_dir = os.path.join(tmp, f"s{seed}")
            received = run_game(seed, sock_dir, tmp)
            for pid, msgs in received.items():
                check_everyone(pid, msgs)
                my_role = next(m["role"] for m in msgs if m["type"] == "ROLE_ASSIGN")
                if my_role == "villager":
                    check_villager(pid, msgs)
                    villagers_checked += 1
                    print(f"seed {seed}: villager {pid} OK ({len(msgs)} messages)")
    assert villagers_checked >= len(SEEDS), "test never saw a villager -- it proved nothing"
    print(f"PASS: {villagers_checked} villager logs, no role leaks")


if __name__ == "__main__":
    main()
