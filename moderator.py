"""
moderator.py -- the hub of the star: owns all game state, runs the phases.

The moderator is the only process that knows everyone's role. Players only
ever learn what the moderator chooses to send them, which is how roles stay
secret even though every message passes through one process.

    local dev:   python3 moderator.py p1 p2 p3 p4 --dir /tmp/ww --local
    over TLS:    python3 moderator.py p1 p2 p3 p4 --port 5555 --tls DIR [--host 0.0.0.0]
    real users:  sudo -u modbot python3 /opt/werewolf/moderator.py \\
                     --peercred player1 player2 player3 player4

Message routing summary (who receives what):
    ROLE_ASSIGN     1:1        each player, their own role only
    PHASE           broadcast  night / day / vote has started -- no secrets
    NIGHT_PROMPT    multicast  wolves (shared prompt, they see each other)
                    1:1        seer, doctor
    PACK_VOTES      multicast  wolves -- current kill choices
    WEREWOLF_CHAT   multicast  wolves -- relayed whisper
    SEER_RESULT     1:1        seer only
    DAY_ANNOUNCE    broadcast  who died overnight (names only, never roles)
    CHAT            multicast  all ALIVE players -- relayed day chat
    VOTE_TALLY      broadcast  everyone's vote and the result
    GAME_OVER       broadcast  winner + EVERY role (the only message that has them)
    INFO / error    1:1        whoever the notice concerns
"""

import argparse
import asyncio
import os
import random
import signal
import sys
from dataclasses import dataclass, field

import roles
from commslib import (DEFAULT_HEARTBEAT_INTERVAL, DEFAULT_HEARTBEAT_MISSES,
                      DEFAULT_TLS_DIR, Server)


def log(text: str) -> None:
    # The moderator's own console is the "god view" -- printing roles here
    # is fine; it never goes over a socket.
    print(f"[moderator] {text}", flush=True)


def log_rejected(pid: str | None, msg: dict) -> None:
    log(f"rejected a connection{f' for {pid}' if pid else ''}: {msg.get('reason')}")


@dataclass
class Timers:
    night: float = 45.0        # all night actions
    discussion: float = 60.0   # day chat before voting
    vote: float = 30.0         # voting


@dataclass
class Night:
    """Everything collected during one night. Thrown away at dawn."""
    number: int
    wolves: list[str]                      # alive (and connected) wolves
    targets: dict[str, list[str]]          # actor -> legal targets
    pending: set[str]                      # actors we're still waiting on
    kill_votes: dict[str, str] = field(default_factory=dict)
    peek: str | None = None
    heal: str | None = None


@dataclass
class Vote:
    """Everything collected during one day vote."""
    candidates: dict[str, list[str]]       # voter -> who they may vote for
    pending: set[str]                      # voters we're still waiting on
    votes: dict[str, str | None] = field(default_factory=dict)


class Game:
    def __init__(self, server: Server, players: list[str], seed: int | None,
                 timers: Timers):
        self.server = server
        self.players = players
        self.rng = random.Random(seed)
        self.timers = timers
        self.roles: dict[str, roles.Role] = {}
        self.alive: list[str] = list(players)
        self.last_healed: str | None = None
        self.phase = "lobby"   # "night" | "day" | "vote" -- decides what chat is allowed
        self.loop = asyncio.get_running_loop()
        self.phase_started = 0.0

    # --- helpers ------------------------------------------------------------

    def alive_with(self, role: roles.Role) -> list[str]:
        return [p for p in self.alive if self.roles[p] is role]

    def alive_connected(self) -> list[str]:
        connected = set(self.server.connected())
        return [p for p in self.alive if p in connected]

    async def tell(self, pid: str, msg: dict) -> None:
        """1:1 send that tolerates a player who has disconnected."""
        try:
            await self.server.send_to(pid, msg)
        except (KeyError, ConnectionError):
            log(f"could not reach {pid}")

    async def error(self, pid: str, text: str) -> None:
        await self.tell(pid, {"type": "error", "text": text})

    async def start_phase(self, phase: str, msg: dict) -> None:
        self.phase = phase
        self.phase_started = self.loop.time()
        await self.server.broadcast({"type": "PHASE", "phase": phase} | msg)

    # --- the one concurrency loop every phase uses ---------------------------

    async def message_loop(self, seconds: float, handle, is_done=lambda: False) -> bool:
        """
        Feed every incoming message, from any player, to `handle` until
        `is_done()` is true or `seconds` have passed. Returns True if the
        phase finished early, False if the deadline ended it.

        One deadline for the whole phase: each wait gets only the time
        that's *left*, so a stream of messages can't stretch the phase, and
        a silent player can delay it by at most `seconds`.
        """
        deadline = self.loop.time() + seconds
        while not is_done():
            remaining = deadline - self.loop.time()
            if remaining <= 0:
                return False
            try:
                pid, msg = await self.server.recv_any(timeout=remaining)
            except TimeoutError:
                return False
            if not await self.handle_common(pid, msg):
                await handle(pid, msg)
        return True

    async def handle_common(self, pid: str, msg: dict) -> bool:
        """
        Messages treated the same in every phase. Returns True if fully
        handled; False passes the message on to the phase's own handler.
        """
        kind = msg.get("type")
        if kind == "_connected":
            return True    # leftover join notice from the lobby
        if kind == "_rejected":
            log_rejected(pid, msg)
            return True
        if kind == "_disconnected":
            log(f"{pid} disconnected ({msg.get('reason')})")
            return False   # the phase may be waiting on them -- let it know
        if pid not in self.alive:
            await self.error(pid, "You're a spectator now -- you can't act or chat.")
            return True
        if kind == "CHAT":
            await self.relay_chat(pid, msg)
            return True
        if kind == "WEREWOLF_CHAT":
            await self.relay_wolf_chat(pid, msg)
            return True
        return False

    async def relay_chat(self, pid: str, msg: dict) -> None:
        if self.phase not in ("day", "vote"):
            await self.error(pid, "It's night -- chat opens at dawn.")
            return
        text = str(msg.get("text", ""))[:500]
        log(f"chat {pid} (+{self.loop.time() - self.phase_started:.1f}s): {text}")
        # Living players only: the dead are spectators of the public record
        # (announcements, tallies), not participants in the discussion.
        await self.server.multicast(self.alive_connected(), {
            "type": "CHAT", "from": pid, "text": text,
        })

    async def relay_wolf_chat(self, pid: str, msg: dict) -> None:
        if self.roles[pid] is not roles.WEREWOLF:
            # Phrased so it reveals nothing about who the wolves are.
            await self.error(pid, "You have no one to whisper to.")
            return
        if self.phase != "night":
            await self.error(pid, "Whispers only work at night.")
            return
        await self.server.multicast(self.alive_with(roles.WEREWOLF), {
            "type": "WEREWOLF_CHAT", "from": pid, "text": str(msg.get("text", ""))[:500],
        })

    # --- stage A: role assignment -----------------------------------------

    async def assign_roles(self) -> None:
        self.roles = roles.deal_roles(self.players, self.rng)
        for pid, role in self.roles.items():
            log(f"{pid} is {role.name}")

        wolves = [p for p, r in self.roles.items() if r.team == roles.WEREWOLVES]

        def role_message(pid: str) -> dict:
            role = self.roles[pid]
            msg = {
                "type": "ROLE_ASSIGN",
                "you": pid,
                "role": role.name,
                "team": role.team,
                "description": role.description,
                "players": self.players,
            }
            # Wolves (and only wolves) learn who the other wolf is.
            if role.team == roles.WEREWOLVES:
                msg["teammates"] = [w for w in wolves if w != pid]
            return msg

        # Each ROLE_ASSIGN is a separate 1:1 message -- a broadcast would
        # hand everyone's role to everyone. gather() sends them concurrently;
        # tell() so one unreachable player can't abort the deal for the rest.
        await asyncio.gather(
            *(self.tell(pid, role_message(pid)) for pid in self.players)
        )

    # --- stage B: night -----------------------------------------------------

    async def run_night(self, number: int) -> list[str]:
        """Prompt, collect actions until done or timed out, resolve. Returns the dead."""
        connected = set(self.server.connected())
        ctx = roles.NightContext(alive=list(self.alive), roles=self.roles,
                                 last_healed=self.last_healed)
        actors = [p for p in self.alive
                  if self.roles[p].night_action and p in connected]
        night = Night(
            number=number,
            wolves=[p for p in self.alive_with(roles.WEREWOLF) if p in connected],
            targets={p: self.roles[p].valid_targets(p, ctx) for p in actors},
            pending=set(actors),
        )

        log(f"--- night {number} --- acting: {sorted(actors)}")
        await self.start_phase("night", {
            "number": number,
            "text": f"Night {number} falls. Some players are acting in secret...",
        })
        await self.send_night_prompts(night)

        # THE night concurrency point: everyone with an action answers at
        # once; we take whatever arrives first, until all are done or the
        # deadline passes.
        finished = await self.message_loop(
            self.timers.night,
            lambda pid, msg: self.handle_night_message(night, pid, msg),
            is_done=lambda: not night.pending,
        )
        elapsed = self.loop.time() - self.phase_started
        if finished:
            log(f"night {number}: all actions in after {elapsed:.1f}s")
        else:
            # Default no-op for everyone who stayed silent.
            log(f"night {number}: timed out after {elapsed:.1f}s; "
                f"no-op for {sorted(night.pending)}")
            await asyncio.gather(*(
                self.tell(p, {"type": "INFO", "text": "Time's up -- you took no action tonight."})
                for p in night.pending
            ))
        return await self.resolve_night(night)

    async def send_night_prompts(self, night: Night) -> None:
        def prompt(pid: str) -> dict:
            return {
                "type": "NIGHT_PROMPT",
                "night": night.number,
                "action": self.roles[pid].night_action,
                "targets": night.targets[pid],
                "seconds": self.timers.night,
            }

        sends = []
        if night.wolves:
            # ONE multicast to the whole pack: same prompt, plus the list of
            # wolves, so they know who they're coordinating with.
            wolf_prompt = prompt(night.wolves[0]) | {"pack": night.wolves}
            sends.append(self.server.multicast(night.wolves, wolf_prompt))
        for pid in night.targets:
            if pid not in night.wolves:
                sends.append(self.tell(pid, prompt(pid)))   # seer / doctor: 1:1
        await asyncio.gather(*sends)

    async def handle_night_message(self, night: Night, pid: str, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "_disconnected":
            night.pending.discard(pid)
            if pid in night.wolves:
                night.wolves.remove(pid)
                # The remaining wolf may now count as "agreed".
                if roles.pack_agrees(night.kill_votes, night.wolves):
                    night.pending.difference_update(night.wolves)
        elif kind == "NIGHT_ACTION":
            await self.night_action(night, pid, msg)
        else:
            await self.error(pid, f"Can't do {kind!r} at night.")

    async def night_action(self, night: Night, pid: str, msg: dict) -> None:
        if pid not in night.targets:
            await self.error(pid, "You have nothing to do at night.")
            return
        target = msg.get("target")
        if target not in night.targets[pid]:
            await self.error(pid, f"{target!r} isn't a valid choice. "
                                  f"Options: {', '.join(night.targets[pid])}")
            return

        role = self.roles[pid]
        if role is roles.WEREWOLF:
            # Wolves may change their minds until they agree.
            night.kill_votes[pid] = target
            await self.server.multicast(night.wolves, {
                "type": "PACK_VOTES", "votes": dict(night.kill_votes),
            })
            if roles.pack_agrees(night.kill_votes, night.wolves):
                night.pending.difference_update(night.wolves)
                await self.server.multicast(night.wolves, {
                    "type": "INFO", "text": f"The pack agrees: {target}.",
                })
            else:
                # Could be a wolf changing its mind after agreement: keep
                # (or put back) the pack on the waiting list.
                night.pending.update(night.wolves)
            return

        # Seer and Doctor: first valid choice is final.
        if pid not in night.pending:
            await self.error(pid, "You've already acted tonight.")
            return
        night.pending.discard(pid)
        if role is roles.SEER:
            night.peek = target
            is_wolf = self.roles[target].team == roles.WEREWOLVES
            log(f"seer {pid} peeks at {target}: {'WOLF' if is_wolf else 'not a wolf'}")
            await self.tell(pid, {"type": "SEER_RESULT", "target": target, "is_werewolf": is_wolf})
        elif role is roles.DOCTOR:
            night.heal = target
            log(f"doctor {pid} protects {target}")
            await self.tell(pid, {"type": "INFO", "text": f"You are protecting {target} tonight."})

    async def resolve_night(self, night: Night) -> list[str]:
        if roles.pack_agrees(night.kill_votes, night.wolves):
            victim = next(iter(night.kill_votes.values()))
        else:
            victim = roles.pack_decision(night.kill_votes, self.rng)
        dead = roles.resolve_night(victim, night.heal)
        # A Doctor who didn't act leaves no restriction for tomorrow.
        self.last_healed = night.heal

        if victim and not dead:
            log(f"night {night.number}: wolves attacked {victim}, but the doctor saved them")
        log(f"night {night.number} result: dead={dead}")
        for p in dead:
            self.alive.remove(p)

        text = (f"Dawn breaks. {', '.join(dead)} did not survive the night."
                if dead else "Dawn breaks. Nobody died last night.")
        await self.server.broadcast({
            "type": "DAY_ANNOUNCE", "night": night.number, "dead": dead,
            "alive": list(self.alive), "text": text,
        })
        return dead

    # --- stage C: day -------------------------------------------------------

    async def run_day(self, number: int) -> str | None:
        """Discussion, then vote. Returns who was eliminated (or None)."""
        log(f"--- day {number} --- alive: {self.alive}")

        # 1. Discussion: relay chat as it arrives while the timer runs. No
        #    is_done -- discussion always lasts the full time.
        await self.start_phase("day", {
            "number": number, "seconds": self.timers.discussion,
            "text": f"Day {number}. Discuss for {self.timers.discussion:.0f}s, then vote.",
        })
        await self.message_loop(self.timers.discussion, self.handle_discussion_message)

        # 2. Voting: ends early once every living (connected) player has voted.
        voters = self.alive_connected()
        vote = Vote(
            candidates={v: [p for p in self.alive if p != v] for v in voters},
            pending=set(voters),
        )
        await self.start_phase("vote", {
            "number": number, "seconds": self.timers.vote, "candidates": list(self.alive),
            "text": f"Voting is open ({self.timers.vote:.0f}s). Most votes is eliminated; a tie spares everyone.",
        })
        finished = await self.message_loop(
            self.timers.vote,
            lambda pid, msg: self.handle_vote_message(vote, pid, msg),
            is_done=lambda: not vote.pending,
        )
        elapsed = self.loop.time() - self.phase_started
        if finished:
            log(f"day {number}: all votes in after {elapsed:.1f}s")
        else:
            log(f"day {number}: vote timed out after {elapsed:.1f}s; "
                f"abstaining: {sorted(vote.pending)}")
            for p in vote.pending:
                vote.votes[p] = None   # silence = abstain

        return await self.resolve_vote(number, vote)

    async def handle_discussion_message(self, pid: str, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "_disconnected":
            return
        if kind == "VOTE":
            await self.error(pid, "Voting hasn't opened yet -- keep talking.")
        else:
            await self.error(pid, f"Can't do {kind!r} during the day.")

    async def handle_vote_message(self, vote: Vote, pid: str, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "_disconnected":
            vote.pending.discard(pid)
            return
        if kind != "VOTE":
            await self.error(pid, f"Can't do {kind!r} during the vote.")
            return
        if pid not in vote.pending:
            await self.error(pid, "You've already voted.")
            return
        target = msg.get("target")   # None = abstain
        if target is not None and target not in vote.candidates[pid]:
            await self.error(pid, f"{target!r} isn't a valid vote. "
                                  f"Options: {', '.join(vote.candidates[pid])} (or none)")
            return
        vote.votes[pid] = target
        vote.pending.discard(pid)
        log(f"vote: {pid} -> {target}")
        await self.tell(pid, {"type": "INFO", "text":
                              f"You voted for {target}." if target else "You abstained."})

    async def resolve_vote(self, number: int, vote: Vote) -> str | None:
        counts, eliminated = roles.tally_votes(vote.votes)
        if eliminated:
            self.alive.remove(eliminated)
            text = f"The village has spoken: {eliminated} is eliminated."
        elif counts:
            text = "It's a tie -- nobody is eliminated today."
        else:
            text = "Nobody voted -- nobody is eliminated today."
        log(f"day {number} result: counts={counts} eliminated={eliminated}")
        # Broadcast, so spectators see the outcome too. Votes are public
        # (as in the tabletop game); roles are not.
        await self.server.broadcast({
            "type": "VOTE_TALLY", "day": number, "votes": vote.votes, "counts": counts,
            "eliminated": eliminated, "alive": list(self.alive), "text": text,
        })
        return eliminated

    # --- stage D: win checks and the end of the game --------------------------

    def winner(self) -> str | None:
        return roles.check_winner(self.alive, self.roles)

    async def game_over(self, winner: str | None, reason: str = "") -> None:
        if winner == roles.VILLAGE:
            text = "The village wins -- every wolf has been found."
        elif winner == roles.WEREWOLVES:
            text = "The werewolves win -- they now match the village."
        else:
            text = f"Game over, no winner: {reason}."
        log(f"GAME OVER: {text}")
        # The ONE message allowed to carry everyone's role. Broadcast, so
        # spectators get the reveal too.
        await self.server.broadcast({
            "type": "GAME_OVER", "winner": winner, "text": text,
            "roles": {p: self.roles[p].name for p in self.players},
            "alive": list(self.alive),
        })

    async def run(self, max_rounds: int) -> None:
        await self.assign_roles()
        for n in range(1, max_rounds + 1):
            await self.run_night(n)
            if self.winner():
                break
            await self.run_day(n)
            if self.winner():
                break
            if not self.server.connected():
                await self.game_over(None, "everyone left")
                return
        else:
            # Ran out of rounds without a winner (e.g. nobody ever died).
            await self.game_over(None, f"no result after {max_rounds} rounds")
            return
        await self.game_over(self.winner())


async def lobby(server: Server, players: list[str], timeout: float) -> bool:
    """
    Wait until every seat is connected at the same moment, logging joins,
    departures and refused connections as they happen. (Reading the inbox
    here, rather than waiting on Server.wait_for_players, is what makes a
    rejected certificate show up in the log while people are still joining.)
    Returns False if `timeout` runs out first.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # Who's here according to the events we've *read*, not server.connected():
    # that one runs ahead of the inbox, so it could say "full" while the last
    # joins are still unread and unlogged.
    joined: set[str] = set()
    while len(joined) < len(players):
        try:
            pid, msg = await server.recv_any(timeout=max(deadline - loop.time(), 0))
        except TimeoutError:
            return False
        kind = msg.get("type")
        if kind == "_connected":
            joined.add(pid)
            log(f"{pid} joined ({len(joined)}/{len(players)})")
        elif kind == "_disconnected":
            joined.discard(pid)
            log(f"{pid} left the lobby ({msg.get('reason')})")
        elif kind == "_rejected":
            log_rejected(pid, msg)
        else:
            try:
                await server.send_to(pid, {"type": "error", "text": "The game hasn't started yet."})
            except (KeyError, ConnectionError):
                pass
    return True


async def session(server: Server, args, holder: dict) -> int:
    """Lobby, then the game. Runs as one task so a signal can cancel it."""
    log(f"waiting for {args.players} (up to {args.join_timeout:.0f}s)")
    if not await lobby(server, args.players, args.join_timeout):
        missing = sorted(set(args.players) - set(server.connected()))
        log(f"gave up waiting for {missing}")
        return 1
    log("everyone is here")
    timers = Timers(args.night_timeout, args.discussion, args.vote_timeout)
    holder["game"] = game = Game(server, args.players, args.seed, timers)
    await game.run(args.max_rounds)
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("players", nargs="+")
    parser.add_argument("--dir", default="/run/werewolf")
    parser.add_argument("--port", type=int,
                        help="listen on TCP instead of UNIX sockets (0 = any free port)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="TCP address to listen on (default loopback only)")
    parser.add_argument("--tls", default=DEFAULT_TLS_DIR,
                        help=f"TLS files from make_certs.sh (default {DEFAULT_TLS_DIR})")
    parser.add_argument("--heartbeat", type=float, default=DEFAULT_HEARTBEAT_INTERVAL,
                        help="seconds between our heartbeats (default %(default)s)")
    parser.add_argument("--heartbeat-misses", type=int, default=DEFAULT_HEARTBEAT_MISSES,
                        help="drop a player silent for this many of its heartbeat "
                             "intervals (default %(default)s)")
    parser.add_argument("--peercred", action="store_true",
                        help="enforce SO_PEERCRED (player ids must be Linux users)")
    parser.add_argument("--seed", type=int, help="fix the role shuffle")
    parser.add_argument("--join-timeout", type=float, default=120.0)
    parser.add_argument("--night-timeout", type=float, default=Timers.night)
    parser.add_argument("--discussion", type=float, default=Timers.discussion)
    parser.add_argument("--vote-timeout", type=float, default=Timers.vote)
    parser.add_argument("--max-rounds", type=int, default=20,
                        help="end in a draw after this many night+day rounds")
    parser.add_argument("--local", action="store_true",
                        help="create per-seat dirs (dev only; setup_users.sh "
                             "does this for real users)")
    args = parser.parse_args()

    if not roles.MIN_PLAYERS <= len(args.players) <= roles.MAX_PLAYERS:
        print(f"need {roles.MIN_PLAYERS}-{roles.MAX_PLAYERS} players", file=sys.stderr)
        return 2
    if args.port is not None and (args.peercred or args.local):
        print("--peercred and --local are for UNIX sockets; drop them with --port",
              file=sys.stderr)
        return 2
    if args.local:
        for pid in args.players:
            os.makedirs(os.path.join(args.dir, pid), exist_ok=True)

    try:
        server = Server(heartbeat_interval=args.heartbeat,
                        heartbeat_misses=args.heartbeat_misses)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    try:
        await server.start(args.players, base_dir=args.dir, check_peer_uid=args.peercred,
                           host=args.host, port=args.port, tls_dir=args.tls)
    except OSError as e:   # e.g. TLS key missing or unreadable, port in use
        print(f"could not start: {e}", file=sys.stderr)
        return 2
    if server.port is not None:
        log(f"listening on TCP+TLS {args.host}:{server.port} (certificates from {args.tls})")

    holder: dict = {}
    task = asyncio.create_task(session(server, args, holder))
    # Ctrl-C (SIGINT) and `kill` (SIGTERM) both just cancel the session
    # task. Cancellation surfaces as CancelledError at whatever `await` the
    # game is sitting on, so the code below always gets to clean up --
    # without this, SIGTERM would kill us on the spot and leave socket
    # files behind.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)

    try:
        return await task
    except asyncio.CancelledError:
        log("shutdown requested")
        game = holder.get("game")
        if game and game.roles:
            try:
                # Tell everyone, but don't let a stuck socket stall shutdown.
                await asyncio.wait_for(game.game_over(None, "the moderator was shut down"), 3)
            except (TimeoutError, ConnectionError):
                pass
        return 1
    finally:
        await server.close()
        log("all sockets closed")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
