"""
roles.py -- what each role IS and what it may do at night.

No networking and no game loop live here; moderator.py owns the state
machine and asks this module questions like "who may this player target?".
Keeping the rules apart from the plumbing means each can be read (and
explained) on its own.
"""

import random
from dataclasses import dataclass

VILLAGE = "village"
WEREWOLVES = "werewolves"

MIN_PLAYERS = 4
MAX_PLAYERS = 6


@dataclass
class NightContext:
    """The slice of game state a role needs to decide its legal targets."""
    alive: list[str]
    roles: dict[str, "Role"]
    last_healed: str | None = None   # who the Doctor protected last night


class Role:
    name = ""
    team = VILLAGE
    night_action: str | None = None   # verb shown to the player, e.g. "kill"
    description = ""

    def valid_targets(self, actor: str, ctx: NightContext) -> list[str]:
        """Players `actor` may pick tonight. Empty = no night action."""
        return []


class Werewolf(Role):
    name = "werewolf"
    team = WEREWOLVES
    night_action = "kill"
    description = ("Each night, agree with your fellow wolf on one villager to "
                   "kill. By day, blend in.")

    def valid_targets(self, actor, ctx):
        # Wolves can't kill each other.
        return [p for p in ctx.alive if ctx.roles[p].team != WEREWOLVES]


class Seer(Role):
    name = "seer"
    night_action = "peek"
    description = ("Each night, pick one player and learn whether they are a "
                   "werewolf.")

    def valid_targets(self, actor, ctx):
        return [p for p in ctx.alive if p != actor]


class Doctor(Role):
    name = "doctor"
    night_action = "heal"
    description = ("Each night, protect one player (yourself included) from "
                   "the wolves -- but never the same player two nights running.")

    def valid_targets(self, actor, ctx):
        return [p for p in ctx.alive if p != ctx.last_healed]


class Villager(Role):
    name = "villager"
    description = "No night power. Find and vote out the wolves by day."


# One shared instance per role: roles carry no per-player state.
WEREWOLF, SEER, DOCTOR, VILLAGER = Werewolf(), Seer(), Doctor(), Villager()
ROLES = {r.name: r for r in (WEREWOLF, SEER, DOCTOR, VILLAGER)}   # lookup by name


def deal_roles(player_ids: list[str], rng: random.Random | None = None) -> dict[str, Role]:
    """2 werewolves, 1 seer, 1 doctor, everyone else a villager."""
    n = len(player_ids)
    if not MIN_PLAYERS <= n <= MAX_PLAYERS:
        raise ValueError(f"need {MIN_PLAYERS}-{MAX_PLAYERS} players, got {n}")
    if len(set(player_ids)) != n:
        raise ValueError("duplicate player ids")

    deck = [WEREWOLF, WEREWOLF, SEER, DOCTOR] + [VILLAGER] * (n - 4)
    # A seeded Random makes a game reproducible (handy when testing).
    (rng or random.Random()).shuffle(deck)
    return dict(zip(player_ids, deck))


# ---------------------------------------------------------------------------
# Resolving the night
# ---------------------------------------------------------------------------

def pack_agrees(kill_votes: dict[str, str], wolves: list[str]) -> bool:
    """True once every listed wolf has voted, all for the same target."""
    return (bool(wolves)
            and all(w in kill_votes for w in wolves)
            and len({kill_votes[w] for w in wolves}) == 1)


def pack_decision(kill_votes: dict[str, str], rng: random.Random) -> str | None:
    """
    The wolves' kill if time ran out before they agreed: the most-voted
    target, ties broken at random. No votes at all means no kill -- a silent
    pack is a no-op, just like any other silent player.
    """
    if not kill_votes:
        return None
    counts: dict[str, int] = {}
    for target in kill_votes.values():
        counts[target] = counts.get(target, 0) + 1
    best = max(counts.values())
    tied = sorted(t for t, c in counts.items() if c == best)
    return rng.choice(tied)


def resolve_night(victim: str | None, healed: str | None) -> list[str]:
    """Who dies: the wolves' victim, unless the Doctor protected them."""
    if victim is None or victim == healed:
        return []
    return [victim]


# ---------------------------------------------------------------------------
# Resolving the day vote
# ---------------------------------------------------------------------------

def tally_votes(votes: dict[str, str | None]) -> tuple[dict[str, int], str | None]:
    """
    votes: voter -> target (None = abstained).
    Returns (counts, eliminated). The single most-voted player is eliminated;
    a tie for the top spot -- or no votes at all -- eliminates nobody.
    """
    counts: dict[str, int] = {}
    for target in votes.values():
        if target is not None:
            counts[target] = counts.get(target, 0) + 1
    if not counts:
        return counts, None
    best = max(counts.values())
    leaders = [t for t, c in counts.items() if c == best]
    return counts, (leaders[0] if len(leaders) == 1 else None)


# ---------------------------------------------------------------------------
# Win conditions
# ---------------------------------------------------------------------------

def check_winner(alive: list[str], roles: dict[str, Role]) -> str | None:
    """
    VILLAGE once no wolf is alive. WEREWOLVES once living wolves are at least
    as many as everyone else (from then on they can't be outvoted by day).
    None while the game goes on.
    """
    wolves = sum(1 for p in alive if roles[p].team == WEREWOLVES)
    others = len(alive) - wolves
    if wolves == 0:
        return VILLAGE
    if wolves >= others:
        return WEREWOLVES
    return None
