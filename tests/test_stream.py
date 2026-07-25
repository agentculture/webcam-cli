"""Tests for ``webcam stream`` (webcam_cli.cli._commands.stream) — task t6.

**Hardware-free by construction (approved deviation d1).** No test here opens,
probes, streams from, or otherwise energizes a camera or microphone. Every
boundary that could reach hardware — ``engine.detect``/``require_engine``,
``engine.probe_formats``, ``access.check_access``/``require_access``, and the
subprocess spawn — is monkeypatched, and the ``_no_hardware`` autouse fixture
arms the ones a given test does not explicitly stub with a boobytrap that
fails the test if touched. The live on-hardware proof belongs to task t9.

The four acceptance criteria are encoded here:

1. dry-run (the default) resolves the device, checks the request, prints the
   attachment plan, and touches nothing — with ``--probe`` opting into real
   enumeration and being logged as an activation (deviation d2);
2. with ``--apply`` a second process attaches using *only* the ``--json``
   payload — proven here at the transport level against a stand-in server
   bound to the announced port, since the pipeline child is mocked;
3. negotiated-versus-requested is reported and an unsupported combination is a
   typed user error, never a silent fallback;
4. warm-up discard is applied at stream start, with a documented, overridable
   default.

These tests build a parser locally rather than going through
``webcam_cli.cli.main``, keeping the verb's behaviour separable from parser
assembly. Registration in ``_build_parser`` landed with task t8 and is asserted
in ``tests/test_cli.py``, including that this noun's parse errors keep the
structured exit-1 contract.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from webcam_cli import access, activation, devices, engine
from webcam_cli.cli import _CliArgumentParser
from webcam_cli.cli._commands import stream
from webcam_cli.cli._errors import EXIT_BUSY_ERROR, EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

# --- fixtures / fakes -------------------------------------------------------

_STABLE_ID = "usb-046d_C270_HD_WEBCAM_200901010001"

# The four leading bytes of any Matroska/WebM file: the EBML magic. The
# stand-in server below serves them so a "consumer" can prove it attached to a
# real, correctly-announced endpoint without any codec being involved.
_EBML_MAGIC = b"\x1aE\xdf\xa3"

# Captured at import, before the _no_hardware fixture arms its boobytrap, so the
# process-plumbing tests at the bottom of this file can drive the real seam.
_REAL_SPAWN = stream._spawn


def _c270(*, with_audio: bool = True, with_camera: bool = True) -> devices.LogicalDevice:
    """The reference host's Logitech C270 as ``devices`` reports it."""
    nodes = (
        devices.VideoNode(
            path="/dev/video0",
            by_id=f"/dev/v4l/by-id/{_STABLE_ID}-video-index0",
            index=0,
        ),
        devices.VideoNode(
            path="/dev/video1",
            by_id=f"/dev/v4l/by-id/{_STABLE_ID}-video-index1",
            index=1,
        ),
    )
    card = devices.AudioCard(
        index=1,
        card_id="C270",
        name="C270 HD WEBCAM",
        alsa_address="hw:CARD=C270,DEV=0",
    )
    return devices.LogicalDevice(
        stable_id=_STABLE_ID,
        label="C270 HD WEBCAM",
        usb_path="3-1",
        video_nodes=nodes if with_camera else (),
        capture_node="/dev/video0" if with_camera else None,
        audio=card if with_audio else None,
    )


_FORMATS = (
    engine.VideoFormat(pixel_format="MJPG", width=1280, height=720, fps=30.0),
    engine.VideoFormat(pixel_format="MJPG", width=640, height=480, fps=30.0),
    engine.VideoFormat(pixel_format="YUYV", width=640, height=480, fps=30.0),
)


def _cap(**overrides: bool) -> engine.Capability:
    plugins = {
        "v4l2src": True,
        "alsasrc": True,
        "matroskamux": True,
        "tcpserversink": True,
        "jpegenc": True,
        "vp8enc": False,
        "opusenc": True,
        "x264enc": False,
    }
    plugins.update(overrides)
    return engine.Capability(
        gst_launch="/usr/bin/gst-launch-1.0",
        gst_inspect="/usr/bin/gst-inspect-1.0",
        plugins=plugins,
        available=True,
    )


class _FakeServer:
    """Stands in for the ``gst-launch-1.0 ... tcpserversink`` child process.

    Binds the port webcam-cli announced and serves one client a Matroska
    magic-number prefix. This is the transport half of criterion 2: it proves
    the announced attachment point is real and complete, without a camera.
    """

    def __init__(self, port: int, payload: bytes = _EBML_MAGIC) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(1)
        self._payload = payload
        self.served = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
            with conn:
                conn.sendall(self._payload)
        except OSError:  # pragma: no cover - only on a torn-down test socket
            pass
        finally:
            self.served.set()
            self._sock.close()


@dataclass
class _FakeProc:
    """Duck-typed stand-in for :class:`stream._StreamProcess`."""

    argv: list[str] = field(default_factory=list)
    server: _FakeServer | None = None
    pid: int = 4242
    returncode: int | None = None
    tail: str = ""
    terminated: bool = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int:
        if self.server is not None:
            self.server.served.wait(timeout=5)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0

    def output_tail(self) -> str:
        return self.tail


def _port_from_argv(argv: list[str]) -> int:
    for token in argv:
        if token.startswith("port="):
            return int(token.split("=", 1)[1])
    raise AssertionError(f"no port= token in argv: {argv}")


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Arm every hardware boundary; a test that needs one stubs it explicitly."""

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("this code path touched hardware")

    monkeypatch.setattr(engine, "detect", _boom)
    monkeypatch.setattr(engine, "require_engine", _boom)
    monkeypatch.setattr(engine, "probe_formats", _boom)
    monkeypatch.setattr(access, "check_access", _boom)
    monkeypatch.setattr(access, "require_access", _boom)
    monkeypatch.setattr(stream, "_spawn", _boom)
    monkeypatch.setattr(devices, "resolve", lambda selector, **kw: _c270())
    monkeypatch.setattr(stream, "_sleep", lambda seconds: None)
    monkeypatch.setenv(activation.ENV_LOG_PATH, str(tmp_path / "activations.jsonl"))


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Capture what the handler emits instead of writing it to stdout."""
    calls: list[object] = []

    def _fake(data: object, *, json_mode: bool, stream: object = None) -> None:
        calls.append(data)

    monkeypatch.setattr(stream, "emit_result", _fake)
    return calls


def _parser() -> argparse.ArgumentParser:
    parser = _CliArgumentParser(prog="webcam")
    sub = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)
    stream.register(sub)
    return parser


def _run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    rc = args.func(args)
    return 0 if rc is None else int(rc)


def _log_lines(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "activations.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _grant_engine(monkeypatch: pytest.MonkeyPatch, **plugins: bool) -> engine.Capability:
    cap = _cap(**plugins)
    monkeypatch.setattr(engine, "require_engine", lambda: cap)
    return cap


def _grant_probe(monkeypatch: pytest.MonkeyPatch, formats: tuple = _FORMATS) -> list[str]:
    probed: list[str] = []

    def _probe(node_path: str) -> tuple:
        probed.append(node_path)
        return formats

    monkeypatch.setattr(engine, "probe_formats", _probe)
    return probed


def _grant_access(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    checked: list[tuple[str, str]] = []

    def _check(path: str, kind: str) -> access.AccessReport:
        checked.append((path, kind))
        return access.AccessReport(
            path=path, kind=kind, state=access.AccessState.OK, remediation=""
        )

    def _require(path: str, kind: str) -> None:
        checked.append((path, kind))

    monkeypatch.setattr(access, "check_access", _check)
    monkeypatch.setattr(access, "require_access", _require)
    return checked


def _grant_spawn(monkeypatch: pytest.MonkeyPatch, *, serve: bool = False) -> list[_FakeProc]:
    procs: list[_FakeProc] = []

    def _spawn(argv: list[str]) -> _FakeProc:
        server = _FakeServer(_port_from_argv(argv)) if serve else None
        proc = _FakeProc(argv=list(argv), server=server)
        procs.append(proc)
        return proc

    monkeypatch.setattr(stream, "_spawn", _spawn)
    return procs


def _apply_ready(monkeypatch: pytest.MonkeyPatch, **plugins: bool) -> list[_FakeProc]:
    _grant_engine(monkeypatch, **plugins)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)
    return _grant_spawn(monkeypatch)


# --- surface ----------------------------------------------------------------


def test_register_exposes_the_three_media_verbs_and_overview() -> None:
    parser = _parser()
    for verb in ("video", "audio", "av", "overview"):
        assert parser.parse_args(
            ["stream", verb, "c270"] if verb != "overview" else ["stream", verb]
        )


def test_stream_without_a_subverb_prints_the_noun_overview(emitted: list[object]) -> None:
    assert _run(["stream", "--json"]) == 0
    payload = emitted[-1]
    assert isinstance(payload, dict)
    assert payload["subject"] == "webcam stream"


def test_stream_overview_documents_the_hardware_split(emitted: list[object]) -> None:
    assert _run(["stream", "overview", "--json"]) == 0
    body = json.dumps(emitted[-1]).lower()
    assert "--probe" in body and "--apply" in body
    assert "warm-up" in body or "warmup" in body


def test_unknown_subverb_exits_1_not_2(capsys: pytest.CaptureFixture[str]) -> None:
    """parser_class=type(p) must propagate, or argparse exits 2 and bypasses hint:."""
    parser = _parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["stream", "bogus"])
    assert exc.value.code == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err


def test_streams_are_unbounded_no_duration_flag_exists(emitted: list[object]) -> None:
    parser = _parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["stream", "video", "c270", "--duration", "5"])
    assert _run(["stream", "video", "c270", "--json"]) == 0
    payload = emitted[-1]
    assert payload["bounded"] is False
    assert "unbounded" in payload["lifetime"].lower()


# --- criterion 1: dry-run is the default and touches nothing ----------------


def test_dry_run_is_the_default_and_touches_no_hardware(emitted: list[object]) -> None:
    # Every hardware boundary is a boobytrap (see _no_hardware); reaching one
    # fails the test. Success here *is* the proof.
    assert _run(["stream", "video", "c270", "--json"]) == 0
    payload = emitted[-1]
    assert payload["mode"] == "dry-run"
    assert payload["applied"] is False
    assert payload["probed"] is False
    assert payload["hardware_touched"] is False
    assert payload["device"]["stable_id"] == _STABLE_ID
    assert payload["source"]["video_node"] == "/dev/video0"


def test_dry_run_prints_a_concrete_plan_for_a_complete_request(emitted: list[object]) -> None:
    rc = _run(
        [
            "stream",
            "video",
            "c270",
            "--format",
            "MJPG",
            "--width",
            "1280",
            "--height",
            "720",
            "--fps",
            "30",
            "--json",
        ]
    )
    assert rc == 0
    payload = emitted[-1]
    assert payload["negotiation"]["status"] == "unvalidated"
    assert payload["request"]["video"] == {
        "pixel_format": "MJPG",
        "width": 1280,
        "height": 720,
        "fps": 30.0,
    }
    argv = payload["pipeline"]
    assert argv[0] == "gst-launch-1.0"
    assert "device=/dev/video0" in argv
    assert any("tcpserversink" in token for token in argv)
    assert payload["pipeline_str"].startswith("gst-launch-1.0 ")


def test_dry_run_defers_the_pipeline_when_the_request_is_partial(emitted: list[object]) -> None:
    assert _run(["stream", "video", "c270", "--width", "1280", "--json"]) == 0
    payload = emitted[-1]
    assert payload["negotiation"]["status"] == "deferred"
    assert payload["pipeline"] is None
    assert "--probe" in payload["negotiation"]["validated_against"]
    assert payload["request"]["video"]["width"] == 1280
    assert payload["request"]["video"]["pixel_format"] is None


def test_dry_run_audio_plan_uses_the_alsa_address(emitted: list[object]) -> None:
    assert _run(["stream", "audio", "c270", "--json"]) == 0
    payload = emitted[-1]
    assert payload["source"]["alsa_address"] == "hw:CARD=C270,DEV=0"
    assert payload["request"]["audio"] == {"rate": 48000, "channels": 1}
    assert "device=hw:CARD=C270,DEV=0" in payload["pipeline"]


def test_dry_run_av_plan_carries_both_sources(emitted: list[object]) -> None:
    rc = _run(
        ["stream", "av", "c270", "--format", "MJPG", "--width", "640"]
        + ["--height", "480", "--fps", "30", "--json"]
    )
    assert rc == 0
    argv = emitted[-1]["pipeline"]
    assert "device=/dev/video0" in argv
    assert "device=hw:CARD=C270,DEV=0" in argv
    assert "matroskamux" in argv


def test_dry_run_writes_no_activation_line(tmp_path: Path, emitted: list[object]) -> None:
    assert _run(["stream", "video", "c270", "--json"]) == 0
    assert _log_lines(tmp_path) == []
    assert emitted[-1]["consent"]["logged"] is False


def test_dry_run_rejects_a_structurally_impossible_request() -> None:
    with pytest.raises(CliError) as exc:
        _run(["stream", "video", "c270", "--width", "0", "--height", "480"])
    assert exc.value.code == EXIT_USER_ERROR
    assert exc.value.remediation


def test_dry_run_payload_is_json_serialisable(emitted: list[object]) -> None:
    assert _run(["stream", "av", "c270", "--json"]) == 0
    json.loads(json.dumps(emitted[-1]))


def test_text_mode_writes_only_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(["stream", "video", "c270"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "attachment plan" in captured.out.lower()
    assert "tcp://127.0.0.1:" in captured.out


# --- criterion 1b: --probe opts into real enumeration (deviation d2) --------


def test_probe_validates_against_the_enumerated_set(
    monkeypatch: pytest.MonkeyPatch, emitted: list[object]
) -> None:
    _grant_engine(monkeypatch)
    probed = _grant_probe(monkeypatch)
    _grant_access(monkeypatch)

    assert _run(["stream", "video", "c270", "--probe", "--json"]) == 0
    payload = emitted[-1]
    assert probed == ["/dev/video0"]
    assert payload["probed"] is True
    assert payload["applied"] is False
    assert payload["hardware_touched"] is True
    assert payload["negotiation"]["status"] == "granted"
    assert len(payload["negotiation"]["available"]) == len(_FORMATS)
    assert payload["access"]["video"] == "ok"


def test_probe_writes_an_activation_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, emitted: list[object]
) -> None:
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)

    assert _run(["stream", "video", "c270", "--probe", "--json"]) == 0
    lines = _log_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["device_id"] == _STABLE_ID
    assert lines[0]["verb"] == "stream video"
    assert lines[0]["detail"]["mode"] == "probe"
    assert lines[0]["target"].startswith("probe://")
    assert lines[0]["ended_at"]
    assert emitted[-1]["consent"]["logged"] is True


def test_probe_does_not_spawn_a_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)
    # stream._spawn is still the boobytrap: reaching it fails the test.
    assert _run(["stream", "video", "c270", "--probe", "--json"]) == 0


# --- criterion 3: negotiated vs requested, never a silent fallback ----------


def test_unsupported_exact_request_is_a_typed_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)

    with pytest.raises(CliError) as exc:
        _run(
            ["stream", "video", "c270", "--probe", "--format", "MJPG"]
            + ["--width", "1920", "--height", "1080", "--fps", "60"]
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert "1920x1080" in exc.value.message
    assert "1280x720" in exc.value.remediation  # names the enumerated alternatives


def test_constrained_request_matching_nothing_is_a_typed_user_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)

    with pytest.raises(CliError) as exc:
        _run(["stream", "video", "c270", "--probe", "--format", "H264"])
    assert exc.value.code == EXIT_USER_ERROR
    assert "MJPG" in exc.value.remediation


def test_granted_exact_request_reports_requested_equals_negotiated(
    monkeypatch: pytest.MonkeyPatch, emitted: list[object]
) -> None:
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)

    rc = _run(
        ["stream", "video", "c270", "--probe", "--format", "YUYV"]
        + ["--width", "640", "--height", "480", "--fps", "30", "--json"]
    )
    assert rc == 0
    negotiation = emitted[-1]["negotiation"]
    assert negotiation["requested"] == negotiation["negotiated"]
    assert negotiation["exact_match"] is True
    assert negotiation["silent_fallback"] is False


def test_partial_request_reports_constraints_and_what_was_granted(
    monkeypatch: pytest.MonkeyPatch, emitted: list[object]
) -> None:
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)

    assert _run(["stream", "video", "c270", "--probe", "--format", "YUYV", "--json"]) == 0
    negotiation = emitted[-1]["negotiation"]
    assert negotiation["requested"] == {
        "pixel_format": "YUYV",
        "width": None,
        "height": None,
        "fps": None,
    }
    assert negotiation["negotiated"] == {
        "pixel_format": "YUYV",
        "width": 640,
        "height": 480,
        "fps": 30.0,
    }
    assert negotiation["exact_match"] is False
    assert negotiation["constrained_selection"] is True


def test_apply_refuses_an_unsupported_request_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)
    procs = _grant_spawn(monkeypatch)

    with pytest.raises(CliError) as exc:
        _run(
            ["stream", "video", "c270", "--apply", "--format", "MJPG"]
            + ["--width", "1920", "--height", "1080", "--fps", "30"]
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert procs == []


# --- criterion 2: --apply exposes an attachment point a consumer can use ----


def test_apply_payload_announces_everything_a_consumer_needs(
    monkeypatch: pytest.MonkeyPatch, emitted: list[object]
) -> None:
    _apply_ready(monkeypatch)
    assert _run(["stream", "video", "c270", "--apply", "--port", "0", "--json"]) == 0

    payload = emitted[-1]
    attach = payload["attach"]
    assert payload["mode"] == "apply" and payload["applied"] is True
    assert attach["mechanism"] == "gstreamer-tcpserversink"
    assert attach["transport"] == "tcp"
    assert attach["host"] == "127.0.0.1"
    assert isinstance(attach["port"], int) and attach["port"] > 0
    assert attach["uri"] == f"tcp://127.0.0.1:{attach['port']}"
    assert attach["container"] == "matroska"
    assert attach["streams"]["video"]["caps"].startswith("image/jpeg")
    assert attach["consumer"]["gst_launch"][0] == "gst-launch-1.0"
    assert "tcpclientsrc" in attach["consumer"]["gst_launch_str"]
    assert attach["exposure"]
    assert payload["started_at"]
    assert payload["pid"] == 4242


def test_a_second_process_attaches_using_only_the_json_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 2, hardware-free: the announced endpoint is real and complete.

    ``emit_result`` is replaced by a "consumer" that has seen nothing but the
    payload dict: it reads ``attach.uri``, connects, and reads bytes. The
    pipeline child is a stand-in TCP server (deviation d1) — proving the
    announcement, not the codec. The live decode belongs to t9.
    """
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)
    _grant_spawn(monkeypatch, serve=True)

    received: list[bytes] = []
    payloads: list[dict] = []

    def _consumer(data: object, *, json_mode: bool, stream: object = None) -> None:
        assert isinstance(data, dict)
        payloads.append(data)
        uri = data["attach"]["uri"]  # the ONLY thing the consumer is given
        assert uri.startswith("tcp://")
        host, _, port = uri[len("tcp://") :].partition(":")
        with socket.create_connection((host, int(port)), timeout=5) as conn:
            received.append(conn.recv(64))

    monkeypatch.setattr(stream, "emit_result", _consumer)

    assert _run(["stream", "video", "c270", "--apply", "--port", "0", "--json"]) == 0
    assert received and received[0].startswith(_EBML_MAGIC)
    assert payloads[0]["attach"]["container"] == "matroska"


def test_apply_av_pipeline_carries_both_node_and_alsa_address(
    monkeypatch: pytest.MonkeyPatch, emitted: list[object]
) -> None:
    procs = _apply_ready(monkeypatch)
    assert _run(["stream", "av", "c270", "--apply", "--port", "0", "--json"]) == 0

    argv = procs[0].argv
    assert "device=/dev/video0" in argv
    assert "device=hw:CARD=C270,DEV=0" in argv
    assert "matroskamux" in argv
    assert emitted[-1]["attach"]["streams"]["audio"]["caps"].startswith("audio/x-raw")


def test_av_consumer_gives_every_demuxer_branch_its_own_queue(
    monkeypatch: pytest.MonkeyPatch, emitted: list[object]
) -> None:
    """Without a queue per branch, no consumer can decode an av stream at all.

    Measured on hardware (task t9): the announced av consumer *without* these
    queues delivered 0 video buffers and 1 audio buffer — both from a live
    stream and from a known-good recorded file — because a demuxer fanning
    out to two branches runs them from a single streaming thread and stalls.
    The identical command with a queue on each branch delivered 40 video and
    268 audio buffers from that same file.
    """
    _apply_ready(monkeypatch)
    assert _run(["stream", "av", "c270", "--apply", "--port", "0", "--json"]) == 0

    command = emitted[-1]["attach"]["consumer"]["gst_launch_str"]
    branches = command.split("demux. ! ")[1:]
    assert len(branches) == 2, command
    assert all(branch.startswith("queue ! ") for branch in branches), command


def test_single_medium_consumers_need_no_queue(
    monkeypatch: pytest.MonkeyPatch, emitted: list[object]
) -> None:
    """One branch off the demuxer cannot stall, and is left plain on purpose."""
    _apply_ready(monkeypatch)
    assert _run(["stream", "video", "c270", "--apply", "--port", "0", "--json"]) == 0
    assert "demux." not in emitted[-1]["attach"]["consumer"]["gst_launch_str"]


def test_apply_activation_record_carries_the_negotiated_format_and_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Late-resolved facts must reach the log, not a dict nobody reads.

    ``activation_scope`` copies the detail mapping it is handed, so writing to
    the caller's own dict after entering the scope is silently lost. On
    hardware (task t9) that meant every live stream's consent record was
    missing the format it captured, the warm-up it applied, and the pid of the
    process holding the camera.
    """
    _apply_ready(monkeypatch)
    assert _run(["stream", "video", "c270", "--apply", "--port", "0", "--json"]) == 0

    detail = _log_lines(tmp_path)[0]["detail"]
    assert detail["negotiated"] == {
        "pixel_format": "MJPG",
        "width": 1280,
        "height": 720,
        "fps": 30.0,
    }
    assert detail["pid"] == 4242
    assert detail["warmup_ms"] == 1000.0


def test_a_busy_v4l2_device_becomes_the_typed_busy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V4L2 exclusivity is invisible to open(2); the engine output is the tell.

    uvcvideo permits several opens of a ``/dev/video*`` node and only refuses
    at ``S_FMT``, so the access gate passes and the pipeline dies instead.
    Verified on hardware (task t9) against a genuinely held C270.
    """
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)
    monkeypatch.setattr(
        access, "find_holder", lambda path: access.Holder(pid=99, command="gst-launch-1.0")
    )

    tail = (
        "ERROR: from element /GstPipeline:pipeline0/GstV4l2Src:v4l2src0: "
        "Device '/dev/video0' is busy\n"
        "Call to S_FMT failed for MJPG @ 1280x960: Device or resource busy"
    )
    monkeypatch.setattr(
        stream, "_spawn", lambda argv: _FakeProc(argv=list(argv), returncode=1, tail=tail)
    )

    with pytest.raises(CliError) as exc:
        _run(["stream", "video", "c270", "--apply", "--port", "0"])

    # BUSY is the retryable class, distinct from the generic env error.
    assert exc.value.code == EXIT_BUSY_ERROR
    assert "busy" in exc.value.message
    assert "/dev/video0" in exc.value.message
    assert "pid 99" in exc.value.message
    assert "gst-launch-1.0" in exc.value.remediation


def test_apply_writes_only_the_json_payload_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _apply_ready(monkeypatch)
    assert _run(["stream", "video", "c270", "--apply", "--port", "0", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.strip().splitlines()) == 1
    json.loads(captured.out)  # the media stream never shares stdout with it


def test_apply_logs_one_activation_line_naming_the_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, emitted: list[object]
) -> None:
    _apply_ready(monkeypatch)
    assert _run(["stream", "audio", "c270", "--apply", "--port", "0", "--json"]) == 0

    lines = _log_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["verb"] == "stream audio"
    assert lines[0]["device_id"] == _STABLE_ID
    assert lines[0]["target"] == emitted[-1]["attach"]["uri"]
    assert lines[0]["detail"]["mode"] == "apply"
    assert lines[0]["started_at"] and lines[0]["ended_at"]


def test_apply_requires_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing() -> engine.Capability:
        raise CliError(EXIT_ENV_ERROR, "gst-launch-1.0 is not installed", "install GStreamer")

    monkeypatch.setattr(engine, "require_engine", _missing)
    with pytest.raises(CliError) as exc:
        _run(["stream", "video", "c270", "--apply"])
    assert exc.value.code == EXIT_ENV_ERROR


def test_apply_requires_the_attachment_sink_element(monkeypatch: pytest.MonkeyPatch) -> None:
    _grant_engine(monkeypatch, tcpserversink=False)
    with pytest.raises(CliError) as exc:
        _run(["stream", "video", "c270", "--apply"])
    assert exc.value.code == EXIT_ENV_ERROR
    assert "tcpserversink" in exc.value.message
    assert exc.value.remediation


def test_apply_propagates_the_typed_busy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    procs = _grant_spawn(monkeypatch)

    busy = CliError(
        EXIT_BUSY_ERROR,
        "video device /dev/video0 is busy (held by gst-launch-1.0, pid 99)",
        "stop that process, then retry",
    )

    def _require(path: str, kind: str) -> None:
        raise busy

    monkeypatch.setattr(access, "require_access", _require)

    with pytest.raises(CliError) as exc:
        _run(["stream", "video", "c270", "--apply", "--port", "0"])
    assert exc.value.code == EXIT_BUSY_ERROR
    assert "pid 99" in exc.value.message
    assert procs == []


def test_a_pipeline_that_dies_during_warmup_is_a_typed_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)

    def _spawn(argv: list[str]) -> _FakeProc:
        return _FakeProc(argv=list(argv), returncode=1, tail="ERROR: from element v4l2src")

    monkeypatch.setattr(stream, "_spawn", _spawn)

    with pytest.raises(CliError) as exc:
        _run(["stream", "video", "c270", "--apply", "--port", "0"])
    assert exc.value.code == EXIT_ENV_ERROR
    assert "v4l2src" in exc.value.message
    # The activation is still recorded — a stream that dies must not vanish.
    lines = _log_lines(tmp_path)
    assert len(lines) == 1
    assert "error" in lines[0]["detail"]


def test_an_unwritable_activation_log_becomes_a_typed_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _apply_ready(monkeypatch)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    monkeypatch.setenv(activation.ENV_LOG_PATH, str(blocker / "nested" / "activations.jsonl"))

    with pytest.raises(CliError) as exc:
        _run(["stream", "video", "c270", "--apply", "--port", "0"])
    assert exc.value.code == EXIT_ENV_ERROR
    assert activation.ENV_LOG_PATH in exc.value.remediation


def test_a_port_already_in_use_is_a_typed_error_naming_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_ready(monkeypatch)
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port_arg = str(holder.getsockname()[1])
    try:
        with pytest.raises(CliError) as exc:
            _run(["stream", "video", "c270", "--apply", "--port", port_arg])
        assert exc.value.code == EXIT_ENV_ERROR
        assert "--port" in exc.value.remediation
    finally:
        holder.close()


# --- criterion 4: warm-up ---------------------------------------------------


def test_warmup_runs_before_the_attachment_point_is_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _grant_engine(monkeypatch)
    _grant_probe(monkeypatch)
    _grant_access(monkeypatch)
    events: list[str] = []

    def _spawn(argv: list[str]) -> _FakeProc:
        events.append("spawn")
        return _FakeProc(argv=list(argv))

    def _sleep(seconds: float) -> None:
        events.append(f"warmup:{seconds}")

    def _emit(data: object, *, json_mode: bool, stream: object = None) -> None:
        events.append("announce")

    monkeypatch.setattr(stream, "_spawn", _spawn)
    monkeypatch.setattr(stream, "_sleep", _sleep)
    monkeypatch.setattr(stream, "emit_result", _emit)

    assert _run(["stream", "video", "c270", "--apply", "--port", "0", "--json"]) == 0
    assert events == ["spawn", "warmup:1.0", "announce"]


def test_warmup_default_is_documented_in_the_payload(emitted: list[object]) -> None:
    assert _run(["stream", "video", "c270", "--json"]) == 0
    warmup = emitted[-1]["warmup"]
    assert warmup["frames"] == stream.DEFAULT_WARMUP_FRAMES == 30
    assert warmup["mechanism"]
    assert warmup["caveat"]
    assert warmup["source"].startswith("default")
    assert any("--warmup-frames" in override for override in warmup["overrides"])
    # The default is measured, not guessed, and the payload has to say so.
    assert "measured" in warmup["basis"]


def test_stream_and_record_share_one_measured_warmup_constant() -> None:
    """The two verbs disagreed while both were guesses; they cannot now drift."""
    from webcam_cli.cli._commands import record

    assert stream.DEFAULT_WARMUP_FRAMES == engine.DEFAULT_WARMUP_FRAMES
    assert record._DEFAULT_WARMUP_VIDEO_S == engine.warmup_seconds(
        engine.DEFAULT_WARMUP_FRAMES, engine.WARMUP_FPS_ASSUMPTION
    )


def test_warmup_frames_are_overridable_and_zero_disables(
    monkeypatch: pytest.MonkeyPatch, emitted: list[object]
) -> None:
    _apply_ready(monkeypatch)
    slept: list[float] = []
    monkeypatch.setattr(stream, "_sleep", lambda seconds: slept.append(seconds))

    rc = _run(
        ["stream", "video", "c270", "--apply", "--port", "0", "--warmup-frames", "60", "--json"]
    )
    assert rc == 0
    assert slept == [2.0]
    assert emitted[-1]["warmup"]["ms"] == 2000.0

    slept.clear()
    rc = _run(
        ["stream", "video", "c270", "--apply", "--port", "0", "--warmup-frames", "0", "--json"]
    )
    assert rc == 0
    assert slept == []
    assert emitted[-1]["warmup"]["ms"] == 0.0


def test_warmup_ms_overrides_frames_and_is_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch, emitted: list[object]
) -> None:
    _apply_ready(monkeypatch)
    slept: list[float] = []
    monkeypatch.setattr(stream, "_sleep", lambda seconds: slept.append(seconds))

    rc = _run(["stream", "video", "c270", "--apply", "--port", "0", "--warmup-ms", "250", "--json"])
    assert rc == 0
    assert slept == [0.25]
    assert emitted[-1]["warmup"]["source"] == "--warmup-ms"

    parser = _parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["stream", "video", "c270", "--warmup-ms", "250", "--warmup-frames", "10"]
        )


def test_audio_warmup_default_is_expressed_in_milliseconds(emitted: list[object]) -> None:
    assert _run(["stream", "audio", "c270", "--json"]) == 0
    warmup = emitted[-1]["warmup"]
    assert warmup["ms"] == stream.DEFAULT_AUDIO_WARMUP_MS == 200.0
    assert warmup["frames"] is None


# --- device shape -----------------------------------------------------------


def test_video_on_a_microphone_only_device_is_a_user_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(devices, "resolve", lambda selector, **kw: _c270(with_camera=False))
    with pytest.raises(CliError) as exc:
        _run(["stream", "video", "c270"])
    assert exc.value.code == EXIT_USER_ERROR
    assert "no camera" in exc.value.message.lower()


def test_audio_on_a_device_without_a_microphone_is_a_user_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(devices, "resolve", lambda selector, **kw: _c270(with_audio=False))
    with pytest.raises(CliError) as exc:
        _run(["stream", "audio", "c270"])
    assert exc.value.code == EXIT_USER_ERROR
    assert "microphone" in exc.value.message.lower()


def test_av_requires_both_a_camera_and_a_microphone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices, "resolve", lambda selector, **kw: _c270(with_audio=False))
    with pytest.raises(CliError) as exc:
        _run(["stream", "av", "c270"])
    assert exc.value.code == EXIT_USER_ERROR


def test_an_unresolvable_selector_propagates_the_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _resolve(selector: str, **kwargs: object) -> devices.LogicalDevice:
        raise CliError(EXIT_USER_ERROR, f"no device matches {selector!r}", "run `webcam list`")

    monkeypatch.setattr(devices, "resolve", _resolve)
    with pytest.raises(CliError) as exc:
        _run(["stream", "video", "nope"])
    assert exc.value.code == EXIT_USER_ERROR


# --- encoder routing on real capability ------------------------------------


def test_vp8_encoding_requires_the_probed_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_ready(monkeypatch, vp8enc=False)
    with pytest.raises(CliError) as exc:
        _run(["stream", "video", "c270", "--apply", "--port", "0", "--encode", "vp8"])
    assert exc.value.code == EXIT_ENV_ERROR
    assert "vp8enc" in exc.value.message


def test_vp8_encoding_builds_an_encoded_matroska_chain(
    monkeypatch: pytest.MonkeyPatch, emitted: list[object]
) -> None:
    procs = _apply_ready(monkeypatch, vp8enc=True)
    rc = _run(["stream", "video", "c270", "--apply", "--port", "0", "--encode", "vp8", "--json"])
    assert rc == 0
    argv = procs[0].argv
    assert "vp8enc" in argv
    assert "jpegdec" in argv  # negotiated default is MJPG, so it must be decoded first
    assert emitted[-1]["attach"]["streams"]["video"]["encoding"] == "vp8"


def test_opus_audio_encoding_is_capability_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_ready(monkeypatch, opusenc=False)
    with pytest.raises(CliError) as exc:
        _run(["stream", "audio", "c270", "--apply", "--port", "0", "--encode", "opus"])
    assert exc.value.code == EXIT_ENV_ERROR
    assert "opusenc" in exc.value.message


def test_av_has_no_encode_flag() -> None:
    parser = _parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["stream", "av", "c270", "--encode", "vp8"])


# --- process plumbing -------------------------------------------------------
#
# These exercise the real _spawn/_supervise seam against a throwaway Python
# interpreter, not gst-launch-1.0. No camera or microphone is involved, so
# deviation d1 holds: this is the subprocess plumbing t9's live run depends on,
# proven without energizing anything.


def test_spawn_keeps_child_output_off_our_streams(capfd: pytest.CaptureFixture[str]) -> None:
    proc = _REAL_SPAWN([sys.executable, "-c", "import sys; print('chatty child'); sys.exit(3)"])
    assert proc.wait() == 3
    captured = capfd.readouterr()
    assert captured.out == ""  # a media stream must never share our stdout
    assert captured.err == ""
    assert "chatty child" in proc.output_tail()


def test_spawn_reports_a_missing_binary_as_a_typed_error() -> None:
    with pytest.raises(CliError) as exc:
        _REAL_SPAWN(["/nonexistent/gst-launch-1.0"])
    assert exc.value.code == EXIT_ENV_ERROR
    assert exc.value.remediation


def test_terminate_reaps_a_running_child() -> None:
    proc = _REAL_SPAWN([sys.executable, "-c", "import time; time.sleep(30)"])
    assert proc.poll() is None
    proc.terminate()
    assert proc.poll() is not None


def test_supervise_returns_the_child_exit_status() -> None:
    proc = _REAL_SPAWN([sys.executable, "-c", "raise SystemExit(4)"])
    assert stream._supervise(proc) == 4


def test_supervise_reaps_the_child_on_interrupt() -> None:
    class _Interruptible(_FakeProc):
        def wait(self) -> int:
            raise KeyboardInterrupt

    proc = _Interruptible()
    assert stream._supervise(proc) == 0
    assert proc.terminated
