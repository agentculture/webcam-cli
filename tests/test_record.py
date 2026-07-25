"""Tests for ``webcam record`` — bounded-by-construction clip/audio recording.

Hermetic and hardware-free by construction, per the operator-approved wave-2
deviation (d1): every subprocess/engine boundary is monkeypatched in every
test. No test ever lets a real ``subprocess.Popen`` run, opens a real device
node, or calls the real :func:`webcam_cli.engine.probe_formats` (which shells
out to ``gst-device-monitor-1.0`` and briefly opens a camera). Dry-run tests
go one step further and patch ``subprocess.Popen`` to *fail the test* if it
is ever invoked at all — the cheapest possible proof that dry-run touches no
hardware.

Covers the three acceptance criteria of build-plan task t7:

1. ``record`` enforces a bound: a default cap is applied when none is given,
   and an unbounded recording is not expressible via any flag combination —
   see the "Bound" and "escape hatch" sections below, including a test that
   actively tries to out-wait a pipeline that ignores its stop signal.
2. ``record --apply`` writes exactly one artifact at the named path; its
   ``--json`` reports the negotiated format, the enforced bound, the output
   path, and timestamps — see the "apply" section.
3. Dry-run resolves and validates without energizing hardware — see the
   "dry-run" section.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import subprocess
from pathlib import Path

import pytest

from webcam_cli import access, activation, engine
from webcam_cli.cli._commands import record
from webcam_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from webcam_cli.devices import AudioCard, LogicalDevice, VideoNode

# ---------------------------------------------------------------------------
# fixtures / fakes
# ---------------------------------------------------------------------------


def _audio_card() -> AudioCard:
    return AudioCard(
        index=1, card_id="WEBCAM", name="C270 HD WEBCAM", alsa_address="hw:CARD=WEBCAM,DEV=0"
    )


def _device_with_mic() -> LogicalDevice:
    return LogicalDevice(
        stable_id="usb-046d_C270_fake",
        label="HD Webcam C270",
        usb_path="3-1",
        video_nodes=(
            VideoNode(path="/dev/video0", by_id="/dev/v4l/by-id/fake-video-index0", index=0),
            VideoNode(path="/dev/video1", by_id="/dev/v4l/by-id/fake-video-index1", index=1),
        ),
        capture_node="/dev/video0",
        audio=_audio_card(),
    )


def _device_video_only() -> LogicalDevice:
    return LogicalDevice(
        stable_id="usb-fake-video-only",
        label="Video Only Cam",
        usb_path="3-2",
        video_nodes=(VideoNode(path="/dev/video2", by_id="/dev/v4l/by-id/fake2", index=0),),
        capture_node="/dev/video2",
        audio=None,
    )


def _device_mic_only() -> LogicalDevice:
    return LogicalDevice(
        stable_id="usb-fake-mic-only",
        label="Reachy Mini Audio",
        usb_path="5-1",
        video_nodes=(),
        capture_node=None,
        audio=_audio_card(),
    )


DEVICE = _device_with_mic()

# Captured at collection time, before the ``env`` fixture ever monkeypatches
# ``access.check_access`` on the shared module object -- ``record.access`` and
# this module's ``access`` are literally the same module, so grabbing the
# "real" function *inside* a test that also uses ``env`` would just re-read
# whatever ``env`` already substituted. Tests that need to exercise the
# genuine implementation (e.g. proving no-flag dry-run never reaches
# ``os.open``) restore this reference explicitly.
_REAL_CHECK_ACCESS = access.check_access


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="webcam-record-test")
    sub = parser.add_subparsers(dest="command")
    record.register(sub)
    return parser


def _parse(argv: list[str]) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


def _ok_report(path: str, kind: str) -> access.AccessReport:
    return access.AccessReport(path=path, kind=kind, state=access.AccessState.OK, remediation="")


def _capability(available: bool = True) -> engine.Capability:
    plugins = {name: True for name in ("v4l2src", "alsasrc", "matroskamux", "fakesink")}
    return engine.Capability(
        gst_launch="/usr/bin/gst-launch-1.0",
        gst_inspect="/usr/bin/gst-inspect-1.0",
        plugins=plugins,
        available=available,
    )


_SAMPLE_FORMATS = (
    engine.VideoFormat(pixel_format="MJPG", width=1280, height=720, fps=30.0),
    engine.VideoFormat(pixel_format="YUYV", width=640, height=480, fps=30.0),
)


class _RaisingPopen:
    """A Popen stand-in that fails the test if it is ever constructed.

    Used as the default for every dry-run test: if dry-run ever tries to
    spawn a subprocess (i.e. actually run gst-launch-1.0), that is exactly
    the "energizes hardware" failure criterion 3 forbids.
    """

    def __call__(self, *args: object, **kwargs: object) -> None:  # pragma: no cover - failure path
        raise AssertionError(f"subprocess.Popen must not be called in dry-run, got {args!r}")


class _ImmediateProc:
    """A Popen stand-in whose pipeline "completes" instantly, no signals needed."""

    def __init__(self, argv: list[str], on_run=None) -> None:
        self.argv = argv
        self.returncode = 0
        self._on_run = on_run
        if on_run is not None:
            on_run(argv)

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return "", ""

    def send_signal(self, sig: int) -> None:  # pragma: no cover - not expected to be hit
        raise AssertionError("an instantly-completing process should never be signalled")

    def kill(self) -> None:  # pragma: no cover
        raise AssertionError("an instantly-completing process should never be killed")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """Standard hermetic environment: resolves to DEVICE, everything reports OK/available.

    Individual tests override specific pieces (a different device, a busy
    report, a missing engine, ...) via further ``monkeypatch`` calls.
    Dry-run safety net: ``subprocess.Popen`` raises if invoked at all.
    """
    monkeypatch.setenv(activation.ENV_LOG_PATH, str(tmp_path / "activations.jsonl"))
    monkeypatch.setattr(record.devices, "resolve", lambda selector, root="/": DEVICE)
    monkeypatch.setattr(record.access, "check_access", lambda path, kind: _ok_report(path, kind))
    monkeypatch.setattr(record.access, "require_access", lambda path, kind: None)
    monkeypatch.setattr(record.engine, "detect", lambda: _capability())
    monkeypatch.setattr(record.engine, "require_engine", lambda: _capability())
    monkeypatch.setattr(record.subprocess, "Popen", _RaisingPopen())
    return {"tmp_path": tmp_path}


def _out(argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    args = _parse(argv)
    rc = args.func(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


# ===========================================================================
# Criterion 1 — bounded by construction; unbounded is not expressible.
# ===========================================================================


class TestBoundConstruction:
    """Direct, type-level "escape attempts" — bypassing argparse entirely."""

    def test_rejects_both_absent(self) -> None:
        with pytest.raises(CliError) as exc:
            # type: ignore[arg-type] -- deliberately passing None to prove it is refused
            record.Bound(duration_s=None, max_bytes=None, duration_is_default=True)
        assert exc.value.code == EXIT_USER_ERROR

    def test_rejects_zero_duration(self) -> None:
        with pytest.raises(CliError):
            record.Bound(duration_s=0.0, max_bytes=None, duration_is_default=False)

    def test_rejects_negative_duration(self) -> None:
        with pytest.raises(CliError):
            record.Bound(duration_s=-5.0, max_bytes=None, duration_is_default=False)

    def test_rejects_infinite_duration(self) -> None:
        with pytest.raises(CliError):
            record.Bound(duration_s=math.inf, max_bytes=None, duration_is_default=False)

    def test_rejects_negative_infinite_duration(self) -> None:
        with pytest.raises(CliError):
            record.Bound(duration_s=-math.inf, max_bytes=None, duration_is_default=False)

    def test_rejects_nan_duration(self) -> None:
        with pytest.raises(CliError):
            record.Bound(duration_s=math.nan, max_bytes=None, duration_is_default=False)

    def test_rejects_duration_over_hard_ceiling(self) -> None:
        with pytest.raises(CliError):
            record.Bound(
                duration_s=record._MAX_DURATION_S + 1,
                max_bytes=None,
                duration_is_default=False,
            )

    def test_rejects_absurdly_large_duration(self) -> None:
        """A caller cannot approximate 'forever' with a very large finite number either."""
        with pytest.raises(CliError):
            record.Bound(duration_s=10**9, max_bytes=None, duration_is_default=False)

    def test_rejects_zero_max_bytes(self) -> None:
        with pytest.raises(CliError):
            record.Bound(duration_s=10.0, max_bytes=0, duration_is_default=False)

    def test_rejects_negative_max_bytes(self) -> None:
        with pytest.raises(CliError):
            record.Bound(duration_s=10.0, max_bytes=-1, duration_is_default=False)

    def test_rejects_max_bytes_over_hard_ceiling(self) -> None:
        with pytest.raises(CliError):
            record.Bound(
                duration_s=10.0, max_bytes=record._MAX_BYTES_CEILING + 1, duration_is_default=False
            )

    def test_accepts_valid_bound(self) -> None:
        bound = record.Bound(duration_s=10.0, max_bytes=1000, duration_is_default=False)
        assert bound.as_dict() == {
            "duration_s": 10.0,
            "duration_is_default": False,
            "max_bytes": 1000,
        }

    def test_bound_is_frozen(self) -> None:
        bound = record.Bound(duration_s=10.0, max_bytes=None, duration_is_default=False)
        with pytest.raises(dataclasses.FrozenInstanceError, match="duration_s"):
            bound.duration_s = 5.0  # type: ignore[misc]


class TestBuildBound:
    def test_default_applied_when_duration_omitted(self) -> None:
        bound = record._build_bound(None, None)
        assert bound.duration_s == record._DEFAULT_DURATION_S
        assert bound.duration_is_default is True
        assert bound.max_bytes is None

    def test_explicit_duration_marked_non_default(self) -> None:
        bound = record._build_bound(45.0, None)
        assert bound.duration_s == 45.0
        assert bound.duration_is_default is False

    def test_size_only_request_still_gets_a_default_duration(self) -> None:
        """The heart of the guarantee: --max-bytes alone never removes the time cap."""
        bound = record._build_bound(None, 5_000_000)
        assert bound.duration_s == record._DEFAULT_DURATION_S
        assert bound.duration_is_default is True
        assert bound.max_bytes == 5_000_000


class TestArgparseEscapeHatches:
    """Prove the CLI surface itself rejects every value that would mean 'forever'."""

    @pytest.mark.parametrize("raw", ["0", "-1", "-100.5", "inf", "-inf", "nan", "99999999"])
    def test_duration_value_rejected(self, raw: str) -> None:
        with pytest.raises(SystemExit):
            _parse(["record", "C270", "/tmp/out.mkv", "--duration", raw])

    @pytest.mark.parametrize("raw", ["0", "-1", "99999999999999999999"])
    def test_max_bytes_value_rejected(self, raw: str) -> None:
        with pytest.raises(SystemExit):
            _parse(["record", "C270", "/tmp/out.mkv", "--max-bytes", raw])

    @pytest.mark.parametrize(
        "flag", ["--no-limit", "--unbounded", "--forever", "--infinite", "--no-duration"]
    )
    def test_no_unbounded_flag_exists_on_the_surface(self, flag: str) -> None:
        with pytest.raises(SystemExit):
            _parse(["record", "C270", "/tmp/out.mkv", flag])

    def test_parser_defines_no_unbounded_sentinel_action(self) -> None:
        """Introspect the parser itself: no action whose name suggests 'no limit'."""
        parser = _build_parser()
        record_parser = parser._subparsers._group_actions[0].choices["record"]
        dests = {action.dest for action in record_parser._actions}
        forbidden = {"no_limit", "unbounded", "forever", "infinite", "no_duration"}
        assert dests.isdisjoint(forbidden)

    def test_duration_omitted_uses_default(self, env, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _out(["record", "C270", "/tmp/out.mkv", "--json"], capsys)
        assert payload["bound"]["duration_s"] == record._DEFAULT_DURATION_S
        assert payload["bound"]["duration_is_default"] is True


class TestBoundedPhaseEscapeAttempt:
    """White-box tests of the bounding mechanism itself (`_run_bounded_phase`).

    These deliberately simulate a pipeline that behaves badly, to prove the
    wall-clock bound holds regardless — the "actively tries to escape the
    bound" test the task calls for.
    """

    def test_completes_cleanly_needs_no_signal(self) -> None:
        proc = _ImmediateProc(["gst-launch-1.0"])
        result = record._run_bounded_phase(
            ["gst-launch-1.0"],
            deadline_s=5.0,
            max_bytes=None,
            output_path=None,
            popen_factory=lambda argv, **kw: proc,
            clock=iter([0.0, 0.0]).__next__,
        )
        assert result.stopped_reason == "completed"
        assert result.returncode == 0

    def test_duration_bound_stops_a_responsive_pipeline(self) -> None:
        """The pipeline honours SIGINT (the normal case): no SIGKILL needed."""

        class _Responsive:
            def __init__(self, argv):
                self.argv = argv
                self.returncode = None
                self.signals: list[int] = []
                self.killed = False

            def communicate(self, timeout=None):
                if self.signals:
                    self.returncode = 0
                    return "", ""
                raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)

            def send_signal(self, sig):
                self.signals.append(sig)

            def kill(self):  # pragma: no cover - must not be needed
                self.killed = True

        proc = _Responsive(["gst-launch-1.0"])
        clock = iter([0.0, 0.5, 1.0, 5.0, 5.1]).__next__
        result = record._run_bounded_phase(
            ["gst-launch-1.0"],
            deadline_s=5.0,
            max_bytes=None,
            output_path=None,
            popen_factory=lambda argv, **kw: proc,
            clock=clock,
            poll_s=0.5,
            grace_s=2.0,
        )
        assert result.stopped_reason == "duration"
        assert proc.signals == [record.signal.SIGINT]
        assert proc.killed is False

    def test_duration_bound_escalates_to_sigkill_when_pipeline_ignores_everything(self) -> None:
        """The escape attempt: the fake pipeline NEVER responds to SIGINT.

        Every ``communicate()`` call times out, forever, until ``kill()`` is
        called — simulating a wedged/misbehaving GStreamer pipeline. Proves
        the bound still holds: this call returns, SIGKILL is sent, and the
        function never blocks indefinitely waiting for the child.
        """

        class _NeverResponds:
            def __init__(self, argv):
                self.argv = argv
                self.returncode = None
                self.signals: list[int] = []
                self.killed = False

            def communicate(self, timeout=None):
                if self.killed:
                    self.returncode = -9
                    return "", ""
                raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)

            def send_signal(self, sig):
                self.signals.append(sig)

            def kill(self):
                self.killed = True

        proc = _NeverResponds(["gst-launch-1.0"])
        # Clock advances forever; the loop must still terminate once elapsed
        # crosses the deadline, without ever needing more "time" than
        # deadline_s + grace_s to elapse before the code gives up on grace.
        clock_values = iter([i * 0.5 for i in range(100)])
        result = record._run_bounded_phase(
            ["gst-launch-1.0"],
            deadline_s=2.0,
            max_bytes=None,
            output_path=None,
            popen_factory=lambda argv, **kw: proc,
            clock=clock_values.__next__,
            poll_s=0.5,
            grace_s=1.0,
        )
        assert result.stopped_reason == "duration"
        assert record.signal.SIGINT in proc.signals
        assert proc.killed is True
        assert result.returncode == -9

    def test_size_bound_triggers_before_duration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _NeverExits:
            def __init__(self, argv):
                self.argv = argv
                self.returncode = None
                self.signals: list[int] = []

            def communicate(self, timeout=None):
                if self.signals:
                    self.returncode = 0
                    return "", ""
                raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)

            def send_signal(self, sig):
                self.signals.append(sig)

            def kill(self):  # pragma: no cover
                raise AssertionError("size bound should stop this gracefully via SIGINT")

        sizes = iter([0, 10, 999_999])
        monkeypatch.setattr(record, "_current_size", lambda path: next(sizes))
        proc = _NeverExits(["gst-launch-1.0"])
        result = record._run_bounded_phase(
            ["gst-launch-1.0"],
            deadline_s=100.0,  # duration cap far away; size must win
            max_bytes=500_000,
            output_path="/tmp/does-not-matter.mkv",
            popen_factory=lambda argv, **kw: proc,
            clock=iter([0.0, 0.1, 0.2, 0.3, 0.4]).__next__,
            poll_s=0.1,
            grace_s=1.0,
        )
        assert result.stopped_reason == "size"

    def test_popen_start_failure_is_a_typed_env_error(self) -> None:
        def _boom(*args, **kwargs):
            raise FileNotFoundError("no such file: gst-launch-1.0")

        clock = iter([0.0]).__next__
        with pytest.raises(CliError) as exc:
            record._run_bounded_phase(
                ["gst-launch-1.0"],
                deadline_s=5.0,
                max_bytes=None,
                output_path=None,
                popen_factory=_boom,
                clock=clock,
            )
        assert exc.value.code == EXIT_ENV_ERROR


# ===========================================================================
# Criterion 3 — dry-run resolves and validates without energizing hardware.
# ===========================================================================


class TestDryRun:
    def test_default_dry_run_never_touches_subprocess_or_probes(
        self, env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom(*_a, **_k):  # pragma: no cover - failure path
            raise AssertionError("probe_formats must not be called without --probe")

        monkeypatch.setattr(record.engine, "probe_formats", _boom)
        payload = _out(["record", "C270", "/tmp/out.mkv", "--json"], capsys)
        assert payload["mode"] == "dry-run"
        assert payload["apply"] is False

    def test_dry_run_writes_no_activation_log(
        self, env, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        _out(["record", "C270", "/tmp/out.mkv", "--json"], capsys)
        assert not (tmp_path / "activations.jsonl").exists()

    def test_dry_run_creates_no_file(
        self, env, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        output = tmp_path / "clip.mkv"
        _out(["record", "C270", str(output), "--json"], capsys)
        assert not output.exists()

    def test_dry_run_reports_would_write_the_named_path(
        self, env, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        output = tmp_path / "clip.mkv"
        payload = _out(["record", "C270", str(output), "--json"], capsys)
        assert payload["would_write"] == [str(output)]
        assert payload["output_path"] == str(output)

    def test_dry_run_reports_default_bound(self, env, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _out(["record", "C270", "/tmp/out.mkv", "--json"], capsys)
        assert payload["bound"]["duration_s"] == record._DEFAULT_DURATION_S
        assert payload["bound"]["duration_is_default"] is True

    def test_dry_run_reports_default_video_warmup(
        self, env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = _out(["record", "C270", "/tmp/out.mkv", "--json"], capsys)
        assert payload["warmup_s"] == record._DEFAULT_WARMUP_VIDEO_S

    def test_dry_run_reports_default_audio_warmup(
        self, env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = _out(["record", "C270", "/tmp/out.mkv", "--kind", "audio", "--json"], capsys)
        assert payload["warmup_s"] == record._DEFAULT_WARMUP_AUDIO_S

    def test_dry_run_full_video_format_request_validates_structurally(
        self, env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = _out(
            [
                "record",
                "C270",
                "/tmp/out.mkv",
                "--pixel-format",
                "mjpg",
                "--width",
                "1280",
                "--height",
                "720",
                "--fps",
                "30",
                "--json",
            ],
            capsys,
        )
        assert payload["video_format"]["requested"] == {
            "pixel_format": "MJPG",
            "width": 1280,
            "height": 720,
            "fps": 30.0,
        }
        assert payload["video_format"]["planned"] == payload["video_format"]["requested"]
        assert payload["video_format"]["probed"] is False
        assert payload["pipeline_preview"][0] in ("gst-launch-1.0", "/usr/bin/gst-launch-1.0")
        assert "v4l2src" in payload["pipeline_preview"]

    def test_dry_run_partial_video_format_request_errors(
        self, env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _parse(["record", "C270", "/tmp/out.mkv", "--width", "640"])
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_USER_ERROR

    def test_dry_run_partial_audio_format_request_errors(
        self, env, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _parse(["record", "C270", "/tmp/out.mkv", "--kind", "audio", "--rate", "48000"])
        with pytest.raises(CliError):
            args.func(args)

    def test_video_flags_rejected_for_audio_kind(self, env) -> None:
        args = _parse(
            [
                "record",
                "C270",
                "/tmp/out.mkv",
                "--kind",
                "audio",
                "--width",
                "640",
                "--height",
                "480",
                "--fps",
                "30",
                "--pixel-format",
                "mjpg",
            ]
        )
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_USER_ERROR

    def test_audio_flags_rejected_for_video_kind(self, env) -> None:
        args = _parse(
            [
                "record",
                "C270",
                "/tmp/out.mkv",
                "--kind",
                "video",
                "--rate",
                "48000",
                "--channels",
                "1",
            ]
        )
        with pytest.raises(CliError):
            args.func(args)

    def test_video_kind_on_mic_only_device_errors(
        self, monkeypatch: pytest.MonkeyPatch, env
    ) -> None:
        monkeypatch.setattr(record.devices, "resolve", lambda s, root="/": _device_mic_only())
        args = _parse(["record", "reachy", "/tmp/out.mkv"])
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_USER_ERROR
        assert "no camera" in exc.value.message

    def test_av_kind_on_video_only_device_errors(
        self, monkeypatch: pytest.MonkeyPatch, env
    ) -> None:
        monkeypatch.setattr(record.devices, "resolve", lambda s, root="/": _device_video_only())
        args = _parse(["record", "cam", "/tmp/out.mkv", "--kind", "av"])
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert "no paired microphone" in exc.value.message

    def test_audio_kind_on_video_only_device_errors(
        self, monkeypatch: pytest.MonkeyPatch, env
    ) -> None:
        monkeypatch.setattr(record.devices, "resolve", lambda s, root="/": _device_video_only())
        args = _parse(["record", "cam", "/tmp/out.mkv", "--kind", "audio"])
        with pytest.raises(CliError):
            args.func(args)

    def test_probe_with_audio_kind_errors(self, env) -> None:
        args = _parse(["record", "C270", "/tmp/out.mkv", "--kind", "audio", "--probe"])
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_USER_ERROR

    def test_output_inside_missing_directory_errors(self, env) -> None:
        args = _parse(["record", "C270", "/no/such/directory/out.mkv"])
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_USER_ERROR

    def test_output_that_is_a_directory_errors(self, env, tmp_path: Path) -> None:
        args = _parse(["record", "C270", str(tmp_path)])
        with pytest.raises(CliError):
            args.func(args)

    def test_default_dry_run_never_calls_check_access(
        self, env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression test for the Qodo-flagged bug: no-flag dry-run must not

        call :func:`webcam_cli.access.check_access` at all -- that function
        performs a real ``os.open()``/``os.close()`` on the device node,
        which contradicts the documented "no flag = opens nothing, logs
        nothing" guarantee (see the module docstring's "Dry-run / --probe
        split" and ``CLAUDE.md``'s three-level hardware rule).
        """

        def _boom(*_a, **_k):  # pragma: no cover - failure path
            raise AssertionError("check_access must not be called in a no-flag dry-run")

        monkeypatch.setattr(record.access, "check_access", _boom)
        payload = _out(["record", "C270", "/tmp/out.mkv", "--json"], capsys)
        assert payload["mode"] == "dry-run"

    def test_default_dry_run_never_calls_os_open(
        self, env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Belt-and-suspenders: no *transitive* path to ``os.open`` either.

        Deliberately restores the *real* ``access.check_access`` (undoing the
        ``env`` fixture's convenience stub) so this test exercises the actual
        production call graph, then patches ``os.open`` as seen from
        :mod:`webcam_cli.access` -- the only module in this call graph that
        would ever call it -- to prove no code path between ``cmd_record``
        and a device node reaches ``open(2)`` on a no-flag dry-run.
        """

        def _boom(*_a, **_k):  # pragma: no cover - failure path
            raise AssertionError("os.open must not be called in a no-flag dry-run")

        monkeypatch.setattr(record.access, "check_access", _REAL_CHECK_ACCESS)
        monkeypatch.setattr(access.os, "open", _boom)
        payload = _out(["record", "C270", "/tmp/out.mkv", "--json"], capsys)
        assert payload["mode"] == "dry-run"

    def test_dry_run_access_reports_absent_for_missing_node(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        # Deliberately a path guaranteed not to exist -- unlike other tests
        # in this suite, DEVICE.capture_node ("/dev/video0") cannot be relied
        # on here: this repo runs against real hardware on some hosts, where
        # that path genuinely exists.
        missing = tmp_path / "no-such-video-node"
        device = dataclasses.replace(DEVICE, capture_node=str(missing))
        monkeypatch.setattr(record.devices, "resolve", lambda s, root="/": device)
        payload = _out(["record", "C270", "/tmp/out.mkv", "--json"], capsys)
        assert payload["access"]["video"]["state"] == "absent"
        assert payload["access"]["video"]["path"] == str(missing)
        assert payload["access"]["video"]["remediation"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
    def test_dry_run_access_reports_forbidden_for_unreadable_node(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        blocked = tmp_path / "blocked-video"
        blocked.write_bytes(b"")
        os.chmod(blocked, 0o000)
        device = dataclasses.replace(DEVICE, capture_node=str(blocked))
        monkeypatch.setattr(record.devices, "resolve", lambda s, root="/": device)
        try:
            payload = _out(["record", "C270", "/tmp/out.mkv", "--json"], capsys)
        finally:
            os.chmod(blocked, 0o644)  # restore so tmp_path cleanup can remove it
        assert payload["access"]["video"]["state"] == "forbidden"
        assert payload["access"]["video"]["path"] == str(blocked)

    def test_dry_run_access_reports_unknown_when_node_exists_and_permitted(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Busy is genuinely not determinable without opening (see access.py's

        ``busy_error`` docstring: V4L2 exclusivity is only enforced at
        S_FMT/STREAMON, not open(2)) -- a node that exists and looks
        permitted must be reported ``"unknown"``, never ``"ok"``, and the
        remediation must say plainly that --probe/--apply is what finds out.
        """
        node = tmp_path / "video0"
        node.write_bytes(b"")
        device = dataclasses.replace(DEVICE, capture_node=str(node))
        monkeypatch.setattr(record.devices, "resolve", lambda s, root="/": device)
        payload = _out(["record", "C270", "/tmp/out.mkv", "--json"], capsys)
        assert payload["access"]["video"]["state"] == "unknown"
        text = payload["access"]["video"]["remediation"].lower()
        assert "busy" in text or "ebusy" in text
        assert "--probe" in text or "--apply" in text

    def test_probe_dry_run_still_uses_real_check_access_for_access_block(
        self, env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Requirement 3: --probe/--apply keep the existing real check_access

        behaviour unchanged -- only the no-flag dry-run path changes.
        """
        calls: list[tuple[str, str]] = []

        def _spy(path: str, kind: str) -> access.AccessReport:
            calls.append((path, kind))
            return _ok_report(path, kind)

        monkeypatch.setattr(record.access, "check_access", _spy)
        monkeypatch.setattr(record.engine, "probe_formats", lambda node: _SAMPLE_FORMATS)
        payload = _out(["record", "C270", "/tmp/out.mkv", "--probe", "--json"], capsys)
        assert ("/dev/video0", "video") in calls
        assert payload["access"]["video"]["state"] == "ok"

    def test_dry_run_text_mode_smoke(self, env, capsys: pytest.CaptureFixture[str]) -> None:
        args = _parse(["record", "C270", "/tmp/out.mkv"])
        rc = args.func(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "record: dry-run" in out
        assert "would_write:" in out


class TestDryRunProbe:
    def test_probe_calls_probe_formats_and_negotiates(
        self, env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls: list[str] = []

        def _fake_probe(node_path: str) -> tuple[engine.VideoFormat, ...]:
            calls.append(node_path)
            return _SAMPLE_FORMATS

        monkeypatch.setattr(record.engine, "probe_formats", _fake_probe)
        payload = _out(["record", "C270", "/tmp/out.mkv", "--probe", "--json"], capsys)
        assert calls == ["/dev/video0"]
        assert payload["video_format"]["probed"] is True
        assert payload["video_format"]["planned"] == {
            "pixel_format": "MJPG",
            "width": 1280,
            "height": 720,
            "fps": 30.0,
        }

    def test_probe_logs_exactly_one_activation(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(record.engine, "probe_formats", lambda node: _SAMPLE_FORMATS)
        _out(["record", "C270", "/tmp/out.mkv", "--probe", "--json"], capsys)
        log_file = tmp_path / "activations.jsonl"
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["verb"] == "record"
        assert entry["device_id"] == DEVICE.stable_id
        assert entry["detail"]["action"] == "probe"
        assert entry["ended_at"] is not None

    def test_probe_still_creates_no_output_file(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(record.engine, "probe_formats", lambda node: _SAMPLE_FORMATS)
        output = tmp_path / "clip.mkv"
        _out(["record", "C270", str(output), "--probe", "--json"], capsys)
        assert not output.exists()

    def test_probe_unsupported_request_is_a_typed_error_not_a_fallback(
        self, env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(record.engine, "probe_formats", lambda node: _SAMPLE_FORMATS)
        args = _parse(
            [
                "record",
                "C270",
                "/tmp/out.mkv",
                "--probe",
                "--pixel-format",
                "H264",
                "--width",
                "1920",
                "--height",
                "1080",
                "--fps",
                "60",
            ]
        )
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_USER_ERROR


# ===========================================================================
# Criterion 2 — apply writes exactly one artifact; --json is complete.
# ===========================================================================


class TestApply:
    def _popen_factory(self, output_path: Path, calls: list[list[str]]):
        def factory(argv, **kwargs):
            calls.append(argv)
            joined = " ".join(argv)
            if "filesink" in joined:
                output_path.write_bytes(b"fake-mkv-bytes")
            return _ImmediateProc(argv)

        return factory

    def _apply_env(self, monkeypatch: pytest.MonkeyPatch, output_path: Path) -> list[list[str]]:
        calls: list[list[str]] = []
        monkeypatch.setattr(record.subprocess, "Popen", self._popen_factory(output_path, calls))
        monkeypatch.setattr(record.engine, "probe_formats", lambda node: _SAMPLE_FORMATS)
        return calls

    def test_apply_writes_exactly_one_artifact(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        out_dir = tmp_path / "clips"
        out_dir.mkdir()
        output = out_dir / "clip.mkv"
        self._apply_env(monkeypatch, output)

        payload = _out(["record", "C270", str(output), "--apply", "--json"], capsys)

        assert payload["apply"] is True
        written = sorted(p.name for p in out_dir.iterdir())
        assert written == ["clip.mkv"]
        assert output.read_bytes() == b"fake-mkv-bytes"

    def test_apply_json_reports_negotiated_bound_path_timestamps(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.mkv"
        self._apply_env(monkeypatch, output)

        payload = _out(
            ["record", "C270", str(output), "--apply", "--duration", "12", "--json"], capsys
        )

        assert payload["video_format"]["negotiated"] == {
            "pixel_format": "MJPG",
            "width": 1280,
            "height": 720,
            "fps": 30.0,
        }
        assert payload["bound"] == {
            "duration_s": 12.0,
            "duration_is_default": False,
            "max_bytes": None,
        }
        assert payload["output_path"] == str(output)
        ts = payload["timestamps"]
        for key in ("started_at", "recording_started_at", "ended_at"):
            assert ts[key], key
        assert ts["warmup_started_at"] is not None  # video kind warms up by default

    def test_apply_reports_bytes_written(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.mkv"
        self._apply_env(monkeypatch, output)
        payload = _out(["record", "C270", str(output), "--apply", "--json"], capsys)
        assert payload["bytes_written"] == len(b"fake-mkv-bytes")

    def test_apply_warmup_default_is_a_frame_count_not_a_fixed_interval(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """The default warm-up must scale with the negotiated frame rate.

        Auto-exposure settle was measured on the reference C270 (task t9) at
        13-15 *frames* whether the camera ran at 30 fps or at 5 fps — the
        wall-clock interval grew five-fold while the frame count barely
        moved. A fixed-seconds default is therefore right at exactly one
        frame rate and under-warms at every lower one.
        """
        output = tmp_path / "clip.mkv"
        self._apply_env(monkeypatch, output)
        monkeypatch.setattr(
            record.engine,
            "probe_formats",
            lambda node: (engine.VideoFormat(pixel_format="MJPG", width=640, height=480, fps=5.0),),
        )

        payload = _out(["record", "C270", str(output), "--apply", "--json"], capsys)

        assert payload["warmup_frames"] == engine.DEFAULT_WARMUP_FRAMES
        assert payload["warmup_s"] == pytest.approx(engine.DEFAULT_WARMUP_FRAMES / 5.0)
        assert payload["warmup_s"] > record._DEFAULT_WARMUP_VIDEO_S
        assert "frames" in payload["warmup_basis"]

    def test_apply_warmup_at_30fps_matches_the_stream_verbs_constant(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.mkv"
        self._apply_env(monkeypatch, output)  # _SAMPLE_FORMATS negotiates 30 fps

        payload = _out(["record", "C270", str(output), "--apply", "--json"], capsys)

        assert payload["warmup_s"] == pytest.approx(
            engine.warmup_seconds(engine.DEFAULT_WARMUP_FRAMES, 30.0)
        )
        assert payload["warmup_frames"] == engine.DEFAULT_WARMUP_FRAMES

    def test_apply_explicit_warmup_still_wins(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.mkv"
        self._apply_env(monkeypatch, output)
        payload = _out(
            ["record", "C270", str(output), "--apply", "--warmup", "0.5", "--json"], capsys
        )
        assert payload["warmup_s"] == 0.5

    def test_apply_on_a_busy_camera_is_the_typed_busy_error(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """V4L2 busy never reaches open(2); it only shows up in engine output.

        Verified on hardware (task t9) against a genuinely held C270:
        ``require_access`` passed and the pipeline died with
        ``Device '/dev/video0' is busy``.
        """
        output = tmp_path / "clip.mkv"
        monkeypatch.setattr(record.engine, "probe_formats", lambda node: _SAMPLE_FORMATS)
        monkeypatch.setattr(
            record.access, "find_holder", lambda path: access.Holder(pid=77, command="gst-launch")
        )

        class _BusyProc:
            def __init__(self, argv: list[str], **kwargs: object) -> None:
                self.argv = argv
                self.returncode = 1

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                return "", "ERROR: Device '/dev/video0' is busy\n"

        monkeypatch.setattr(record.subprocess, "Popen", _BusyProc)

        args = _parse(["record", "C270", str(output), "--apply", "--warmup", "0"])
        with pytest.raises(CliError) as exc:
            args.func(args)

        assert exc.value.code == EXIT_ENV_ERROR
        assert "busy" in exc.value.message
        assert "/dev/video0" in exc.value.message
        assert "pid 77" in exc.value.message

    def test_apply_calls_require_engine_and_require_access(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.mkv"
        self._apply_env(monkeypatch, output)
        engine_calls = []
        monkeypatch.setattr(
            record.engine, "require_engine", lambda: (engine_calls.append(1), _capability())[1]
        )
        access_calls = []
        monkeypatch.setattr(
            record.access,
            "require_access",
            lambda path, kind: access_calls.append((path, kind)),
        )
        _out(["record", "C270", str(output), "--apply", "--json"], capsys)
        assert engine_calls == [1]
        assert access_calls == [("/dev/video0", "video")]

    def test_apply_av_checks_both_subsystems(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.mkv"
        self._apply_env(monkeypatch, output)
        access_calls = []
        monkeypatch.setattr(
            record.access,
            "require_access",
            lambda path, kind: access_calls.append((path, kind)),
        )
        _out(["record", "C270", str(output), "--kind", "av", "--apply", "--json"], capsys)
        kinds = {kind for _path, kind in access_calls}
        assert kinds == {"video", "audio"}

    def test_apply_uses_fakesink_for_warmup_then_filesink_for_record(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.mkv"
        calls = self._apply_env(monkeypatch, output)
        _out(["record", "C270", str(output), "--apply", "--warmup", "1.5", "--json"], capsys)
        assert len(calls) == 2
        assert "fakesink" in calls[0]
        assert any("filesink" in tok for tok in calls[1])

    def test_apply_warmup_zero_skips_the_warmup_phase(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.mkv"
        calls = self._apply_env(monkeypatch, output)
        payload = _out(
            ["record", "C270", str(output), "--apply", "--warmup", "0", "--json"], capsys
        )
        assert len(calls) == 1
        assert payload["timestamps"]["warmup_started_at"] is None

    def test_apply_pins_resolved_gst_launch_executable(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.mkv"
        calls = self._apply_env(monkeypatch, output)
        _out(["record", "C270", str(output), "--apply", "--json"], capsys)
        assert all(argv[0] == "/usr/bin/gst-launch-1.0" for argv in calls)

    def test_apply_missing_artifact_is_a_typed_env_error(
        self, env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        output = tmp_path / "clip.mkv"

        def factory(argv, **kwargs):
            return _ImmediateProc(argv)  # never writes the file

        monkeypatch.setattr(record.subprocess, "Popen", factory)
        monkeypatch.setattr(record.engine, "probe_formats", lambda node: _SAMPLE_FORMATS)
        args = _parse(["record", "C270", str(output), "--apply"])
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_ENV_ERROR
        assert not output.exists()

    def test_apply_empty_artifact_is_a_typed_env_error(
        self, env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        output = tmp_path / "clip.mkv"

        def factory(argv, **kwargs):
            joined = " ".join(argv)
            if "filesink" in joined:
                output.write_bytes(b"")
            return _ImmediateProc(argv)

        monkeypatch.setattr(record.subprocess, "Popen", factory)
        monkeypatch.setattr(record.engine, "probe_formats", lambda node: _SAMPLE_FORMATS)
        args = _parse(["record", "C270", str(output), "--apply"])
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_ENV_ERROR

    def test_apply_logs_exactly_one_activation_covering_the_whole_session(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.mkv"
        self._apply_env(monkeypatch, output)
        _out(["record", "C270", str(output), "--apply", "--json"], capsys)
        log_file = tmp_path / "activations.jsonl"
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["verb"] == "record"
        assert entry["target"] == str(output)
        assert entry["device_id"] == DEVICE.stable_id
        assert entry["ended_at"] is not None
        assert entry["detail"]["stopped_reason"] == "completed"
        assert entry["detail"]["bytes_written"] == len(b"fake-mkv-bytes")

    def test_apply_engine_missing_fails_before_any_subprocess(
        self, env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        output = tmp_path / "clip.mkv"

        def _raise_env_error():
            raise CliError(
                EXIT_ENV_ERROR, "gst-launch-1.0 is not installed", remediation="install it"
            )

        monkeypatch.setattr(record.engine, "require_engine", _raise_env_error)
        monkeypatch.setattr(record.subprocess, "Popen", _RaisingPopen())
        args = _parse(["record", "C270", str(output), "--apply"])
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_ENV_ERROR

    def test_apply_busy_device_fails_before_any_subprocess(
        self, env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        output = tmp_path / "clip.mkv"

        def _raise_busy(path, kind):
            raise CliError(EXIT_ENV_ERROR, f"{kind} device {path} is busy", remediation="wait")

        monkeypatch.setattr(record.access, "require_access", _raise_busy)
        monkeypatch.setattr(record.subprocess, "Popen", _RaisingPopen())
        args = _parse(["record", "C270", str(output), "--apply"])
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_ENV_ERROR

    def test_apply_records_a_crashed_activation_on_failure(
        self, env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        output = tmp_path / "clip.mkv"

        def _raise_busy(path, kind):
            raise CliError(EXIT_ENV_ERROR, "busy", remediation="wait")

        monkeypatch.setattr(record.access, "require_access", _raise_busy)
        monkeypatch.setattr(record.subprocess, "Popen", _RaisingPopen())
        args = _parse(["record", "C270", str(output), "--apply"])
        with pytest.raises(CliError):
            args.func(args)
        log_file = tmp_path / "activations.jsonl"
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["ended_at"] is not None
        assert "error" in entry["detail"]

    def test_apply_ignores_probe_flag_even_for_audio_kind(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.wav"

        def factory(argv, **kwargs):
            joined = " ".join(argv)
            if "filesink" in joined:
                output.write_bytes(b"audio")
            return _ImmediateProc(argv)

        monkeypatch.setattr(record.subprocess, "Popen", factory)
        payload = _out(
            ["record", "C270", str(output), "--kind", "audio", "--probe", "--apply", "--json"],
            capsys,
        )
        assert payload["apply"] is True

    def test_apply_unsupported_negotiation_is_typed_error_not_silent_fallback(
        self, env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        output = tmp_path / "clip.mkv"
        monkeypatch.setattr(record.engine, "probe_formats", lambda node: _SAMPLE_FORMATS)
        monkeypatch.setattr(record.subprocess, "Popen", _RaisingPopen())
        args = _parse(
            [
                "record",
                "C270",
                str(output),
                "--apply",
                "--pixel-format",
                "H264",
                "--width",
                "1920",
                "--height",
                "1080",
                "--fps",
                "60",
            ]
        )
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_USER_ERROR

    def test_apply_activation_log_oserror_is_wrapped(
        self, env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        output = tmp_path / "clip.mkv"
        self._apply_env(monkeypatch, output)

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(record.activation, "record_activation", _boom)
        args = _parse(["record", "C270", str(output), "--apply"])
        with pytest.raises(CliError) as exc:
            args.func(args)
        assert exc.value.code == EXIT_ENV_ERROR
        assert "activation log" in exc.value.message

    def test_apply_text_mode_smoke(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.mkv"
        self._apply_env(monkeypatch, output)
        args = _parse(["record", "C270", str(output), "--apply"])
        rc = args.func(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "record: apply" in out
        assert "bytes_written:" in out

    def test_apply_audio_only_kind_uses_alsasrc_and_default_format(
        self,
        env,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "clip.wav"
        calls: list[list[str]] = []

        def factory(argv, **kwargs):
            calls.append(argv)
            if any("filesink" in tok for tok in argv):
                output.write_bytes(b"pcm")
            return _ImmediateProc(argv)

        monkeypatch.setattr(record.subprocess, "Popen", factory)
        payload = _out(
            ["record", "C270", str(output), "--kind", "audio", "--apply", "--json"], capsys
        )
        assert payload["audio_format"]["negotiated"] == {
            "rate": record._DEFAULT_AUDIO_RATE,
            "channels": record._DEFAULT_AUDIO_CHANNELS,
        }
        assert len(calls) == 1  # no warm-up phase for audio-only
        assert any("alsasrc" in tok for tok in calls[0])


# ===========================================================================
# small helpers
# ===========================================================================


def test_audio_node_path_reconstruction() -> None:
    card = AudioCard(index=2, card_id="WEBCAM", name="C270", alsa_address="hw:CARD=WEBCAM,DEV=3")
    assert record._audio_node_path(card) == "/dev/snd/pcmC2D3c"


def test_audio_node_path_defaults_dev_zero_when_unparseable() -> None:
    card = AudioCard(index=1, card_id="X", name="X", alsa_address="hw:CARD=X")
    assert record._audio_node_path(card) == "/dev/snd/pcmC1D0c"


class TestTypeFunctionsRejectNonNumeric:
    @pytest.mark.parametrize(
        ("type_fn", "raw"),
        [
            (record._duration_type, "not-a-number"),
            (record._max_bytes_type, "not-a-number"),
            (record._warmup_type, "not-a-number"),
            (record._positive_int_type, "not-a-number"),
            (record._positive_float_type, "not-a-number"),
        ],
    )
    def test_rejects_non_numeric_input(self, type_fn, raw: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            type_fn(raw)

    def test_warmup_over_ceiling_rejected(self) -> None:
        raw = str(record._MAX_WARMUP_S + 1)
        with pytest.raises(argparse.ArgumentTypeError):
            record._warmup_type(raw)

    def test_warmup_negative_rejected(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            record._warmup_type("-1")

    def test_positive_int_rejects_zero(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            record._positive_int_type("0")

    def test_positive_float_rejects_infinite(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            record._positive_float_type("inf")

    def test_pixel_format_rejects_blank(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            record._pixel_format_type("   ")

    def test_pixel_format_normalises_case(self) -> None:
        assert record._pixel_format_type(" mjpg ") == "MJPG"


class TestSmallHelpers:
    def test_current_size_of_missing_path_is_zero(self, tmp_path: Path) -> None:
        assert record._current_size(str(tmp_path / "does-not-exist")) == 0

    def test_current_size_of_real_file(self, tmp_path: Path) -> None:
        target = tmp_path / "f.bin"
        target.write_bytes(b"12345")
        assert record._current_size(str(target)) == 5

    def test_pin_executable_passes_through_when_gst_launch_unknown(self) -> None:
        argv = ["gst-launch-1.0", "v4l2src"]
        assert record._pin_executable(argv, None) == argv

    def test_pin_executable_substitutes_resolved_path(self) -> None:
        argv = ["gst-launch-1.0", "v4l2src"]
        assert record._pin_executable(argv, "/usr/bin/gst-launch-1.0") == [
            "/usr/bin/gst-launch-1.0",
            "v4l2src",
        ]

    def test_require_artifact_missing_file_includes_stderr_detail(self) -> None:
        result = record._PhaseResult(
            returncode=1, stopped_reason="completed", stdout="", stderr="no such device"
        )
        with pytest.raises(CliError) as exc:
            record._require_artifact("/no/such/path.mkv", result)
        assert "no such device" in exc.value.message


def test_dry_run_av_full_request_builds_full_pipeline_preview(
    env, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _out(
        [
            "record",
            "C270",
            "/tmp/out.mkv",
            "--kind",
            "av",
            "--pixel-format",
            "mjpg",
            "--width",
            "1280",
            "--height",
            "720",
            "--fps",
            "30",
            "--rate",
            "48000",
            "--channels",
            "1",
            "--json",
        ],
        capsys,
    )
    preview = payload["pipeline_preview"]
    assert preview is not None
    assert "v4l2src" in preview
    assert "alsasrc" in preview
    assert "matroskamux" in preview
