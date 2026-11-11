"""
test_day.py -- the day phase: chat relay, voting, ties, spectators.

    python3 test_day.py

Part 1 runs the real Game code in-process against a FakeServer that records
everything sent, so vote outcomes are fully deterministic.
Part 2 runs a real multi-process game to check chat is relayed live,
during the discussion timer, and never reaches dead players.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile

import roles
from moderator import Game, Timers
from testnet import net_args

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYERS = ["p1", "p2", "p3", "p4", "p5"]


# ---------------------------------------------------------------------------
# Part 1: in-process, deterministic
# ---------------------------------------------------------------------------

class FakeServer:
    """Stands in for commslib.Server: same methods, no sockets."""

    def __init__(self, players):
        self.players = players
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[tuple[str, dict]] = []   # (recipient, message)

    def connected(self):
        return list(self.players)

    async def send_to(self, pid, msg):
        self.sent.append((pid, msg))

    async def multicast(self, pids, msg):
        for pid in pids:
            await self.send_to(pid, msg)
        return []

    async def broadcast(self, msg):
        return await self.multicast(self.players, msg)

    async def recv_any(self, timeout=None):
        return await asyncio.wait_for(self.inbox.get(), timeout)

    # test helpers
    def push(self, pid, msg):
        self.inbox.put_nowait((pid, msg))

    def to(self, pid, kind):
        return [m for p, m in self.sent if p == pid and m["type"] == kind]

    async def wait_for_phase(self, phase):
        while not any(m["type"] == "PHASE" and m["phase"] == phase for _, m in self.sent):
            await asyncio.sleep(0.01)


def make_game(server, vote_timeout=1.0) -> Game:
    game = Game(server, PLAYERS, seed=0, timers=Timers(night=1, discussion=0.3, vote=vote_timeout))
    game.roles = {"p1": roles.WEREWOLF, "p2": roles.WEREWOLF, "p3": roles.SEER,
                  "p4": roles.DOCTOR, "p5": roles.VILLAGER}
    game.alive = ["p1", "p2", "p3", "p4"]   # p5 died last night
    return game


async def test_majority_and_spectator():
    server = FakeServer(PLAYERS)
    game = make_game(server)

    async def script():
        server.push("p5", {"type": "CHAT", "text": "boo"})          # dead: rejected
        server.push("p1", {"type": "CHAT", "text": "hello"})        # relayed to alive
        server.push("p2", {"type": "VOTE", "target": "p3"})         # too early
        await server.wait_for_phase("vote")
        server.push("p1", {"type": "VOTE", "target": "p3"})
        server.push("p2", {"type": "VOTE", "target": "p3"})
        server.push("p3", {"type": "VOTE", "target": "p1"})
        server.push("p3", {"type": "VOTE", "target": "p2"})         # second vote: rejected
        server.push("p5", {"type": "VOTE", "target": "p1"})         # dead: rejected
        server.push("p4", {"type": "VOTE", "target": None})         # abstain

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(script())
    eliminated = await game.run_day(1)
    await task
    vote_time = loop.time() - game.phase_started

    assert eliminated == "p3", eliminated
    assert game.alive == ["p1", "p2", "p4"]
    assert vote_time < 0.5, f"vote should end as soon as all 4 voted, took {vote_time:.2f}s"

    chats = {p: server.to(p, "CHAT") for p in PLAYERS}
    assert all(len(chats[p]) == 1 for p in ["p1", "p2", "p3", "p4"]), chats
    assert chats["p5"] == [], "dead player received live chat"
    assert any("spectator" in m["text"] for m in server.to("p5", "error"))
    assert any("hasn't opened" in m["text"] for m in server.to("p2", "error"))
    assert any("already voted" in m["text"] for m in server.to("p3", "error"))

    tally = server.to("p5", "VOTE_TALLY")   # spectators DO get the tally (broadcast)
    assert len(tally) == 1 and tally[0]["counts"] == {"p3": 2, "p1": 1}
    assert "p5" not in tally[0]["votes"], "dead player's vote was counted"
    print(f"PASS majority: p3 out 2-1, vote ended after {vote_time:.2f}s; "
          f"dead p5 got no chat, couldn't chat or vote, but saw the tally")


async def test_tie_and_timeout():
    server = FakeServer(PLAYERS)
    game = make_game(server, vote_timeout=0.5)

    async def script():
        await server.wait_for_phase("vote")
        server.push("p2", {"type": "VOTE", "target": "p2"})         # self-vote: rejected
        server.push("p1", {"type": "VOTE", "target": "p3"})
        server.push("p3", {"type": "VOTE", "target": "p1"})
        # p2 and p4 never vote -> abstain at the deadline

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(script())
    eliminated = await game.run_day(1)
    await task
    vote_time = loop.time() - game.phase_started

    assert eliminated is None
    assert game.alive == ["p1", "p2", "p3", "p4"]
    assert vote_time >= 0.5, "should have waited for the deadline"
    tally = server.to("p1", "VOTE_TALLY")[0]
    assert tally["votes"] == {"p1": "p3", "p3": "p1", "p2": None, "p4": None}, tally["votes"]
    assert "tie" in tally["text"]
    assert any("isn't a valid vote" in m["text"] for m in server.to("p2", "error"))
    print(f"PASS tie 1-1 -> nobody eliminated; silent p2/p4 abstained at the "
          f"{vote_time:.2f}s deadline; self-vote rejected")


def test_tally_rules():
    assert roles.tally_votes({}) == ({}, None)
    assert roles.tally_votes({"a": None, "b": None}) == ({}, None)
    assert roles.tally_votes({"a": "c", "b": "c", "c": "a"})[1] == "c"
    assert roles.tally_votes({"a": "b", "b": "a"})[1] is None                 # tie
    assert roles.tally_votes({"a": "c", "b": None, "c": None})[1] == "c"      # plurality
    print("PASS tally_votes rules")


# ---------------------------------------------------------------------------
# Part 2: real processes -- chat is relayed live, during the timer
# ---------------------------------------------------------------------------

def test_live_chat_relay():
    # 6 players: with 5, one night kill makes it 2 wolves vs 2 and the game
    # ends before day 1 ever starts.
    players = PLAYERS + ["p6"]
    discussion = 2.0
    with tempfile.TemporaryDirectory(prefix="ww-") as tmp:
        mod_net, player_net = net_args(os.path.join(tmp, "s"))
        mod = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "moderator.py"), *players, *mod_net,
             "--seed", "5", "--max-rounds", "1", "--night-timeout", "3",
             "--discussion", str(discussion), "--vote-timeout", "3"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        logs = {p: os.path.join(tmp, f"{p}.jsonl") for p in players}
        procs = [subprocess.Popen(
            [sys.executable, os.path.join(HERE, "player.py"), p, *player_net,
             "--bot", "--log", logs[p]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            for p in players]
        out, _ = mod.communicate(timeout=60)
        for proc in procs:
            proc.wait(timeout=10)
        received = {p: [json.loads(l) for l in open(path)] for p, path in logs.items()}

    # Relay times as logged by the moderator: "chat p3 (+0.2s): ..."
    times = [float(t) for t in re.findall(r"chat p\d \(\+([\d.]+)s\)", out)]
    assert times, "no chat was relayed:\n" + out
    assert max(times) < discussion, f"chat relayed after the timer? {times}"

    dead = next(m["dead"] for m in received["p1"] if m["type"] == "DAY_ANNOUNCE")
    for p, msgs in received.items():
        kinds = [m["type"] for m in msgs]
        chat_idx = [i for i, k in enumerate(kinds) if k == "CHAT"]
        vote_idx = next(i for i, m in enumerate(msgs) if m["type"] == "PHASE" and m["phase"] == "vote")
        if p in dead:
            assert not chat_idx, f"dead {p} received chat"
            assert "VOTE_TALLY" in kinds, f"dead {p} should still see the tally"
        else:
            assert chat_idx and min(chat_idx) < vote_idx, f"{p} got no chat before voting"
    print(f"PASS live relay: {len(times)} chats relayed at "
          f"{', '.join(f'+{t:.1f}s' for t in sorted(times))} of a {discussion:.0f}s discussion; "
          f"dead {dead or '(nobody died)'} got none")


if __name__ == "__main__":
    test_tally_rules()
    asyncio.run(test_majority_and_spectator())
    asyncio.run(test_tie_and_timeout())
    test_live_chat_relay()
    print("all day tests passed")
