"""pi-peer: let Claude Code sessions talk to pi, and pi talk back.

    pi-peer start            run the inbox in the background
    pi-peer stop             stop it
    pi-peer status           is it running, and where
    pi-peer ls               every live session on this machine
    pi-peer ask models "..." send, and wait for the answer
    pi-peer inbox            what arrived
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import registry
from .daemon import Inbox, control, inbox_entries, read_state

DEFAULT_WAIT = 120.0
LOG_NAME = "daemon.log"

def _log_path() -> Path:
    return registry.state_dir() / LOG_NAME

# -- start / stop ---------------------------------------------------------
def cmd_start(args: argparse.Namespace) -> int:
    state = read_state()
    if state:
        print(f"pi-peer already running (pid {state['pid']}, {state['socket']})")
        return 0

    cwd = os.getcwd()
    pid = os.fork()
    if pid:
        for _ in range(100):
            state = read_state()
            if state:
                print(f"pi-peer listening as {state['name']!r} on {state['socket']}")
                print(f"log: {_log_path()}")
                return 0
            time.sleep(0.05)
        print("pi-peer failed to start; see", _log_path(), file=sys.stderr)
        return 1

    os.setsid()
    if os.fork():
        os._exit(0)
    os.chdir(cwd)
    log = _log_path().open("a", buffering=1)
    devnull = open(os.devnull, "rb")
    os.dup2(devnull.fileno(), 0)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    code = Inbox(name=args.name, cwd=cwd).serve()
    os._exit(code)

def cmd_stop(_args: argparse.Namespace) -> int:
    state = read_state()
    if not state:
        print("pi-peer is not running")
        return 1
    try:
        os.kill(int(state["pid"]), signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(60):
        if read_state() is None:
            print("pi-peer stopped")
            return 0
        time.sleep(0.05)
    print("pi-peer did not stop cleanly", file=sys.stderr)
    return 1

def cmd_status(_args: argparse.Namespace) -> int:
    state = read_state()
    if not state:
        print("pi-peer is not running")
        return 1
    print(f"name      {state['name']}")
    print(f"pid       {state['pid']}")
    print(f"socket    {state['socket']}")
    print(f"session   {state['sessionId']}")
    print(f"log       {_log_path()}")
    return 0

# -- inspect --------------------------------------------------------------
def cmd_ls(args: argparse.Namespace) -> int:
    ours = read_state()
    sessions = registry.live_sessions(include_dead=args.all)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": s.name,
                        "pid": s.pid,
                        "status": s.status,
                        "cwd": s.cwd,
                        "socket": s.socket_path,
                        "is_pi_peer": bool(ours and s.pid == ours["pid"]),
                    }
                    for s in sessions
                ],
                indent=2,
            )
        )
        return 0
    if not sessions:
        print("no live sessions")
        return 1
    width = max(len(s.name or "-") for s in sessions)
    for s in sessions:
        mark = " (this)" if ours and s.pid == ours["pid"] else ""
        print(f"{s.name or '-':<{width}}  {s.pid:>7}  {str(s.status or '-'):<8}  {s.cwd}{mark}")
    return 0

def _target_token(session: registry.Session) -> str | None:
    key = registry.sessions_dir() / registry.key_filename(session.pid, session.socket_path)
    try:
        return json.loads(key.read_text()).get("peerToken")
    except (OSError, ValueError):
        return None

def cmd_ask(args: argparse.Namespace) -> int:
    state = read_state()
    if not state:
        print("pi-peer is not running: start it with 'pi-peer start'", file=sys.stderr)
        return 2
    target = registry.resolve_target(args.target)
    if target is None:
        print(f"no live session matches {args.target!r}; try 'pi-peer ls'", file=sys.stderr)
        return 2
    if not target.alive:
        print(f"session {target.name!r} is listed but its inbox is gone", file=sys.stderr)
        return 2
    if target.pid == state["pid"]:
        print("that is this session", file=sys.stderr)
        return 2

    since = int(time.time() * 1000)
    # The daemon sends: the receiver checks that the process connecting to it is
    # the one that owns the address in `from`.
    result = control(
        {
            "action": "send",
            "target": target.socket_path,
            "session_id": target.session_id,
            "token": _target_token(target),
            "text": args.message,
        }
    )
    if not result.get("ok"):
        print(result.get("error", "send failed"), file=sys.stderr)
        return 1
    print(f"sent to {target.name or target.pid} ({target.pid}) from {result.get('from')}")

    if args.wait <= 0:
        return 0
    print(f"waiting up to {args.wait:.0f}s for an answer...")
    deadline = time.time() + args.wait
    while time.time() < deadline:
        time.sleep(0.5)
        for entry in inbox_entries():
            if entry.get("at", 0) < since or entry.get("type") != "user":
                continue
            sender = entry.get("from") or ""
            same_peer = (
                target.socket_path in sender
                or str(target.pid) in sender
                or (entry.get("from_name") and entry["from_name"] == target.name)
            )
            if not same_peer:
                continue
            print()
            print(f"← {target.name or target.pid} ({time.strftime('%H:%M:%S', time.localtime(entry['at'] / 1000))})")
            print(entry.get("text") or json.dumps(entry.get("frame"), ensure_ascii=False))
            return 0
    print("no answer within the wait; check 'pi-peer inbox'", file=sys.stderr)
    return 1

def cmd_inbox(args: argparse.Namespace) -> int:
    entries = inbox_entries(limit=args.limit)
    if args.json:
        for entry in entries:
            print(json.dumps(entry, ensure_ascii=False))
        return 0
    if not entries:
        print("empty")
        return 1
    for entry in entries:
        when = time.strftime("%H:%M:%S", time.localtime(entry["at"] / 1000))
        who = entry.get("from_name") or entry.get("from") or "?"
        print(f"[{when}] {entry.get('type')} from {who}")
        text = entry.get("text")
        if text:
            print("    " + text.replace("\n", "\n    "))
        else:
            print("    " + json.dumps(entry.get("frame"), ensure_ascii=False)[:300])
        print()
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-peer", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="run the inbox in the background")
    p.add_argument("--name", default="pi", help="the name other sessions see (default: pi)")
    p.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="stop the inbox").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="is it running").set_defaults(func=cmd_status)

    p = sub.add_parser("ls", help="list live sessions")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="include sessions whose inbox is gone")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("ask", help="send a message and wait for the answer")
    p.add_argument("target", help="session name or pid")
    p.add_argument("message")
    p.add_argument("--wait", type=float, default=DEFAULT_WAIT, help="seconds to wait (0: don't)")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("inbox", help="what arrived")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inbox)
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
