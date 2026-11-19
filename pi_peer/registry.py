"""Read and write the local Claude Code session registry.

Claude Code (2.1.x) keeps one record per live session in ``~/.claude/sessions``:
``<pid>.json`` describes the session and ``<pid>.<sha256(socket)>.key`` holds the
token other sessions must present. A session's inbox is a Unix socket named
``<pid>.sock`` inside a well-known per-user directory (``/run/user/<uid>/cc-socks``
or ``/tmp/cc-socks``).

None of this is documented upstream, so treat every field as a guess that was
checked against a real session. See README.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "pi-peer"
PEER_PROTOCOL = 1
PEER_FEATURES = ["notify_idle", "reply_across_default_dirs", "artifact_yield"]


def sessions_dir() -> Path:
    return Path(os.environ.get("CLAUDE_SESSIONS_DIR", Path.home() / ".claude" / "sessions"))


def state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def socket_dir() -> Path:
    """A directory Claude Code accepts peer sockets in."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        d = Path(runtime) / "cc-socks"
        if d.is_dir():
            return d
    for cand in (Path("/tmp") / "cc-socks", Path("/tmp") / f"cc-socks-{os.getuid()}"):
        if cand.is_dir():
            return cand
    return Path("/tmp") / "cc-socks"


def canonical(path: str | Path) -> str:
    return os.path.realpath(str(path))


def key_filename(pid: int, socket_path: str | Path) -> str:
    digest = hashlib.sha256(canonical(socket_path).encode()).hexdigest()
    return f"{pid}.{digest}.key"


def proc_start(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat: the process start time in clock ticks."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm (field 2) may contain spaces and parentheses; everything after the
    # last ')' starts at field 3.
    rest = stat[stat.rindex(")") + 2 :].split()
    return rest[19]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Session:
    pid: int
    name: str | None
    status: str | None
    cwd: str | None
    socket_path: str
    session_id: str | None
    peer_protocol: int | None
    raw: dict

    @property
    def alive(self) -> bool:
        return pid_alive(self.pid) and os.path.exists(self.socket_path)

    @property
    def is_self(self) -> bool:
        return self.pid == os.getpid()


def read_session(record: Path) -> Session | None:
    try:
        data = json.loads(record.read_text())
    except (OSError, ValueError):
        return None
    sock = data.get("messagingSocketPath")
    pid = data.get("pid")
    if not sock or not isinstance(pid, int):
        return None
    return Session(
        pid=pid,
        name=data.get("name"),
        status=data.get("status"),
        cwd=data.get("cwd"),
        socket_path=sock,
        session_id=data.get("sessionId"),
        peer_protocol=data.get("peerProtocol"),
        raw=data,
    )


def live_sessions(include_dead: bool = False) -> list[Session]:
    out: list[Session] = []
    for record in sorted(sessions_dir().glob("*.json")):
        session = read_session(record)
        if session is None:
            continue
        if include_dead or session.alive:
            out.append(session)
    return out


def pid_domain() -> str | None:
    """The pid namespace identity, copied from a session that already has one.

    Its middle component is a per-machine id we cannot compute, so we borrow it
    from a live Claude session. Talking to Claude requires one anyway.
    """
    for session in live_sessions(include_dead=True):
        value = session.raw.get("pidDomain")
        if isinstance(value, str) and value:
            return value
    return None


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return cleaned[:32]


def resolve_name(cwd: str | None = None) -> str:
    """The name a human already sees, so several sessions stay apart.

    ``PI_PEER_NAME`` wins; otherwise the Herdr space the terminal is in (the
    label Herdr shows in its agents list); otherwise the folder.
    """
    explicit = os.environ.get("PI_PEER_NAME")
    if explicit:
        return sanitize_name(explicit)

    terminal = os.environ.get("HC_TERMINAL_ID")
    if terminal:
        try:
            out = subprocess.run(
                ["hc", "get", terminal], capture_output=True, text=True, timeout=3
            )
            info = json.loads(out.stdout or "{}")
            where = (info.get("labels") or {}).get("where")
            if isinstance(where, str) and "/" in where:
                return sanitize_name("pi-" + where.split("/")[0].strip())
            title = info.get("title")
            if isinstance(title, str) and " - " in title:
                return sanitize_name("pi-" + title.split(" - ")[-1].strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    return sanitize_name("pi-" + Path(cwd or os.getcwd()).name)


def name_for_socket(socket_path: str) -> str | None:
    """The session name behind an address, so a message says who sent it."""
    wanted = canonical(socket_path.replace("uds:", ""))
    for session in live_sessions(include_dead=True):
        if canonical(session.socket_path) == wanted:
            return session.name
    return None


def resolve_target(target: str) -> Session | None:
    """Find a session by pid, exact name, or unique name prefix."""
    sessions = live_sessions()
    if target.isdigit():
        for s in sessions:
            if s.pid == int(target):
                return s
        return None
    exact = [s for s in sessions if s.name == target]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return exact[0]
    partial = [s for s in sessions if s.name and s.name.startswith(target)]
    if len(partial) == 1:
        return partial[0]
    return None
