#!/usr/bin/env python3
"""boss-call — one Boss, several members, one line each.

A room is a star: exactly one Boss and any number of members. A member talks
only to the Boss; the Boss talks to one member or to all. That is the whole
relationship, and the CLI enforces it rather than describing it.

Any agent that can run a shell command can be the Boss or a member: Claude
Code, Codex, kiso, a script. The harness reads one SKILL.md and calls this
command; `boss-call who` tells it which side it is on.

State lives outside every repository:

    ~/.model-boss/boss-call/<room>/
        room.json        {"boss": {"name", "root"?}, "members": {name: {"root"}}}
        messages.jsonl   {seq, ts, from, to, kind, text, ref?}   append-only
        cursors/<name>   last seq that member has acknowledged
        .lock            flock for append + cursor moves

A message is never an authorization. Spending money, pushing, merging and
deploying stay with the person at the terminal, whoever sent the message.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("BOSS_CALL_HOME", Path.home() / ".model-boss" / "boss-call"))
DEFAULT_ROOM = os.environ.get("BOSS_CALL_ROOM", "default")
KINDS = ("msg", "ask", "reply", "status")


# ----------------------------------------------------------------------------
# storage
# ----------------------------------------------------------------------------


def room_dir(room: str) -> Path:
    d = HOME / room
    d.mkdir(parents=True, exist_ok=True)
    (d / "cursors").mkdir(exist_ok=True)
    return d


class Locked:
    def __init__(self, room: str) -> None:
        self.path = room_dir(room) / ".lock"

    def __enter__(self) -> "Locked":
        self.fh = open(self.path, "a+")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def load_room(room: str) -> dict:
    p = room_dir(room) / "room.json"
    if not p.exists():
        return {"boss": None, "members": {}}
    return json.loads(p.read_text())


def save_room(room: str, data: dict) -> None:
    (room_dir(room) / "room.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_messages(room: str) -> list[dict]:
    p = room_dir(room) / "messages.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # torn tail from a crash mid-write: skip, never guess
    return out


def get_cursor(room: str, name: str) -> int:
    p = room_dir(room) / "cursors" / name
    if not p.exists():
        return -1
    try:
        return int(p.read_text().strip())
    except ValueError:
        return -1


def set_cursor(room: str, name: str, seq: int) -> None:
    (room_dir(room) / "cursors" / name).write_text(f"{seq}\n")


# ----------------------------------------------------------------------------
# identity — who am I, and which side of the line
# ----------------------------------------------------------------------------


def _root_contains(root: str | None, here: Path) -> int:
    """Length of the matching root, or -1. Longest match wins."""
    if not root:
        return -1
    r = Path(root).expanduser().resolve()
    try:
        here.relative_to(r)
    except ValueError:
        return -1
    return len(str(r))


def identify(room: str, explicit: str | None, cwd: str | None = None) -> tuple[str, str]:
    """Returns (name, role) with role in {'boss', 'member'}.
    Order: --me, $BOSS_CALL_ME, then the registered root that contains the cwd."""
    data = load_room(room)
    boss = data.get("boss") or {}
    members = data.get("members", {})
    name = explicit or os.environ.get("BOSS_CALL_ME")
    if name:
        if boss.get("name") == name:
            return name, "boss"
        if name in members:
            return name, "member"
        sys.exit(f"boss-call: {name!r} is not in room {room!r} (boss: {boss.get('name')}, members: {', '.join(members) or 'none'})")
    here = Path(cwd or os.getcwd()).resolve()
    best: tuple[int, str, str] | None = None
    for n, m in members.items():
        k = _root_contains(m.get("root"), here)
        if k >= 0 and (best is None or k > best[0]):
            best = (k, n, "member")
    k = _root_contains(boss.get("root"), here)
    if k >= 0 and (best is None or k > best[0]):
        best = (k, boss["name"], "boss")
    if best is None:
        sys.exit(
            "boss-call: cannot tell who you are — pass --me <name>, set BOSS_CALL_ME, "
            "or run from inside a registered repo. Run `boss-call who --room " + room + "` to see the room."
        )
    return best[1], best[2]


# ----------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------


def cmd_init(a: argparse.Namespace) -> int:
    data = load_room(a.room)
    if a.boss:
        name, _, root = a.boss.partition("=")
        data["boss"] = {"name": name, **({"root": str(Path(root).expanduser().resolve())} if root else {})}
    for spec in a.member:
        if "=" not in spec:
            sys.exit(f"--member wants name=/repo/root, got {spec!r}")
        name, root = spec.split("=", 1)
        data.setdefault("members", {})[name] = {"root": str(Path(root).expanduser().resolve())}
    if not data.get("boss"):
        sys.exit("boss-call: a room needs exactly one boss: --boss <name>[=/root]")
    save_room(a.room, data)
    return cmd_who(argparse.Namespace(room=a.room, me=data["boss"]["name"]))


def cmd_who(a: argparse.Namespace) -> int:
    data = load_room(a.room)
    boss = data.get("boss") or {}
    members = data.get("members", {})
    try:
        name, role = identify(a.room, a.me)
    except SystemExit:
        name, role = "?", "unknown"
    print(f"room    : {a.room}  ({room_dir(a.room)})")
    print(f"you     : {name}  ({role})")
    print(f"boss    : {boss.get('name', '-')}" + (f"  root={boss['root']}" if boss.get("root") else ""))
    print("members :")
    for n, m in members.items():
        print(f"  {n:12s} {m.get('root', '')}")
    if role == "member":
        print(f"\nyou talk only to {boss.get('name')}: `boss-call post \"...\"` goes there by default.")
    elif role == "boss":
        print("\nyou may `post --to <member>` or `--to all`; members can only reach you.")
    return 0


def cmd_post(a: argparse.Namespace) -> int:
    text = a.text if a.text is not None else sys.stdin.read()
    text = text.rstrip("\n")
    if not text.strip():
        sys.exit("boss-call: empty message")
    data = load_room(a.room)
    boss_name = (data.get("boss") or {}).get("name")
    sender, role = identify(a.room, a.sender)
    if role == "member":
        to = a.to or boss_name
        if to != boss_name:
            sys.exit(f"boss-call: a member talks only to the boss ({boss_name}); --to {to!r} refused")
    else:
        if not a.to:
            sys.exit("boss-call: the boss must say --to <member> or --to all")
        to = a.to
        if to != "all" and to not in data.get("members", {}):
            sys.exit(f"boss-call: {to!r} is not a member (members: {', '.join(data.get('members', {})) or 'none'})")
    with Locked(a.room):
        msgs = read_messages(a.room)
        seq = (msgs[-1]["seq"] + 1) if msgs else 0
        rec = {
            "seq": seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "from": sender,
            "to": to,
            "kind": a.kind,
            "text": text,
        }
        if a.ref is not None:
            rec["ref"] = a.ref
        with open(room_dir(a.room) / "messages.jsonl", "a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    print(f"posted #{seq} {sender} -> {to} [{a.kind}]")
    return 0


def _addressed(m: dict, me: str) -> bool:
    return m["to"] in (me, "all") and m["from"] != me


def cmd_read(a: argparse.Namespace) -> int:
    me, role = identify(a.room, a.me)
    msgs = read_messages(a.room)
    cur = get_cursor(a.room, me)
    unread = [m for m in msgs if m["seq"] > cur and (a.all or _addressed(m, me))]
    if not unread:
        print(f"(no new messages for {me} in room {a.room!r})")
        return 0
    for m in unread:
        ref = f" ref={m['ref']}" if m.get("ref") else ""
        print(f"--- #{m['seq']} {m['ts']} {m['from']} -> {m['to']} [{m['kind']}]{ref}")
        print(m["text"])
    if a.ack:
        with Locked(a.room):
            set_cursor(a.room, me, msgs[-1]["seq"])
        print(f"(acked through #{msgs[-1]['seq']})")
    return 0


def cmd_ack(a: argparse.Namespace) -> int:
    me, _ = identify(a.room, a.me)
    msgs = read_messages(a.room)
    if msgs:
        with Locked(a.room):
            set_cursor(a.room, me, msgs[-1]["seq"])
        print(f"acked through #{msgs[-1]['seq']} for {me}")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    data = load_room(a.room)
    msgs = read_messages(a.room)
    boss = (data.get("boss") or {}).get("name", "-")
    names = [boss] + sorted(data.get("members", {}))
    print(f"room {a.room!r}: boss={boss}  {len(msgs)} messages")
    for n in names:
        cur = get_cursor(a.room, n)
        unread = [m for m in msgs if m["seq"] > cur and _addressed(m, n)]
        asks = [m for m in unread if m["kind"] == "ask"]
        last = max((m["ts"] for m in msgs if m["from"] == n), default="-")
        tag = "boss  " if n == boss else "member"
        print(f"  {tag} {n:12s} unread={len(unread):3d} asks={len(asks):2d} last-posted={last}")
    return 0


def cmd_tail(a: argparse.Namespace) -> int:
    for m in read_messages(a.room)[-a.n :]:
        first = m["text"].splitlines()[0] if m["text"] else ""
        print(f"#{m['seq']:4d} {m['ts']} {m['from']:>9s} -> {m['to']:<9s} [{m['kind']:6s}] {first[:110]}")
    return 0


# ----------------------------------------------------------------------------
# kiso session peek — the Boss reads the durable log instead of asking
# ----------------------------------------------------------------------------


def _sessions_dir() -> Path:
    return Path(os.environ.get("KISO_HOME", Path.home() / ".kiso")) / "sessions"


def _load_session(path: Path) -> list[dict]:
    events = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = rec.get("event")
        if isinstance(ev, dict):
            ev = dict(ev)
            ev["_runId"] = rec.get("runId")
            events.append(ev)
    return events


def _pick_session(spec: str, match: str | None) -> Path:
    d = _sessions_dir()
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if spec != "latest":
        cands = [p for p in files if p.stem == spec or p.stem.endswith(spec)]
        if not cands:
            sys.exit(f"boss-call: no session matching {spec!r} in {d}")
        return cands[0]
    if match:
        for p in files:
            for ev in _load_session(p):
                if ev.get("type") == "user_input":
                    if match in str(ev.get("content", "")):
                        return p
                    break
        sys.exit(f"boss-call: no session whose first input contains {match!r}")
    if not files:
        sys.exit(f"boss-call: no sessions in {d}")
    return files[0]


def cmd_peek(a: argparse.Namespace) -> int:
    path = _pick_session(a.session, a.match)
    events = _load_session(path)
    if not events:
        print(f"{path.name}: empty")
        return 0
    last_run = events[-1]["_runId"]
    run = [e for e in events if e["_runId"] == last_run]
    inputs = [e for e in events if e.get("type") == "user_input"]
    terminal = next((e for e in reversed(run) if e.get("type") == "terminal"), None)
    last_in_idx = max((i for i, e in enumerate(events) if e.get("type") == "user_input"), default=-1)
    text = "".join(e.get("text", "") for e in events[last_in_idx + 1 :] if e.get("type") == "text_delta")
    decided = {e.get("decisionId") for e in events if e.get("type") in ("permission_decided", "permission_expired")}
    pending = [e for e in events if e.get("type") == "permission_requested" and e.get("decisionId") not in decided]
    receipts = {e.get("executionId") for e in run if e.get("type") in ("tool_execution_succeeded", "tool_execution_failed", "tool_execution_resolved")}
    uncertain = [e for e in run if e.get("type") == "tool_execution_started" and e.get("executionId") not in receipts]
    tool_calls = sum(1 for e in events if e.get("type") == "tool_call_end")
    mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    print(f"session {path.stem}  last-write {mtime}  events {len(events)}  turns {len(inputs)}  tool_calls {tool_calls}")
    if inputs:
        print(f"first input : {str(inputs[0].get('content', '')).replace(chr(10), ' ')[: a.width]}")
        print(f"last input  : {str(inputs[-1].get('content', '')).replace(chr(10), ' ')[: a.width]}")
    if terminal is None:
        state = "OPEN (no terminal in last run)"
        if pending:
            state += f" — WAITING ON A PERSON: {len(pending)} pending approval(s)"
        elif uncertain:
            state += f" — {len(uncertain)} uncertain execution(s) (started, no receipt)"
        else:
            state += " — running or interrupted"
    else:
        state = f"ended: {terminal.get('outcome', {}).get('kind', '?')}"
    print(f"state       : {state}")
    if text.strip():
        print("last text   :")
        for line in text.strip().splitlines()[-a.lines :]:
            print("  " + line[: a.width])
    return 0


# ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="boss-call", description=__doc__.split("\n\n")[0])
    ap.add_argument("--room", default=DEFAULT_ROOM, help=f"room (default: $BOSS_CALL_ROOM or {DEFAULT_ROOM!r})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create/update a room: one boss, N members")
    p.add_argument("--boss", default=None, help="boss name, optionally name=/root")
    p.add_argument("--member", action="append", default=[], help="name=/repo/root (repeatable)")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("who", help="who am I, who is the boss, who are the members")
    p.add_argument("--me", default=None)
    p.set_defaults(fn=cmd_who)

    p = sub.add_parser("post", help="send a message (a member's goes to the boss; the boss says --to)")
    p.add_argument("--me", "--from", dest="sender", default=None)
    p.add_argument("--to", default=None, help="boss only: a member name or 'all'")
    p.add_argument("--kind", choices=KINDS, default="msg")
    p.add_argument("--ref", default=None, help="what this is about: '#12', a commit, a file")
    p.add_argument("text", nargs="?", default=None, help="omit to read stdin")
    p.set_defaults(fn=cmd_post)

    p = sub.add_parser("read", help="print messages you have not acknowledged")
    p.add_argument("--me", default=None)
    p.add_argument("--all", action="store_true", help="include traffic not addressed to you")
    p.add_argument("--ack", action="store_true", help="mark what was printed as read")
    p.set_defaults(fn=cmd_read)

    p = sub.add_parser("ack", help="mark everything as read")
    p.add_argument("--me", default=None)
    p.set_defaults(fn=cmd_ack)

    p = sub.add_parser("status", help="unread and open asks per participant")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("tail", help="last N messages, one line each")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_tail)

    p = sub.add_parser("peek-session", help="summarise a kiso session from its durable log")
    p.add_argument("session", nargs="?", default="latest")
    p.add_argument("--match", default=None, help="with 'latest': newest session whose first input contains this")
    p.add_argument("--lines", type=int, default=12)
    p.add_argument("--width", type=int, default=160)
    p.set_defaults(fn=cmd_peek)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
