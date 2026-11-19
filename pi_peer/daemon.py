"""The inbox: a Unix socket that other agent sessions can send messages to."""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import socket
import threading
import time
import uuid
from pathlib import Path

from . import registry
from .wire import PeerError, decode, message_frame, send

REFRESH_SECONDS = 30.0

# A reply from another session arrives wrapped in a tag carrying its provenance:
#
#   <cross-session-message from="uds:/run/user/1000/cc-socks/123.sock"
#     hop-chain="1cba058ab4e6458bb34e2490" from-name="models" from-mode="prompting">
#   the text
#   </cross-session-message>
WRAPPER = re.compile(
    r"<cross-session-message\s+(?P<attrs>[^>]*)>(?P<body>.*?)(?:</cross-session-message>)?\s*$",
    re.DOTALL,
)
ATTR = re.compile(r"([a-z-]+)=\"([^\"]*)\"")


def unwrap(text: str) -> tuple[str, dict[str, str]]:
    """Split a peer reply into its body and the provenance attributes."""
    match = WRAPPER.search(text.strip())
    if not match:
        return text.strip(), {}
    return match.group("body").strip(), dict(ATTR.findall(match.group("attrs")))



def _message_text(frame: dict) -> str:
    message = frame.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return ""


class Inbox:
    def __init__(self, name: str = "pi", cwd: str | None = None) -> None:
        self.pid = os.getpid()
        self.name = name
        self.cwd = cwd or os.getcwd()
        self.socket_path = str(registry.socket_dir() / f"{self.pid}.sock")
        self.session_id = str(uuid.uuid4())
        self.token = secrets.token_hex(16)
        self.started_at = int(time.time() * 1000)
        self.home = registry.state_dir()
        self.inbox_path = self.home / "inbox.jsonl"
        self.state_path = self.home / "state.json"
        # A session must send from the process that owns its socket: the
        # receiver checks the connecting pid against the address in `from`.
        self.control_path = self.home / "control.sock"
        self.pid_domain = registry.pid_domain()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server: socket.socket | None = None

    # -- registration -----------------------------------------------------
    def _record(self) -> dict:
        start = registry.proc_start(self.pid)
        return {
            "pid": self.pid,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "startedAt": self.started_at,
            "procStart": start,
            "version": "0.1.0",
            "peerProtocol": registry.PEER_PROTOCOL,
            "peerFeatures": registry.PEER_FEATURES,
            "kind": "interactive",
            "entrypoint": "cli",
            "pidDomain": self.pid_domain,
            "asksForConvertedSkills": None,
            "messagingSocketPath": self.socket_path,
            "name": self.name,
            "nameSource": "user",
            "nameSince": self.started_at,
            "updatedAt": int(time.time() * 1000),
            "status": "idle",
            "statusUpdatedAt": int(time.time() * 1000),
            "bridgeSessionId": None,
        }

    def _write_registry(self) -> None:
        directory = registry.sessions_dir()
        directory.mkdir(parents=True, exist_ok=True)
        record = json.dumps(self._record(), separators=(",", ":"))
        (directory / f"{self.pid}.json").write_text(record)

        key_name = registry.key_filename(self.pid, self.socket_path)
        key = {
            "peerToken": self.token,
            "procStart": registry.proc_start(self.pid),
            "pidDomain": self.pid_domain,
        }
        path = directory / key_name
        path.write_text(json.dumps(key, separators=(",", ":")))
        os.chmod(path, 0o600)

    def _unregister(self) -> None:
        directory = registry.sessions_dir()
        for path in (
            directory / f"{self.pid}.json",
            directory / registry.key_filename(self.pid, self.socket_path),
            self.state_path,
            Path(self.socket_path),
            self.control_path,
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    # -- mail -------------------------------------------------------------
    def record(self, frame: dict, peer: str | None) -> dict:
        body, meta = unwrap(_message_text(frame))
        entry = {
            "at": int(time.time() * 1000),
            "from": peer,
            "from_name": meta.get("from-name"),
            "hop_chain": meta.get("hop-chain"),
            "mode": meta.get("from-mode"),
            "type": frame.get("type"),
            "text": body,
            "frame": frame,
        }
        with self._lock:
            with self.inbox_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        text = body.replace("\n", " ")
        who = entry["from_name"] or peer or "?"
        print(f"[pi-peer] {entry['type']} from {who}: {text[:200]}", flush=True)
        return entry

    # -- server -----------------------------------------------------------
    def _handle(self, conn: socket.socket) -> None:
        peer: str | None = None
        authenticated = False
        buf = b""
        conn.settimeout(120)
        try:
            while not self._stop.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    frame = decode(line.decode("utf-8", "replace")) or {}
                    kind = frame.get("type")
                    if kind == "auth":
                        authenticated = frame.get("token") == self.token
                        continue
                    if kind == "peer_message_status":
                        continue
                    if isinstance(frame.get("from"), str):
                        peer = frame["from"]
                    if kind == "user":
                        entry = self.record(frame, peer)
                        entry["authenticated"] = authenticated
        except (TimeoutError, socket.timeout):
            return
        except OSError:
            return
        finally:
            conn.close()

    # -- control ----------------------------------------------------------
    def _control(self, conn: socket.socket) -> None:
        buf = b""
        conn.settimeout(30)
        try:
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            request = decode(buf.split(b"\n", 1)[0].decode("utf-8", "replace")) or {}
            reply = self._run_request(request)
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode())
        except (OSError, ValueError) as exc:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())
            except OSError:
                pass
        finally:
            conn.close()

    def _run_request(self, request: dict) -> dict:
        action = request.get("action")
        if action == "send":
            target = str(request.get("target", ""))
            if not target:
                return {"ok": False, "error": "no target"}
            frame = message_frame(
                str(request.get("text", "")),
                sender=self.socket_path,
                target_session_id=request.get("session_id"),
            )
            try:
                send(target, [frame], token=request.get("token"), timeout=5.0)
            except PeerError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "from": self.socket_path, "pid": self.pid}
        if action == "ping":
            return {"ok": True, "pid": self.pid, "socket": self.socket_path}
        return {"ok": False, "error": f"unknown action {action!r}"}

    def serve(self) -> int:
        if self.pid_domain is None:
            print(
                "[pi-peer] no Claude session is registered on this machine, so there is "
                "no pid namespace to borrow. Start one Claude Code session first.",
                flush=True,
            )
            return 2

        registry.socket_dir().mkdir(parents=True, exist_ok=True)
        for path in (self.socket_path, self.control_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        server.listen(16)
        self._server = server

        control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        control.bind(str(self.control_path))
        os.chmod(self.control_path, 0o600)
        control.listen(8)

        def control_loop() -> None:
            while not self._stop.is_set():
                try:
                    conn, _ = control.accept()
                except OSError:
                    return
                threading.Thread(target=self._control, args=(conn,), daemon=True).start()

        threading.Thread(target=control_loop, daemon=True).start()

        self._write_registry()
        self.state_path.write_text(
            json.dumps(
                {
                    "pid": self.pid,
                    "name": self.name,
                    "socket": self.socket_path,
                    "control": str(self.control_path),
                    "sessionId": self.session_id,
                    "startedAt": self.started_at,
                },
                indent=2,
            )
        )
        print(f"[pi-peer] listening on {self.socket_path} as {self.name!r}", flush=True)

        def refresh() -> None:
            while not self._stop.wait(REFRESH_SECONDS):
                try:
                    self._write_registry()
                except OSError:
                    pass

        threading.Thread(target=refresh, daemon=True).start()

        def shutdown(*_args) -> None:
            self._stop.set()
            for sock in (server, control):
                try:
                    sock.close()
                except OSError:
                    pass

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except OSError:
                    break
                except KeyboardInterrupt:
                    break
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            self._unregister()
            print("[pi-peer] stopped", flush=True)
        return 0


def control(request: dict, timeout: float = 10.0) -> dict:
    """Ask the running daemon to do something (it holds the sendable identity)."""
    state = read_state()
    if not state:
        return {"ok": False, "error": "pi-peer is not running"}
    path = state.get("control") or str(registry.state_dir() / "control.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError as exc:
        return {"ok": False, "error": f"cannot reach the pi-peer daemon: {exc}"}
    finally:
        sock.close()
    return decode(buf.split(b"\n", 1)[0].decode("utf-8", "replace")) or {
        "ok": False,
        "error": "empty reply from the daemon",
    }


def read_state() -> dict | None:
    path = registry.state_dir() / "state.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not registry.pid_alive(int(state.get("pid", 0))):
        return None
    if not os.path.exists(state.get("socket", "")):
        return None
    return state


def inbox_entries(limit: int | None = None) -> list[dict]:
    path = registry.state_dir() / "inbox.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries[-limit:] if limit else entries
