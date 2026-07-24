"""Activation log — the append-only audit trail of every stream/record activation.

This module is webcam-cli's consent surface (see the repo's ``CLAUDE.md``,
"Consent posture"). The project cannot promise a hardware activity light —
that is device firmware, outside this tool's control — but it *can* promise
that every capture writes to a named path with no hidden buffer, and that
every activation is logged. This module owns exactly the second half of that
promise: recording *that* an activation happened, never the media itself.

Scope boundary: this module knows nothing about devices, permissions, or the
capture engine. It takes plain strings and writes metadata — nothing more.

Zero runtime dependencies: standard library only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

# Env var that overrides the default activation-log location. Documented in
# ``log_path()`` below; the CLI/docs surfaces should point agents at it.
ENV_LOG_PATH = "WEBCAM_ACTIVATION_LOG"

_ENV_XDG_STATE_HOME = "XDG_STATE_HOME"
_STATE_SUBPATH = Path("webcam-cli") / "activations.jsonl"


def log_path() -> Path:
    """Return the documented default activation-log location.

    Resolution order:

    1. ``$WEBCAM_ACTIVATION_LOG`` — an explicit override, used verbatim.
    2. ``$XDG_STATE_HOME/webcam-cli/activations.jsonl`` — XDG state dir.
    3. ``~/.local/state/webcam-cli/activations.jsonl`` — XDG fallback, used
       when ``$XDG_STATE_HOME`` is unset.

    This function never touches the filesystem — it only computes a path.
    Callers that write (:func:`record_activation`) create parent
    directories on demand.
    """
    override = os.environ.get(ENV_LOG_PATH)
    if override:
        return Path(override)

    xdg_state_home = os.environ.get(_ENV_XDG_STATE_HOME)
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / _STATE_SUBPATH


@dataclass(frozen=True)
class Activation:
    """One activation of a capture device: a stream or a recording session.

    ``ended_at`` is ``None`` while the activation is still running; once it
    is populated the activation is complete — including when it ended by
    crashing (see :func:`activation_scope`), so a dead stream never simply
    vanishes from the record.
    """

    device_id: str
    verb: str
    target: str
    started_at: str
    ended_at: str | None
    pid: int
    detail: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "verb": self.verb,
            "target": self.target,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "pid": self.pid,
            "detail": self.detail,
        }


def record_activation(activation: Activation, *, path: Path | None = None) -> None:
    """Append exactly one JSON line for ``activation`` to the activation log.

    Writes via a single ``os.open(..., O_APPEND)`` followed by exactly one
    ``os.write()`` of the complete ``line + "\\n"``. Under ``O_APPEND`` the
    kernel serialises writers to the same regular file, so a single
    syscall carrying a complete line cannot interleave with another
    writer's line — the standard zero-dependency way to make concurrent
    appends safe. ``tests/test_activation.py`` proves this from both
    multiple processes and multiple threads.

    Any failure to write — permission denied, missing parent, disk full,
    a parent path component that is not a directory, ... — propagates to
    the caller as the underlying ``OSError``. It is never swallowed here:
    a silently-lost line would break the project's consent guarantee that
    every activation is logged.
    """
    target = path if path is not None else log_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(activation.to_dict())
    payload = (line + "\n").encode("utf-8")

    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def activation_scope(
    *,
    device_id: str,
    verb: str,
    target: str,
    detail: dict[str, object] | None = None,
    path: Path | None = None,
) -> Iterator[Activation]:
    """Context manager around one activation; writes exactly one log line on exit.

    The activation is recorded once, on exit — never on entry, so a still
    running activation never appears in the log — and exactly once whether
    the body finishes cleanly or raises. On a raise, the exception's type
    and message are folded into ``detail["error"]`` (without overwriting an
    "error" key the caller already set) before the single line is written,
    and the original exception is re-raised unchanged. This is deliberate:
    a stream that dies mid-activation must still leave a completed record
    with ``ended_at`` set, not vanish silently.
    """
    activation = Activation(
        device_id=device_id,
        verb=verb,
        target=target,
        started_at=_now_iso(),
        ended_at=None,
        pid=os.getpid(),
        detail=dict(detail) if detail is not None else {},
    )
    try:
        yield activation
    except BaseException as exc:
        crash_detail = dict(activation.detail)
        crash_detail.setdefault("error", f"{type(exc).__name__}: {exc}")
        finished = replace(activation, ended_at=_now_iso(), detail=crash_detail)
        record_activation(finished, path=path)
        raise
    else:
        finished = replace(activation, ended_at=_now_iso())
        record_activation(finished, path=path)
