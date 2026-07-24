"""Tests for :mod:`webcam_cli.access` — typed device-access errors.

Covers both acceptance criteria for build-plan task t2:

1. A busy device names its holder (pid + command) when determinable, never
   hangs, and stays within a bounded time — proven with monkeypatched
   ``os.open``/``find_holder`` (hermetic) and, separately, against the real
   ``/proc`` filesystem scoped to this test process only (never a real camera
   or microphone).
2. Absent and forbidden device access are never conflated: distinct states,
   distinct remediation wording, distinct exit-code policy — proven against a
   fake filesystem (monkeypatched ``os.open``) and, where safe, real
   ``tmp_path`` files.
"""

from __future__ import annotations

import dataclasses
import errno
import os
import time

import pytest

from webcam_cli import access
from webcam_cli.access import (
    AccessReport,
    AccessState,
    Holder,
    access_error,
    check_access,
    find_holder,
    require_access,
)
from webcam_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

# --- public-API shape (locked: later tasks are written against this) ------


def test_access_state_values() -> None:
    assert AccessState.OK.value == "ok"
    assert AccessState.ABSENT.value == "absent"
    assert AccessState.FORBIDDEN.value == "forbidden"
    assert AccessState.BUSY.value == "busy"


def test_access_report_and_holder_are_frozen() -> None:
    holder = Holder(pid=1, command="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        holder.pid = 2  # type: ignore[misc]

    report = AccessReport(path="p", kind="video", state=AccessState.OK, remediation="")
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.path = "q"  # type: ignore[misc]


def test_check_access_invalid_kind_raises_value_error() -> None:
    with pytest.raises(ValueError):
        check_access("/dev/video0", "bogus")


# --- check_access: OK -------------------------------------------------------


def test_check_access_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    monkeypatch.setattr(access.os, "open", lambda path, flags: 99)
    monkeypatch.setattr(access.os, "close", lambda fd: closed.append(fd))

    report = check_access("/dev/video0", "video")

    assert report.state is AccessState.OK
    assert report.remediation == ""
    assert report.holder is None
    assert closed == [99]  # the probe closes its own descriptor


# --- check_access: ABSENT ---------------------------------------------------


def test_check_access_absent_via_fake_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        access.os, "open", lambda path, flags: (_ for _ in ()).throw(FileNotFoundError())
    )

    report = check_access("/dev/video7", "video")

    assert report.state is AccessState.ABSENT
    assert report.holder is None
    assert report.remediation  # non-empty, actionable
    assert access_error(report).code == EXIT_USER_ERROR


def test_check_access_absent_real_missing_path(tmp_path: object) -> None:
    missing = str(tmp_path) + "/does-not-exist"  # type: ignore[operator]

    report = check_access(missing, "video")

    assert report.state is AccessState.ABSENT
    assert report.holder is None


def test_absent_remediation_differs_by_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        access.os, "open", lambda path, flags: (_ for _ in ()).throw(FileNotFoundError())
    )

    video_report = check_access("/dev/video0", "video")
    audio_report = check_access("/dev/snd/pcmC0D0c", "audio")

    assert video_report.remediation != audio_report.remediation
    video_text = video_report.remediation.lower()
    audio_text = audio_report.remediation.lower()
    assert "v4l" in video_text or "camera" in video_text
    assert "alsa" in audio_text or "arecord" in audio_text


# --- check_access: FORBIDDEN -------------------------------------------------


def test_check_access_forbidden_via_fake_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        access.os, "open", lambda path, flags: (_ for _ in ()).throw(PermissionError())
    )

    report = check_access("/dev/video0", "video")

    assert report.state is AccessState.FORBIDDEN
    assert report.holder is None
    assert access_error(report).code == EXIT_ENV_ERROR


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_check_access_forbidden_real_permission_denied(tmp_path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_bytes(b"")
    os.chmod(blocked, 0o000)
    try:
        report = check_access(str(blocked), "video")
    finally:
        os.chmod(blocked, 0o644)  # restore so tmp_path cleanup can remove it

    assert report.state is AccessState.FORBIDDEN


def test_forbidden_remediation_names_seat_acl_for_video(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        access.os, "open", lambda path, flags: (_ for _ in ()).throw(PermissionError())
    )

    report = check_access("/dev/video0", "video")

    text = report.remediation.lower()
    assert "seat" in text or "logind" in text
    assert "video" in text  # the group-membership fallback is still named


def test_forbidden_remediation_names_audio_group_for_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        access.os, "open", lambda path, flags: (_ for _ in ()).throw(PermissionError())
    )

    report = check_access("/dev/snd/pcmC0D0c", "audio")

    text = report.remediation.lower()
    assert "audio" in text
    assert "group" in text


def test_check_access_unrecognized_oserror_reports_forbidden_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An errno this module doesn't specifically know must never be silently OK."""
    monkeypatch.setattr(
        access.os,
        "open",
        lambda path, flags: (_ for _ in ()).throw(OSError(errno.EIO, "I/O error")),
    )

    report = check_access("/dev/video0", "video")

    assert report.state is AccessState.FORBIDDEN
    assert report.remediation
    assert access_error(report).code == EXIT_ENV_ERROR


@pytest.mark.parametrize("code", [errno.ENODEV, errno.ENXIO])
@pytest.mark.parametrize("kind", ["video", "audio"])
def test_vanished_hardware_reports_absent_not_forbidden(
    monkeypatch: pytest.MonkeyPatch, code: int, kind: str
) -> None:
    """A node whose hardware vanished is ABSENT, not FORBIDDEN.

    ENODEV/ENXIO mean the node exists but there is no device behind it — the
    camera was unplugged between enumeration and open. Reporting that as
    FORBIDDEN hands the caller permission remediation (seat ACL, group
    membership) for a problem that is not a permission problem at all, which
    is the same dead end the absent/forbidden split exists to prevent.
    """
    monkeypatch.setattr(
        access.os,
        "open",
        lambda path, flags: (_ for _ in ()).throw(OSError(code, "gone")),
    )

    report = check_access("/dev/video0", kind)

    assert report.state is AccessState.ABSENT
    assert access_error(report).code == EXIT_USER_ERROR
    lowered = report.remediation.lower()
    assert "permission" not in lowered
    assert "seat" not in lowered
    assert "group" not in lowered
    assert "replug" in lowered or "unplug" in lowered


def test_absent_and_forbidden_are_never_conflated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two states must never share an outcome: state, remediation, or exit code."""
    monkeypatch.setattr(
        access.os, "open", lambda path, flags: (_ for _ in ()).throw(FileNotFoundError())
    )
    absent = check_access("/dev/video9", "video")

    monkeypatch.setattr(
        access.os, "open", lambda path, flags: (_ for _ in ()).throw(PermissionError())
    )
    forbidden = check_access("/dev/video9", "video")

    assert absent.state is AccessState.ABSENT
    assert forbidden.state is AccessState.FORBIDDEN
    assert absent.state != forbidden.state
    assert absent.remediation != forbidden.remediation

    absent_error = access_error(absent)
    forbidden_error = access_error(forbidden)
    assert absent_error.code == EXIT_USER_ERROR
    assert forbidden_error.code == EXIT_ENV_ERROR
    assert absent_error.code != forbidden_error.code
    assert absent_error.message != forbidden_error.message


# --- check_access: BUSY, holder naming, and the no-hang guarantee ----------


def _raise_ebusy(path: str, flags: int) -> int:
    raise OSError(errno.EBUSY, "Device or resource busy")


def test_check_access_busy_names_holder_when_determinable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access.os, "open", _raise_ebusy)
    monkeypatch.setattr(access, "find_holder", lambda path: Holder(pid=4242, command="zoom"))

    report = check_access("/dev/video0", "video")

    assert report.state is AccessState.BUSY
    assert report.holder == Holder(pid=4242, command="zoom")
    assert "zoom" in report.remediation
    assert "4242" in report.remediation
    assert access_error(report).code == EXIT_ENV_ERROR


def test_check_access_busy_degrades_to_unknown_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access.os, "open", _raise_ebusy)
    monkeypatch.setattr(access, "find_holder", lambda path: None)

    report = check_access("/dev/video0", "video")

    assert report.state is AccessState.BUSY
    assert report.holder is None
    assert report.remediation  # still actionable even without a named holder


def test_check_access_busy_audio_wording_differs_from_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access.os, "open", _raise_ebusy)
    monkeypatch.setattr(access, "find_holder", lambda path: None)

    video_report = check_access("/dev/video0", "video")
    audio_report = check_access("/dev/snd/pcmC0D0c", "audio")

    assert video_report.remediation != audio_report.remediation


def test_check_access_busy_is_bounded_and_never_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access.os, "open", _raise_ebusy)
    monkeypatch.setattr(access, "find_holder", lambda path: None)

    start = time.monotonic()
    report = check_access("/dev/video0", "video")
    elapsed = time.monotonic() - start

    assert report.state is AccessState.BUSY
    assert elapsed < 1.0  # single syscall attempt, no retries, no sleeps


def test_check_access_busy_with_real_proc_scan_is_still_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real (unmocked) find_holder against the real /proc on this
    host — read-only enumeration only, never opens the device itself — to
    prove the full busy path stays bounded without mocking the scan away."""
    monkeypatch.setattr(access.os, "open", _raise_ebusy)

    start = time.monotonic()
    report = check_access("/dev/__webcam_cli_test_missing_device__", "video")
    elapsed = time.monotonic() - start

    assert report.state is AccessState.BUSY
    assert elapsed < 5.0


def test_require_access_busy_raises_bounded_and_never_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access.os, "open", _raise_ebusy)
    monkeypatch.setattr(access, "find_holder", lambda path: Holder(pid=99, command="cheese"))

    start = time.monotonic()
    with pytest.raises(CliError) as exc_info:
        require_access("/dev/video0", "video")
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert exc_info.value.code == EXIT_ENV_ERROR
    assert "cheese" in exc_info.value.remediation
    assert "99" in exc_info.value.remediation


# --- require_access: the enforcing path -------------------------------------


def test_require_access_ok_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access.os, "open", lambda path, flags: 7)
    monkeypatch.setattr(access.os, "close", lambda fd: None)

    assert require_access("/dev/video0", "video") is None


def test_require_access_absent_raises_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        access.os, "open", lambda path, flags: (_ for _ in ()).throw(FileNotFoundError())
    )

    with pytest.raises(CliError) as exc_info:
        require_access("/dev/video0", "video")

    assert exc_info.value.code == EXIT_USER_ERROR
    assert exc_info.value.remediation


def test_require_access_forbidden_raises_env_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        access.os, "open", lambda path, flags: (_ for _ in ()).throw(PermissionError())
    )

    with pytest.raises(CliError) as exc_info:
        require_access("/dev/video0", "video")

    assert exc_info.value.code == EXIT_ENV_ERROR
    assert exc_info.value.remediation


# --- access_error -----------------------------------------------------------


def test_access_error_rejects_ok_state() -> None:
    report = AccessReport(path="/dev/video0", kind="video", state=AccessState.OK, remediation="")
    with pytest.raises(ValueError):
        access_error(report)


def test_access_error_messages_name_the_path_and_kind() -> None:
    absent = AccessReport(
        path="/dev/video3",
        kind="video",
        state=AccessState.ABSENT,
        remediation="fix it",
    )
    err = access_error(absent)
    assert "/dev/video3" in err.message
    assert "video" in err.message
    assert err.remediation == "fix it"


# --- find_holder: /proc scanning and graceful degradation ------------------


def test_find_holder_finds_self_via_real_proc(tmp_path) -> None:
    """Integration test against the real /proc, scoped to our own process —
    no camera or microphone involved."""
    held = tmp_path / "held-file"
    held.write_bytes(b"x")
    fd = os.open(str(held), os.O_RDONLY)
    try:
        holder = find_holder(str(held))
    finally:
        os.close(fd)

    assert holder is not None
    assert holder.pid == os.getpid()
    assert holder.command  # best-effort process name, non-empty


def test_find_holder_returns_none_when_nothing_holds_it(tmp_path) -> None:
    unheld = tmp_path / "unheld-file"
    unheld.write_bytes(b"x")

    assert find_holder(str(unheld)) is None


def test_find_holder_degrades_when_proc_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[str]:
        raise OSError("no /proc here")

    monkeypatch.setattr(access, "_list_proc_pids", boom)

    assert find_holder("/dev/video0") is None


def test_find_holder_degrades_when_realpath_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path: str) -> str:
        raise OSError("cannot resolve")

    monkeypatch.setattr(access.os.path, "realpath", boom)

    assert find_holder("/dev/video0") is None


def test_find_holder_skips_unreadable_pids_and_keeps_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pid owned by another user raises PermissionError listing its fds; the
    scan must not abort — it should keep going and still find the real match."""
    target = "/dev/video0"

    def fake_pids() -> list[str]:
        return ["111", "222", "self", "net"]  # "self"/"net" are non-numeric /proc entries

    def fake_fds(pid: str) -> list[str]:
        if pid == "111":
            raise PermissionError()
        if pid == "222":
            return ["3"]
        raise AssertionError(f"unexpected pid scanned: {pid}")

    def fake_readlink(fd_path: str) -> str:
        if fd_path == "/proc/222/fd/3":
            return target
        raise OSError("dangling symlink")

    def fake_command(pid: str) -> str:
        return "ffmpeg\n"

    monkeypatch.setattr(access, "_list_proc_pids", fake_pids)
    monkeypatch.setattr(access, "_list_fds", fake_fds)
    monkeypatch.setattr(access, "_readlink", fake_readlink)
    monkeypatch.setattr(access, "_read_command", fake_command)

    holder = find_holder(target)

    assert holder == Holder(pid=222, command="ffmpeg\n")


def test_find_holder_falls_back_to_unknown_command(monkeypatch: pytest.MonkeyPatch) -> None:
    target = "/dev/video0"

    monkeypatch.setattr(access, "_list_proc_pids", lambda: ["555"])
    monkeypatch.setattr(access, "_list_fds", lambda pid: ["9"])
    monkeypatch.setattr(access, "_readlink", lambda fd_path: target)

    def boom(pid: str) -> str:
        raise OSError("comm unreadable")

    monkeypatch.setattr(access, "_read_command", boom)

    holder = find_holder(target)

    assert holder == Holder(pid=555, command="unknown")


def test_find_holder_returns_none_when_no_pid_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access, "_list_proc_pids", lambda: ["111", "222"])
    monkeypatch.setattr(access, "_list_fds", lambda pid: ["3"])
    monkeypatch.setattr(access, "_readlink", lambda fd_path: "/dev/null")

    assert find_holder("/dev/video0") is None


def test_find_holder_is_bounded_over_many_pids(monkeypatch: pytest.MonkeyPatch) -> None:
    """A large, entirely non-matching /proc must still resolve quickly — no
    accidental quadratic blowup or retry loop hidden in the scan."""
    pids = [str(n) for n in range(2000)]
    monkeypatch.setattr(access, "_list_proc_pids", lambda: pids)
    monkeypatch.setattr(access, "_list_fds", lambda pid: ["0", "1", "2"])
    monkeypatch.setattr(access, "_readlink", lambda fd_path: "/dev/null")

    start = time.monotonic()
    holder = find_holder("/dev/video0")
    elapsed = time.monotonic() - start

    assert holder is None
    assert elapsed < 2.0
