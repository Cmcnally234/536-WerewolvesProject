"""
player.py -- thin client: one seat at the table.

Two things happen at the same time, as two asyncio tasks:
    socket task    waits for moderator pushes and prints them immediately
    keyboard task  waits for lines you type and sends actions
Neither waits for the other, so a message from the moderator shows up even
while you're halfway through typing.

It connects to the moderator over TCP + mutual TLS (see commslib.py):

    local dev:   python3 player.py p1 --tls /tmp/pki            (./make_certs.sh -o /tmp/pki ...)
    real users:  sudo -u player1 python3 /opt/werewolf/player.py player1
    other hosts: ... --host <the moderator's address, as in its certificate>
"""

import argparse
import asyncio
import codecs
import json
import os
import random
import signal
import sys
import termios
import tty

from commslib import (DEFAULT_HEARTBEAT_INTERVAL, DEFAULT_HEARTBEAT_MISSES, DEFAULT_PORT,
                      DEFAULT_TLS_DIR, Client, ConnectionClosed)


# ---------------------------------------------------------------------------
# Terminal: async keyboard input that survives incoming messages
# ---------------------------------------------------------------------------

class Terminal:
    """
    Reads the keyboard without blocking the event loop, and prints messages
    without mangling a half-typed line.

    Normally the terminal is in "canonical" mode: the kernel buffers what you
    type and only hands over the whole line on Enter. We can't see that
    buffer, so we couldn't redraw it after printing a message over it. In
    "cbreak" mode the kernel hands us every keystroke immediately and stops
    echoing, so WE keep the line buffer (self.buf) and draw it ourselves.

    No thread is involved: loop.add_reader() asks the event loop to call
    _on_readable() whenever stdin has bytes -- the same readiness mechanism
    (epoll) that drives the socket.
    """

    PROMPT = "> "

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.interactive = sys.stdin.isatty()
        self.lines: asyncio.Queue[str | None] = asyncio.Queue()   # None = EOF
        self.buf = ""
        self._saved_mode = None
        self._in_escape = False   # inside an arrow-key/escape sequence
        # Keystrokes arrive as bytes; a multi-byte UTF-8 character may be
        # split across two reads, which an incremental decoder handles.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def start(self) -> None:
        if self.interactive:
            self._saved_mode = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)   # keys arrive one at a time; no echo; Ctrl-C still works
            self._write(self.PROMPT)
        asyncio.get_running_loop().add_reader(self.fd, self._on_readable)

    def stop(self) -> None:
        try:
            asyncio.get_running_loop().remove_reader(self.fd)
        except RuntimeError:
            pass   # loop already gone
        if self._saved_mode is not None:
            # Always put the terminal back, or the user's shell is left with
            # no echo after we exit.
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved_mode)
            self._saved_mode = None
            self._write("\n")

    async def readline(self) -> str | None:
        """Next complete line the user typed, or None on EOF / Ctrl-D."""
        return await self.lines.get()

    def print(self, text: str) -> None:
        """Print a message above the input line, then redraw the input line."""
        if self.interactive:
            # \r = back to column 0, \x1b[K = erase to end of line (ANSI).
            self._write("\r\x1b[K" + text + "\n" + self.PROMPT + self.buf)
        else:
            self._write(text + "\n")

    # --- internals --------------------------------------------------------

    def _write(self, s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    def _on_readable(self) -> None:
        data = os.read(self.fd, 1024)
        if not data:   # EOF (e.g. stdin was a pipe that closed)
            asyncio.get_running_loop().remove_reader(self.fd)
            self.lines.put_nowait(None)
            return
        for ch in self._decoder.decode(data):
            self._on_char(ch)

    def _on_char(self, ch: str) -> None:
        # Arrow/function keys send escape sequences like ESC [ A. We don't
        # support line editing, so swallow the whole sequence -- otherwise
        # the "[A" part would land in the line as text.
        if ch == "\x1b":
            self._in_escape = True
            return
        if self._in_escape:
            if ch.isalpha() or ch == "~":   # final byte of the sequence
                self._in_escape = False
            return

        if ch in "\r\n":
            line, self.buf = self.buf, ""
            if self.interactive:
                self._write("\n" + self.PROMPT)
            self.lines.put_nowait(line)
        elif ch in ("\x7f", "\b"):          # Backspace
            if self.buf:
                self.buf = self.buf[:-1]
                if self.interactive:
                    self._write("\b \b")
        elif ch == "\x04":                  # Ctrl-D on an empty line = quit
            if not self.buf:
                self.lines.put_nowait(None)
        elif ch == "\x15":                  # Ctrl-U = clear line
            self.buf = ""
            if self.interactive:
                self._write("\r\x1b[K" + self.PROMPT)
        elif ch.isprintable():
            self.buf += ch
            if self.interactive:
                self._write(ch)


# ---------------------------------------------------------------------------
# What this player knows, and how to show messages
# ---------------------------------------------------------------------------

class PlayerState:
    def __init__(self, name: str):
        self.name = name
        self.role: str | None = None
        self.team: str | None = None
        self.description = ""
        self.teammates: list[str] = []
        self.players: list[str] = []
        self.alive_players: list[str] = []
        self.prompt: dict | None = None        # current NIGHT_PROMPT, if any
        self.pack_votes: dict[str, str] = {}   # wolves only
        self.phase = "lobby"
        self.candidates: list[str] = []        # who I may vote for, while voting

    @property
    def is_alive(self) -> bool:
        return self.name in self.alive_players


def describe_role(state: PlayerState) -> str:
    if state.role is None:
        return "* No role yet."
    text = f"* You are {state.name}, the {state.role.upper()} (team: {state.team}).\n  {state.description}"
    if state.teammates:
        text += f"\n  Your fellow wolf: {', '.join(state.teammates)}"
    return text


def handle(msg: dict, state: PlayerState) -> str:
    """Update local state from a moderator message; return text to show."""
    kind = msg.get("type")

    if kind == "ROLE_ASSIGN":
        state.role = msg["role"]
        state.team = msg["team"]
        state.description = msg["description"]
        state.teammates = msg.get("teammates", [])
        state.players = msg["players"]
        state.alive_players = list(msg["players"])
        return f"* Players: {', '.join(state.players)}\n" + describe_role(state)

    if kind == "PHASE":
        state.phase = msg["phase"]
        state.prompt = None
        state.pack_votes = {}
        text = f"\n===== {msg['text']} ====="
        if not state.is_alive:
            return text + "\n  (you are spectating)"
        if state.phase == "day":
            text += "\n  Just type to talk to the other living players."
        elif state.phase == "vote":
            state.candidates = [p for p in msg["candidates"] if p != state.name]
            options = "  ".join(f"{i}) {t}" for i, t in enumerate(state.candidates, 1))
            text += f"\n  {options}\n  Type /vote <name or number>, or /vote none to abstain"
        return text

    if kind == "NIGHT_PROMPT":
        state.prompt = msg
        verb = msg["action"]
        options = "  ".join(f"{i}) {t}" for i, t in enumerate(msg["targets"], 1))
        text = (f"* Night {msg['night']}: choose someone to {verb.upper()} "
                f"({msg['seconds']:.0f}s).\n  {options}\n"
                f"  Type /{verb} <name or number>")
        if "pack" in msg:
            text += "\n  Whisper to your pack with /w <message>"
        return text

    if kind == "PACK_VOTES":
        state.pack_votes = msg["votes"]
        votes = ", ".join(f"{w} -> {t}" for w, t in msg["votes"].items())
        return f"[pack] votes: {votes}"

    if kind == "WEREWOLF_CHAT":
        return f"[pack] {msg['from']}: {msg['text']}"

    if kind == "SEER_RESULT":
        verdict = "IS a werewolf!" if msg["is_werewolf"] else "is not a werewolf."
        return f"* Your vision: {msg['target']} {verdict}"

    if kind == "DAY_ANNOUNCE":
        state.prompt = None
        state.alive_players = msg["alive"]
        text = f"\n===== {msg['text']} =====\n* Alive: {', '.join(msg['alive'])}"
        if state.name in msg["dead"]:
            text += "\n* You are dead. You'll keep watching, but can't act or chat."
        return text

    if kind == "CHAT":
        return f"<{msg['from']}> {msg['text']}"

    if kind == "VOTE_TALLY":
        state.alive_players = msg["alive"]
        lines = [f"\n===== {msg['text']} ====="]
        for voter, target in msg["votes"].items():
            lines.append(f"  {voter} -> {target or '(abstained)'}")
        lines.append(f"* Alive: {', '.join(msg['alive'])}")
        if msg["eliminated"] == state.name:
            lines.append("* You were voted out. You'll keep watching, but can't act or chat.")
        return "\n".join(lines)

    if kind == "GAME_OVER":
        lines = [f"\n===== {msg['text']} =====", "  Roles:"]
        for pid, role in msg["roles"].items():
            status = "alive" if pid in msg["alive"] else "dead"
            me = "  <- you" if pid == state.name else ""
            lines.append(f"    {pid:<10} {role:<9} ({status}){me}")
        if msg["winner"]:
            won = msg["winner"] == state.team
            lines.append(f"* {'You WON!' if won else 'You lost.'}")
        return "\n".join(lines)

    if kind == "_reconnecting":
        return (f"* Lost the link to the moderator ({msg['reason']}). "
                f"Reconnecting for up to {msg['resume_timeout']:g}s; "
                "anything you type is sent when we're back.")
    if kind == "_resumed":
        return (f"* Reconnected after {msg['away']}s. "
                f"Catching up on {msg['replaying']} missed message(s):")
    if kind == "INFO":
        return f"* {msg['text']}"
    if kind == "error":
        return f"! {msg.get('text')}"
    return f"? unhandled message: {msg}"


HELP = """\
commands:
  /kill /peek /heal <name|number>   your night action (when prompted)
  /w <message>                      whisper to the pack (wolves only, at night)
  /vote <name|number|none>          your vote (when voting is open)
  <anything else>                   chat (daytime, living players)
  /role                             show your role again
  /help                             this text
  /quit                             leave the game"""

NIGHT_VERBS = {"/kill": "kill", "/peek": "peek", "/heal": "heal"}


def parse_target(arg: str, targets: list[str]) -> str | None:
    """Accept either a name ("p3") or its number in the prompt list ("2")."""
    if arg in targets:
        return arg
    if arg.isdigit() and 1 <= int(arg) <= len(targets):
        return targets[int(arg) - 1]
    return None


# ---------------------------------------------------------------------------
# Bot: plays automatically, for tests (--bot act | --bot silent)
# ---------------------------------------------------------------------------

class Bot:
    def __init__(self, client: Client, state: PlayerState, mode: str, think: float = 0.0):
        self.client = client
        self.state = state
        self.mode = mode
        # Extra seconds before every action, on top of the short random
        # pause. Gives a demo (or a fault-injection test) a predictable
        # window between "the prompt arrived" and "the bot answered".
        self.think = think
        self.rng = random.Random()

    async def react(self, msg: dict) -> None:
        if self.mode == "silent" or not self.state.is_alive:
            return   # silent: exercises the moderator's timeout path
        kind = msg.get("type")
        if kind == "PHASE" and msg["phase"] == "day":
            await asyncio.sleep(self.think + self.rng.uniform(0.0, 0.3))
            suspect = self.rng.choice([p for p in self.state.alive_players if p != self.state.name])
            await self.client.send({"type": "CHAT", "text": f"I don't trust {suspect}."})
            return
        if kind == "PHASE" and msg["phase"] == "vote":
            await asyncio.sleep(self.think + self.rng.uniform(0.1, 0.4))
            # Wolves never vote for their packmate; everyone else votes at random.
            options = [p for p in self.state.candidates if p not in self.state.teammates]
            await self.client.send({"type": "VOTE", "target": self.rng.choice(options) if options else None})
            return
        if kind == "NIGHT_PROMPT":
            # Think for a moment, like a human would.
            await asyncio.sleep(self.think + self.rng.uniform(0.1, 0.6))
            target = self.rng.choice(msg["targets"])
            if "pack" in msg:
                # If a packmate already voted, back them up.
                target = next(iter(self.state.pack_votes.values()), target)
                await self.client.send({"type": "WEREWOLF_CHAT", "text": f"I'm going for {target}"})
            await self.client.send({"type": "NIGHT_ACTION", "target": target})
        elif kind == "PACK_VOTES" and self.state.prompt:
            votes = msg["votes"]
            if self.state.name in votes and len(set(votes.values())) > 1:
                # Disagreement: everyone follows the alphabetically-first
                # wolf, so two bots can't keep swapping votes forever.
                leader = min(votes)
                if leader != self.state.name and votes[self.state.name] != votes[leader]:
                    await self.client.send({"type": "NIGHT_ACTION", "target": votes[leader]})


# ---------------------------------------------------------------------------
# The two concurrent tasks
# ---------------------------------------------------------------------------

async def socket_loop(client: Client, term: Terminal, state: PlayerState,
                      log_path: str | None, bot: Bot | None):
    """Print every moderator message the moment it arrives."""
    log_file = open(log_path, "a") if log_path else None
    bot_tasks = set()
    try:
        while True:
            try:
                msg = await client.recv()
            except ConnectionClosed as e:
                term.print(f"* Lost the connection to the moderator: {e}")
                return
            if log_file:
                # Raw record of everything received; the role-leak test reads it.
                log_file.write(json.dumps(msg) + "\n")
                log_file.flush()
            term.print(handle(msg, state))
            if msg.get("type") == "GAME_OVER":
                return   # nothing more will come; leave cleanly
            if bot:
                # Run the bot's reaction as its own task so its "thinking"
                # sleep never stops this loop from reading the socket.
                task = asyncio.create_task(bot.react(msg))
                bot_tasks.add(task)
                task.add_done_callback(bot_tasks.discard)
    finally:
        for task in bot_tasks:
            task.cancel()
        if log_file:
            log_file.close()


async def keyboard_loop(client: Client, term: Terminal, state: PlayerState):
    """Turn typed lines into actions."""
    while True:
        line = await term.readline()
        if line is None:
            return
        line = line.strip()
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        cmd, rest = cmd.lower(), rest.strip()

        if cmd == "/quit":
            return
        elif cmd == "/help":
            term.print(HELP)
        elif cmd == "/role":
            term.print(describe_role(state))
        elif cmd in NIGHT_VERBS:
            # Quick local checks for a friendly message; the moderator still
            # validates everything (a client can't be trusted).
            if state.prompt is None:
                term.print("* You have nothing to do right now.")
            elif NIGHT_VERBS[cmd] != state.prompt["action"]:
                term.print(f"* Tonight you /{state.prompt['action']}, not {cmd}.")
            else:
                target = parse_target(rest, state.prompt["targets"])
                if target is None:
                    term.print(f"* Pick one of: {', '.join(state.prompt['targets'])}")
                else:
                    await client.send({"type": "NIGHT_ACTION", "target": target})
        elif cmd == "/vote":
            if state.phase != "vote":
                term.print("* Voting isn't open.")
            elif rest.lower() in ("none", "abstain"):
                await client.send({"type": "VOTE", "target": None})
            else:
                target = parse_target(rest, state.candidates)
                if target is None:
                    term.print(f"* Vote for one of: {', '.join(state.candidates)} (or none)")
                else:
                    await client.send({"type": "VOTE", "target": target})
        elif cmd == "/w":
            await client.send({"type": "WEREWOLF_CHAT", "text": rest})
        elif cmd.startswith("/"):
            term.print("* Unknown command -- type /help")
        else:
            await client.send({"type": "CHAT", "text": line})


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="your seat / player id, e.g. player1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="the moderator's TCP port (default %(default)s)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="the moderator's address; must be a name or IP in its certificate")
    parser.add_argument("--tls", default=DEFAULT_TLS_DIR,
                        help=f"TLS files from make_certs.sh (default {DEFAULT_TLS_DIR})")
    parser.add_argument("--heartbeat", type=float, default=DEFAULT_HEARTBEAT_INTERVAL,
                        help="seconds between our heartbeats (default %(default)s)")
    parser.add_argument("--heartbeat-misses", type=int, default=DEFAULT_HEARTBEAT_MISSES,
                        help="give up on a moderator silent for this many of its "
                             "heartbeat intervals (default %(default)s)")
    parser.add_argument("--log", help="append every received message (JSON lines) to this file")
    parser.add_argument("--bot-think", type=float, default=0.0, metavar="SECONDS",
                        help="bots: wait this much longer before every action")
    parser.add_argument("--bot", nargs="?", const="act", choices=["act", "silent"],
                        help="no keyboard; play automatically (act) or never respond (silent)")
    parser.add_argument("--wait", type=float, default=30.0,
                        help="seconds to keep retrying the connection")
    args = parser.parse_args()

    try:
        client = Client(heartbeat_interval=args.heartbeat,
                        heartbeat_misses=args.heartbeat_misses)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    try:
        await client.connect(player_id=args.name, host=args.host, port=args.port,
                             tls_dir=args.tls, retry_seconds=args.wait)
    except OSError as e:
        # A connect timeout has an empty message; say what it usually means.
        reason = str(e) or (f"no answer from {args.host}:{args.port} within the connect timeout "
                            "(wrong address, or a firewall silently dropping the port)")
        print(f"could not connect: {reason}", file=sys.stderr)
        return 1

    state = PlayerState(args.name)
    term = Terminal()
    bot = Bot(client, state, args.bot, args.bot_think) if args.bot else None
    tasks = [asyncio.create_task(socket_loop(client, term, state, args.log, bot))]
    if not bot:
        term.start()
        tasks.append(asyncio.create_task(keyboard_loop(client, term, state)))

    # SIGTERM (e.g. `kill`) or SIGHUP (terminal window closed): cancel our
    # tasks so the finally-block below still restores the terminal and
    # closes the socket. (Ctrl-C is already handled by asyncio.run.)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, lambda: [t.cancel() for t in tasks])

    try:
        # Run both tasks; stop as soon as EITHER finishes (moderator hung up,
        # or the user typed /quit). gather() would wait for BOTH, and the
        # keyboard task never finishes on its own if the moderator leaves.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            if not t.cancelled():
                t.result()   # re-raise any crash instead of hiding it
    finally:
        term.stop()
        await client.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
