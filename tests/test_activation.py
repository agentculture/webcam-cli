"""Tests for webcam_cli.activation — the append-only activation log.

Covers both acceptance criteria of task t4:

1. Every activation appends exactly one JSON line carrying device stable id,
   verb, named target, start/end timestamps, and pid.
2. No bytes are written anywhere except the named target/buffer and the
   activation log.

Plus the concurrency guarantee the API contract calls out explicitly: two
writers (processes or threads) must never interleave into a corrupted line.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from webcam_cli.activation import (
    ENV_LOG_PATH,
    Activation,
    activation_scope,
    log_path,
    record_activation,
)

# --- Activation dataclass ---------------------------------------------------


def test_activation_to_dict_shape() -> None:
    act = Activation(
        device_id="usb-046d_C270_HD_WEBCAM_200901010001",
        verb="stream",
        target="rtsp://127.0.0.1:8554/c270",
        started_at="2026-07-24T12:00:00+00:00",
        ended_at=None,
        pid=4321,
        detail={"format": "mjpeg", "resolution": "1280x720"},
    )
    assert act.to_dict() == {
        "device_id": "usb-046d_C270_HD_WEBCAM_200901010001",
        "verb": "stream",
        "target": "rtsp://127.0.0.1:8554/c270",
        "started_at": "2026-07-24T12:00:00+00:00",
        "ended_at": None,
        "pid": 4321,
        "detail": {"format": "mjpeg", "resolution": "1280x720"},
    }


def test_activation_is_frozen() -> None:
    act = Activation(
        device_id="d",
        verb="stream",
        target="t",
        started_at="s",
        ended_at=None,
        pid=1,
        detail={},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        act.device_id = "other"  # type: ignore[misc]


# --- log_path() --------------------------------------------------------------


def test_log_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "custom" / "activations.jsonl"
    monkeypatch.setenv(ENV_LOG_PATH, str(override))
    assert log_path() == override


def test_log_path_xdg_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ENV_LOG_PATH, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert log_path() == tmp_path / "webcam-cli" / "activations.jsonl"


def test_log_path_falls_back_to_local_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENV_LOG_PATH, raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert log_path() == tmp_path / ".local" / "state" / "webcam-cli" / "activations.jsonl"


def test_log_path_does_not_touch_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENV_LOG_PATH, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "nowhere"))
    log_path()
    assert list(tmp_path.rglob("*")) == []


# --- record_activation: exactly one line ------------------------------------


def test_record_activation_appends_exactly_one_line(tmp_path: Path) -> None:
    log = tmp_path / "activations.jsonl"
    act = Activation(
        device_id="usb-046d_C270_HD_WEBCAM_200901010001",
        verb="record",
        target=str(tmp_path / "clip.mp4"),
        started_at="2026-07-24T12:00:00+00:00",
        ended_at="2026-07-24T12:00:05+00:00",
        pid=os.getpid(),
        detail={"fps": 30},
    )
    record_activation(act, path=log)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == act.to_dict()


def test_record_activation_creates_parent_dirs(tmp_path: Path) -> None:
    log = tmp_path / "a" / "b" / "c" / "activations.jsonl"
    assert not log.parent.exists()
    act = Activation(
        device_id="d", verb="stream", target="t", started_at="s", ended_at="e", pid=1, detail={}
    )
    record_activation(act, path=log)
    assert log.exists()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_record_activation_appends_without_truncating(tmp_path: Path) -> None:
    log = tmp_path / "activations.jsonl"
    for i in range(3):
        act = Activation(
            device_id=f"dev-{i}",
            verb="stream",
            target="t",
            started_at="s",
            ended_at="e",
            pid=1,
            detail={},
        )
        record_activation(act, path=log)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["device_id"] for line in lines] == ["dev-0", "dev-1", "dev-2"]


def test_record_activation_propagates_write_failures(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    bad_path = blocker / "activations.jsonl"  # blocker is a file, not a dir
    act = Activation(
        device_id="d", verb="stream", target="t", started_at="s", ended_at="e", pid=1, detail={}
    )
    with pytest.raises(OSError):
        record_activation(act, path=bad_path)


# --- activation_scope: exactly one line, including on crash -----------------


def test_activation_scope_writes_nothing_until_exit(tmp_path: Path) -> None:
    log = tmp_path / "activations.jsonl"
    cm = activation_scope(device_id="dev", verb="stream", target="t", path=log)
    act = cm.__enter__()
    try:
        assert act.ended_at is None
        assert not log.exists()
    finally:
        cm.__exit__(None, None, None)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ended_at"] is not None


def test_activation_scope_writes_one_line_on_success(tmp_path: Path) -> None:
    log = tmp_path / "activations.jsonl"
    with activation_scope(
        device_id="usb-046d_C270_HD_WEBCAM_200901010001",
        verb="stream",
        target="rtsp://127.0.0.1:8554/c270",
        detail={"format": "mjpeg", "resolution": "1280x720"},
        path=log,
    ) as act:
        assert act.device_id == "usb-046d_C270_HD_WEBCAM_200901010001"
        assert act.verb == "stream"
        assert act.ended_at is None
        assert act.pid == os.getpid()

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["device_id"] == "usb-046d_C270_HD_WEBCAM_200901010001"
    assert record["verb"] == "stream"
    assert record["target"] == "rtsp://127.0.0.1:8554/c270"
    assert record["pid"] == os.getpid()
    assert record["started_at"]
    assert record["ended_at"]
    assert record["detail"] == {"format": "mjpeg", "resolution": "1280x720"}


def test_activation_scope_writes_one_line_on_exception(tmp_path: Path) -> None:
    log = tmp_path / "activations.jsonl"
    # Constructing the context manager cannot raise (it's a plain @contextmanager
    # object, nothing runs until __enter__); hoisting it above pytest.raises leaves
    # exactly one call inside the block that can actually throw.
    cm = activation_scope(
        device_id="usb-1234_arducam",
        verb="record",
        target="/tmp/clip.mp4",
        path=log,
    )
    with pytest.raises(RuntimeError, match="boom"):
        with cm:
            raise RuntimeError("boom")

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["device_id"] == "usb-1234_arducam"
    assert record["ended_at"] is not None
    assert "RuntimeError" in record["detail"]["error"]
    assert "boom" in record["detail"]["error"]


def test_activation_scope_preserves_caller_error_detail(tmp_path: Path) -> None:
    log = tmp_path / "activations.jsonl"
    # Same hoist as above: build the context manager (cannot raise) before
    # pytest.raises so only the deliberate raise below is inside the block.
    cm = activation_scope(
        device_id="dev",
        verb="stream",
        target="t",
        detail={"error": "already explained by caller"},
        path=log,
    )
    with pytest.raises(ValueError):
        with cm:
            raise ValueError("nope")

    record = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert record["detail"]["error"] == "already explained by caller"


def test_multiple_activations_produce_multiple_distinct_lines(tmp_path: Path) -> None:
    log = tmp_path / "activations.jsonl"
    for i in range(4):
        with activation_scope(device_id=f"dev-{i}", verb="stream", target=f"target-{i}", path=log):
            pass

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    device_ids = [json.loads(line)["device_id"] for line in lines]
    assert device_ids == [f"dev-{i}" for i in range(4)]


# --- criterion 2: no bytes anywhere except target and log --------------------


def test_activation_module_writes_only_the_log(tmp_path: Path) -> None:
    log = tmp_path / "state" / "activations.jsonl"

    with activation_scope(device_id="usb-x", verb="stream", target="rtsp://out", path=log):
        pass  # this module never touches "target" — capture is out of lane

    # Hoisted for the same reason as the two tests above: construction cannot
    # raise, so only the deliberate raise below is inside pytest.raises.
    cm = activation_scope(
        device_id="usb-x", verb="record", target=str(tmp_path / "clip.mp4"), path=log
    )
    with pytest.raises(RuntimeError):
        with cm:
            raise RuntimeError("crashed mid-record")

    created_files = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert created_files == {log}


def test_activation_module_never_creates_the_named_target(tmp_path: Path) -> None:
    log = tmp_path / "activations.jsonl"
    target = tmp_path / "would-be-captured-frame.jpg"

    with activation_scope(device_id="usb-x", verb="stream", target=str(target), path=log):
        pass

    assert not target.exists()


# --- concurrency: appends from multiple processes must not interleave -------


def _process_worker(log_path_str: str, count: int, device_id: str) -> None:
    path = Path(log_path_str)
    for i in range(count):
        record_activation(
            Activation(
                device_id=device_id,
                verb="stream",
                target=f"/tmp/target-{i}",
                started_at="2026-01-01T00:00:00+00:00",
                ended_at="2026-01-01T00:00:01+00:00",
                pid=os.getpid(),
                detail={"i": i},
            ),
            path=path,
        )


def test_concurrent_process_appends_do_not_interleave(tmp_path: Path) -> None:
    log = tmp_path / "activations.jsonl"
    per_process = 40
    n_processes = 6

    procs = [
        multiprocessing.Process(target=_process_worker, args=(str(log), per_process, f"dev-{i}"))
        for i in range(n_processes)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == per_process * n_processes

    # json.loads raising here would mean a line was corrupted by interleaving.
    parsed = [json.loads(line) for line in lines]

    counts: dict[str, int] = {}
    for record in parsed:
        counts[record["device_id"]] = counts.get(record["device_id"], 0) + 1
    assert counts == {f"dev-{i}": per_process for i in range(n_processes)}


def test_concurrent_thread_activation_scope_do_not_interleave(tmp_path: Path) -> None:
    log = tmp_path / "activations.jsonl"
    n_threads = 64

    def _run(i: int) -> None:
        with activation_scope(
            device_id=f"thread-dev-{i}",
            verb="stream" if i % 2 == 0 else "record",
            target=f"/tmp/target-{i}",
            detail={"i": i},
            path=log,
        ):
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(_run, range(n_threads)))

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads

    parsed = [json.loads(line) for line in lines]
    ids = {record["device_id"] for record in parsed}
    assert ids == {f"thread-dev-{i}" for i in range(n_threads)}
    assert all(record["ended_at"] is not None for record in parsed)
