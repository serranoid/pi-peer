"""The wire: newline-delimited JSON over a Unix socket.

Claude Code documents the shape in its own diagnostics:

    {"type":"auth","token":"<token>"}
    {"type":"user","message":{"role":"user","content":"hello"}}

A connection that sends no complete line quickly is closed, so frames are sent
in one write.
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any, Iterable

MAX_MESSAGE = 100_000


class PeerError(RuntimeError):
    pass


def encode(frames: Iterable[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in frames).encode()


def decode(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        value = json.loads(line)
    except ValueError:
        return {"type": "_unparsed", "raw": line}
    return value if isinstance(value, dict) else {"type": "_unparsed", "raw": line}


def message_frame(text: str, *, sender: str, target_session_id: str | None) -> dict[str, Any]:
    if len(text) > MAX_MESSAGE:
        raise PeerError(f"message is longer than {MAX_MESSAGE} characters")
    frame: dict[str, Any] = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "from": sender,
        "uuid": str(uuid.uuid4()),
    }
    if target_session_id:
        # A frame carrying another session's id is dropped by the receiver.
        frame["session_id"] = target_session_id
    return frame


def send(
    socket_path: str,
    frames: list[dict[str, Any]],
    *,
    token: str | None = None,
    timeout: float = 5.0,
    wait_for_reply: bool = False,
    reply_timeout: float = 0.0,
) -> list[dict[str, Any]]:
    """Send frames to a peer's inbox; optionally read back what it sends us."""
    payload = ([{"type": "auth", "token": token}] if token else []) + frames
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
    except OSError as exc:
        sock.close()
        raise PeerError(f"cannot reach {socket_path}: {exc}") from exc

    try:
        sock.sendall(encode(payload))
        if not wait_for_reply:
            return []
        return _read_frames(sock, reply_timeout)
    finally:
        sock.close()


def _read_frames(sock: socket.socket, timeout: float) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    buf = b""
    sock.settimeout(timeout)
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                frame = decode(line.decode("utf-8", "replace"))
                if frame:
                    frames.append(frame)
    except (TimeoutError, socket.timeout, OSError):
        pass
    return frames
