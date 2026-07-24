"""Typed device-access errors: absent, forbidden, and busy device nodes.

Scope: this module owns exactly one question — *can this device node be
opened right now, and if not, why not and what fixes it*. It does not own
device enumeration or pairing (a sibling module, ``webcam_cli.devices``, not
yet on this branch) and it does not own pixel formats or audio parameters.
Callers pass a plain path string; this module never imports ``webcam_cli``
sibling modules.

Three failure states matter and must never be conflated (see the project
brief, ``CLAUDE.md``, "Domain constraints"):

* **absent** — no node at that path at all (a bad/stale path, or hardware
  unplugged). The agent named a device that is not there: a user error.
* **forbidden** — the node exists but ``open()`` fails with ``EACCES``/
  ``EPERM``. On this project's reference host, video access comes from a
  **seat ACL** granted by logind to the active graphical session — *not*
  from ``video``-group membership (the operator is not in that group, yet
  ``getfacl /dev/video0`` shows ``user:spark:rw-``). A headless, containerized,
  or systemd-unit session receives no seat and loses camera access while
  *keeping* microphone access, because ALSA capture is gated by the
  ``audio`` group instead, which such a session typically does have. The two
  subsystems fail for different reasons and need differently-worded fixes;
  telling someone to join ``video`` when the real mechanism is a seat ACL
  sends them down a dead end.
* **busy** — the node exists, is permitted, but is already held open by
  another process (``EBUSY``). :func:`find_holder` makes a best-effort,
  bounded, non-blocking scan of ``/proc/*/fd`` to name the holder; when it
  cannot (permission-limited for another user's process, a race on a
  vanishing pid, a dangling symlink), it degrades to ``holder=None`` rather
  than raising or hanging. A busy report with an unknown holder is a normal,
  expected outcome — not a bug.

:func:`check_access` never raises for an inaccessible device; it is the
reporting path used by ``list``-shaped verbs, which must exit 0 and simply
show a bad device as bad. :func:`require_access` is the enforcing path used
by capture verbs: it raises the typed :class:`~webcam_cli.cli._errors.CliError`
for anything other than ``AccessState.OK``.
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from enum import Enum

from webcam_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

__all__ = [
    "AccessState",
    "Holder",
    "AccessReport",
    "check_access",
    "require_access",
    "find_holder",
    "access_error",
]

_KINDS = ("video", "audio")


class AccessState(str, Enum):
    """The outcome of attempting to open a device node."""

    OK = "ok"
    ABSENT = "absent"
    FORBIDDEN = "forbidden"
    BUSY = "busy"


@dataclass(frozen=True)
class Holder:
    """The process a busy device is currently held open by, best-effort."""

    pid: int
    command: str  # process name, best-effort (from /proc/<pid>/comm)


@dataclass(frozen=True)
class AccessReport:
    """The result of :func:`check_access` — never raised, always returned."""

    path: str
    kind: str  # "video" | "audio"
    state: AccessState
    remediation: str  # "" when state is OK
    holder: Holder | None = None  # populated only when state is BUSY and determinable


def _validate_kind(kind: str) -> None:
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {_KINDS!r}, got {kind!r}")


def _open_flags(kind: str) -> int:
    # O_NONBLOCK so a slow/blocking-on-open node can never hang us here; this
    # module only ever asks "can it be opened", it does not read or stream.
    # O_CLOEXEC so a probe never leaks an fd into a child process.
    extra = getattr(os, "O_CLOEXEC", 0)
    if kind == "audio":
        return os.O_RDONLY | os.O_NONBLOCK | extra
    return os.O_RDWR | os.O_NONBLOCK | extra


def _absent_remediation(kind: str, path: str) -> str:
    if kind == "audio":
        return (
            f"{path} does not exist — check the microphone/card is plugged in and "
            "listed by 'arecord -l' or /proc/asound/cards; ALSA card numbers renumber "
            "on replug, so a stale path is the most common cause"
        )
    return (
        f"{path} does not exist — check the camera is plugged in and listed under "
        "/dev/v4l/by-id/ (or /dev/videoN); V4L2 node numbers renumber on replug, so a "
        "stale path is the most common cause"
    )


def _forbidden_remediation(kind: str, path: str) -> str:
    if kind == "audio":
        return (
            f"permission denied opening {path} — ALSA capture devices are gated by "
            "'audio'-group membership; add the invoking user to the 'audio' group and "
            "re-login (unlike /dev/video*, there is no seat-ACL path for ALSA on this host)"
        )
    return (
        f"permission denied opening {path} — on an active desktop session, logind "
        f"grants a per-seat ACL (see 'getfacl {path}'); a headless, containerized, or "
        "systemd-unit session receives no seat and will not get that ACL even though a "
        "human's desktop login would. Run this from an active graphical/logind seat, or "
        "add the invoking user to the 'video' group and re-login as a fallback"
    )


def _busy_remediation(kind: str, holder: Holder | None) -> str:
    subsystem = "ALSA capture device" if kind == "audio" else "V4L2 device"
    if holder is not None:
        who = f"{holder.command} (pid {holder.pid})"
    else:
        who = (
            "another process that could not be identified "
            "(no permission to read its /proc/<pid>/fd)"
        )
    return (
        f"{subsystem} is already open by {who}; only one exclusive capture client is "
        "supported at a time — stop that process (or wait for it to release the device), "
        "then retry"
    )


def check_access(path: str, kind: str) -> AccessReport:
    """Report whether ``path`` (a ``kind`` device node) can be opened right now.

    Never raises for an inaccessible device — this is the reporting path used
    by ``list``-shaped verbs, which must exit 0 while still showing a bad
    device as bad. Only an invalid ``kind`` (a caller bug, not a device-access
    outcome) raises ``ValueError``.

    Makes exactly one non-blocking open attempt (``O_NONBLOCK``) and closes
    the descriptor immediately on success; no retries, no sleeps, no loop —
    so this call is bounded by however long a single ``open(2)`` takes, which
    for a character device is not a wait on remote I/O.
    """
    _validate_kind(kind)
    try:
        fd = os.open(path, _open_flags(kind))
    except FileNotFoundError:
        return AccessReport(
            path=path,
            kind=kind,
            state=AccessState.ABSENT,
            remediation=_absent_remediation(kind, path),
        )
    except PermissionError:
        return AccessReport(
            path=path,
            kind=kind,
            state=AccessState.FORBIDDEN,
            remediation=_forbidden_remediation(kind, path),
        )
    except OSError as exc:
        if exc.errno == errno.EBUSY:
            holder = find_holder(path)
            return AccessReport(
                path=path,
                kind=kind,
                state=AccessState.BUSY,
                remediation=_busy_remediation(kind, holder),
                holder=holder,
            )
        # Any other OSError (ENODEV/ENXIO for a node whose hardware vanished
        # mid-race, etc.) is still an environment problem the caller cannot
        # fix by blindly retrying — report it rather than silently claim OK.
        return AccessReport(
            path=path,
            kind=kind,
            state=AccessState.FORBIDDEN,
            remediation=_forbidden_remediation(kind, path),
        )
    else:
        os.close(fd)
        return AccessReport(path=path, kind=kind, state=AccessState.OK, remediation="")


def require_access(path: str, kind: str) -> None:
    """Raise the typed :class:`CliError` unless ``path`` is openable now.

    Used by capture paths, which need to fail loudly rather than report.
    """
    report = check_access(path, kind)
    if report.state is not AccessState.OK:
        raise access_error(report)


def _list_proc_pids() -> list[str]:
    return os.listdir("/proc")


def _list_fds(pid: str) -> list[str]:
    return os.listdir(f"/proc/{pid}/fd")


def _readlink(fd_path: str) -> str:
    return os.readlink(fd_path)


def _read_command(pid: str) -> str:
    with open(f"/proc/{pid}/comm", encoding="utf-8") as handle:
        return handle.read().strip()


def find_holder(path: str) -> Holder | None:
    """Best-effort, bounded scan of ``/proc/*/fd`` for a process with ``path`` open.

    Degrades gracefully to ``None`` — never raises — on anything short of a
    clean match: ``/proc`` unavailable, a pid's ``fd`` directory unreadable
    (owned by another user, the common case), a pid that exits mid-scan, or a
    dangling symlink. A busy report with ``holder=None`` is correct and
    expected, not a failure of this function.

    The scan is a single linear pass over ``/proc`` with no retries and no
    sleeps, so it is bounded by the number of processes and open descriptors
    on the host at the moment of the call — it cannot hang waiting on a
    device, because it never opens one.
    """
    try:
        target = os.path.realpath(path)
    except OSError:
        return None

    try:
        pids = _list_proc_pids()
    except OSError:
        return None

    for entry in pids:
        if not entry.isdigit():
            continue
        try:
            fd_names = _list_fds(entry)
        except OSError:
            # Not ours to read, or the process is already gone — skip it,
            # don't guess.
            continue
        for fd_name in fd_names:
            try:
                link = _readlink(f"/proc/{entry}/fd/{fd_name}")
            except OSError:
                continue
            if link == target:
                try:
                    command = _read_command(entry)
                except OSError:
                    command = "unknown"
                return Holder(pid=int(entry), command=command)
    return None


def access_error(report: AccessReport) -> CliError:
    """Map a non-OK :class:`AccessReport` to the typed :class:`CliError`.

    Exit-code policy:

    * ``ABSENT``    -> ``EXIT_USER_ERROR`` (1) — the agent named a device that isn't there.
    * ``FORBIDDEN`` -> ``EXIT_ENV_ERROR``  (2) — the host/session is misconfigured.
    * ``BUSY``      -> ``EXIT_ENV_ERROR``  (2) — a transient environment condition.

    Calling this with an ``OK`` report is a programming error, not a device
    outcome, and raises ``ValueError``.
    """
    if report.state is AccessState.OK:
        raise ValueError("access_error() called with an OK report; there is nothing to map")

    if report.state is AccessState.ABSENT:
        return CliError(
            code=EXIT_USER_ERROR,
            message=f"no {report.kind} device at {report.path}",
            remediation=report.remediation,
        )

    if report.state is AccessState.FORBIDDEN:
        return CliError(
            code=EXIT_ENV_ERROR,
            message=f"permission denied opening {report.kind} device {report.path}",
            remediation=report.remediation,
        )

    # BUSY
    holder_desc = ""
    if report.holder is not None:
        holder_desc = f" (held by {report.holder.command}, pid {report.holder.pid})"
    return CliError(
        code=EXIT_ENV_ERROR,
        message=f"{report.kind} device {report.path} is busy{holder_desc}",
        remediation=report.remediation,
    )
