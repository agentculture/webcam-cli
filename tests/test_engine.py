"""Tests for the GStreamer engine adapter (webcam_cli.engine).

Hermetic by construction: every subprocess boundary (``shutil.which`` and
``subprocess.run``) is monkeypatched. No test opens, streams from, or
otherwise energizes a real camera or microphone — samples below are captured
gst-inspect-1.0 / gst-device-monitor-1.0 shapes, replayed through fakes.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from webcam_cli import engine
from webcam_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

# --- fixtures / fakes -------------------------------------------------------


@dataclass
class _FakeCompletedProcess:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _which_both_present(name: str) -> str | None:
    if name == engine.GST_LAUNCH:
        return "/usr/bin/gst-launch-1.0"
    if name == engine.GST_INSPECT:
        return "/usr/bin/gst-inspect-1.0"
    if name == engine.GST_DEVICE_MONITOR:
        return "/usr/bin/gst-device-monitor-1.0"
    return None


def _make_inspect_run(missing: set[str]) -> "callable":
    """Build a fake subprocess.run for `gst-inspect-1.0 <element>` probes.

    Mirrors real gst-inspect-1.0 behaviour observed on-host: exit 0 with
    element details on stdout when present, exit 255 with
    "No such element or plugin '<name>'" on stderr when absent.
    """

    def _run(argv, **kwargs):  # noqa: ANN001 - test double, signature mirrors subprocess.run
        element = argv[-1]
        if element in missing:
            return _FakeCompletedProcess(
                returncode=255, stderr=f"No such element or plugin '{element}'\n"
            )
        return _FakeCompletedProcess(returncode=0, stdout=f"Factory Details:\n  ... {element}\n")

    return _run


# A realistic gst-device-monitor-1.0 "Video/Source" transcript, hand-built to
# match the tool's observed shape (Device found: blocks, a tab-indented
# `caps  :` header with wrapped continuation lines, a `properties:` section
# carrying `device.path`). Deliberately includes: duplicate caps entries (to
# exercise de-dup), a range-valued caps line (unparseable -> must be skipped,
# not guessed at), and an unsupported media type (video/x-bayer -> skipped).
_DEVICE_MONITOR_OUTPUT = (
    "Probing devices...\n"
    "\n"
    "\n"
    "Device found:\n"
    "\n"
    "\tname  : HD Webcam C270\n"
    "\tclass : Video/Source\n"
    "\tcaps  : image/jpeg, width=(int)1280, height=(int)720, framerate=(fraction)30/1\n"
    "\t        image/jpeg, width=(int)1280, height=(int)720, framerate=(fraction)15/2\n"
    "\t        image/jpeg, width=(int)1280, height=(int)720, framerate=(fraction)30/1\n"
    "\t        video/x-raw, format=(string)YUY2, width=(int)640, height=(int)480, "
    "framerate=(fraction)30/1\n"
    "\t        video/x-raw, format=(string)YUY2, width=(int)[1, 640], "
    "height=(int)[1, 480], framerate=(fraction)[0/1, 2147483647/1]\n"
    "\t        video/x-bayer, format=(string)bggr, width=(int)640, height=(int)480, "
    "framerate=(fraction)30/1\n"
    "\tproperties:\n"
    "\t\tudev-probed = true\n"
    "\t\tsysfs.path = /sys/class/video4linux/video0\n"
    "\t\tdevice.vendor.name = Logitech\n"
    "\t\tdevice.serial = Logitech_HD_Webcam_C270_200901010001\n"
    "\t\tdevice.api = v4l2\n"
    "\t\tdevice.path = /dev/video0\n"
    "\t\tv4l2.device.driver = uvcvideo\n"
    "\tgst-launch-1.0 v4l2src device=/dev/video0 ! ...\n"
    "\n"
    "\n"
    "Device found:\n"
    "\n"
    "\tname  : Arducam\n"
    "\tclass : Video/Source\n"
    "\tcaps  : video/x-raw, format=(string)YUYV, width=(int)1920, height=(int)1080, "
    "framerate=(fraction)30/1\n"
    "\tproperties:\n"
    "\t\tdevice.path = /dev/video2\n"
    "\t\tdevice.api = v4l2\n"
    "\tgst-launch-1.0 v4l2src device=/dev/video2 ! ...\n"
    "\n"
)


# The transcript actually observed on the reference host (GStreamer 1.24.2,
# Ubuntu, PipeWire running). PipeWire's device provider calls
# gst_device_provider_hide_provider("v4l2deviceprovider"), so *its* spelling is
# the only one gst-device-monitor-1.0 emits here, and it differs from the
# GStreamer v4l2 provider's in three ways that each independently broke
# parsing before task t9 measured it on hardware:
#
#   1. no `device.path` property at all — the node path arrives as
#      `api.v4l2.path` and as `object.path = v4l2:/dev/videoN`;
#   2. caps are serialized *without* type annotations (`width=640`, not
#      `width=(int)640`);
#   3. every framerate is a *list* (`framerate={ (fraction)30/1, ... }`), not
#      the single fraction the annotated spelling carries.
#
# Trimmed to three caps lines per device; otherwise byte-faithful.
_PIPEWIRE_DEVICE_MONITOR_OUTPUT = (
    "Probing devices...\n"
    "\n"
    "\n"
    "Device found:\n"
    "\n"
    "\tname  : C270 HD WEBCAM (V4L2)\n"
    "\tclass : Video/Source\n"
    "\tcaps  : video/x-raw, format=YUY2, width=640, height=480, framerate={ "
    "(fraction)30/1, (fraction)15/1 }\n"
    "\t        video/x-raw, format=YUY2, width=(int)[1, 640], height=(int)[1, 480], "
    "framerate=[ 0/1, 2147483647/1 ]\n"
    "\t        image/jpeg, width=1280, height=720, framerate={ (fraction)30/1, "
    "(fraction)5/1 }\n"
    "\tproperties:\n"
    "\t\tapi.v4l2.cap.card = C270 HD WEBCAM\n"
    "\t\tapi.v4l2.cap.driver = uvcvideo\n"
    "\t\tapi.v4l2.path = /dev/video0\n"
    "\t\tdevice.api = v4l2\n"
    "\t\tfactory.name = api.v4l2.source\n"
    "\t\tmedia.class = Video/Source\n"
    "\t\tobject.path = v4l2:/dev/video0\n"
    "\tgst-launch-1.0 pipewiresrc target-object=76 ! ...\n"
    "\n"
    "\n"
    "Device found:\n"
    "\n"
    "\tname  : Arducam_12MP (V4L2)\n"
    "\tclass : Video/Source\n"
    "\tcaps  : image/jpeg, width=1920, height=1080, framerate={ (fraction)30/1 }\n"
    "\tproperties:\n"
    "\t\tapi.v4l2.path = /dev/video2\n"
    "\t\tobject.path = v4l2:/dev/video2\n"
    "\tgst-launch-1.0 pipewiresrc target-object=78 ! ...\n"
    "\n"
)


# Same shape, but with `api.v4l2.path` removed so only the `object.path =
# v4l2:/dev/videoN` spelling is left to match on.
_OBJECT_PATH_ONLY_OUTPUT = _PIPEWIRE_DEVICE_MONITOR_OUTPUT.replace(
    "\t\tapi.v4l2.path = /dev/video0\n", ""
)


# --- Capability.detect() -----------------------------------------------------


def test_detect_reports_available_when_core_elements_present(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)
    monkeypatch.setattr(engine.subprocess, "run", _make_inspect_run(missing={"x264enc"}))

    cap = engine.detect()

    assert cap.gst_launch == "/usr/bin/gst-launch-1.0"
    assert cap.gst_inspect == "/usr/bin/gst-inspect-1.0"
    assert cap.available is True
    assert cap.plugins["v4l2src"] is True
    assert cap.plugins["alsasrc"] is True
    assert cap.plugins["matroskamux"] is True
    # x264enc is absent on the reference host; it must not block availability.
    assert cap.plugins["x264enc"] is False


def test_detect_gst_launch_absent_makes_capability_unavailable(monkeypatch):
    monkeypatch.setattr(
        engine.shutil,
        "which",
        lambda name: ("/usr/bin/gst-inspect-1.0" if name == engine.GST_INSPECT else None),
    )
    monkeypatch.setattr(engine.subprocess, "run", _make_inspect_run(missing=set()))

    cap = engine.detect()

    assert cap.gst_launch is None
    assert cap.available is False


def test_detect_missing_core_element_makes_capability_unavailable(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)
    monkeypatch.setattr(engine.subprocess, "run", _make_inspect_run(missing={"alsasrc"}))

    cap = engine.detect()

    assert cap.plugins["alsasrc"] is False
    assert cap.plugins["v4l2src"] is True
    assert cap.available is False


def test_detect_gst_inspect_absent_reports_all_plugins_false(monkeypatch):
    monkeypatch.setattr(
        engine.shutil,
        "which",
        lambda name: ("/usr/bin/gst-launch-1.0" if name == engine.GST_LAUNCH else None),
    )

    def _boom(*_a, **_kw):
        raise AssertionError("subprocess.run must not be called when gst-inspect is absent")

    monkeypatch.setattr(engine.subprocess, "run", _boom)

    cap = engine.detect()

    assert cap.gst_inspect is None
    assert all(present is False for present in cap.plugins.values())
    assert cap.available is False


def test_detect_survives_oserror_from_inspect_probe(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)

    def _raise(*_a, **_kw):
        raise OSError("boom")

    monkeypatch.setattr(engine.subprocess, "run", _raise)

    cap = engine.detect()

    assert all(present is False for present in cap.plugins.values())
    assert cap.available is False


# --- require_engine() --------------------------------------------------------


def test_require_engine_raises_typed_exit_2_when_absent(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", lambda _name: None)

    with pytest.raises(CliError) as exc_info:
        engine.require_engine()

    err = exc_info.value
    assert err.code == EXIT_ENV_ERROR
    assert err.code == 2
    assert err.remediation
    assert "install" in err.remediation.lower()


def test_require_engine_names_missing_core_element(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)
    monkeypatch.setattr(engine.subprocess, "run", _make_inspect_run(missing={"matroskamux"}))

    with pytest.raises(CliError) as exc_info:
        engine.require_engine()

    assert exc_info.value.code == EXIT_ENV_ERROR
    assert "matroskamux" in exc_info.value.message


def test_require_engine_returns_capability_when_available(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)
    monkeypatch.setattr(engine.subprocess, "run", _make_inspect_run(missing=set()))

    cap = engine.require_engine()

    assert cap.available is True


# --- probe_formats() ---------------------------------------------------------


def test_probe_formats_parses_and_dedupes_matching_device(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)
    monkeypatch.setattr(
        engine.subprocess,
        "run",
        lambda *_a, **_kw: _FakeCompletedProcess(returncode=0, stdout=_DEVICE_MONITOR_OUTPUT),
    )
    monkeypatch.setattr(engine.os.path, "realpath", lambda p: p)

    formats = engine.probe_formats("/dev/video0")

    assert formats == (
        engine.VideoFormat(pixel_format="MJPG", width=1280, height=720, fps=30.0),
        engine.VideoFormat(pixel_format="MJPG", width=1280, height=720, fps=7.5),
        engine.VideoFormat(pixel_format="YUYV", width=640, height=480, fps=30.0),
    )


def test_probe_formats_ignores_other_devices_in_output(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)
    monkeypatch.setattr(
        engine.subprocess,
        "run",
        lambda *_a, **_kw: _FakeCompletedProcess(returncode=0, stdout=_DEVICE_MONITOR_OUTPUT),
    )
    monkeypatch.setattr(engine.os.path, "realpath", lambda p: p)

    formats = engine.probe_formats("/dev/video2")

    assert formats == (engine.VideoFormat(pixel_format="YUYV", width=1920, height=1080, fps=30.0),)


def test_probe_formats_no_matching_device_returns_empty_tuple(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)
    monkeypatch.setattr(
        engine.subprocess,
        "run",
        lambda *_a, **_kw: _FakeCompletedProcess(returncode=0, stdout=_DEVICE_MONITOR_OUTPUT),
    )
    monkeypatch.setattr(engine.os.path, "realpath", lambda p: p)

    assert engine.probe_formats("/dev/video9") == ()


def test_probe_formats_garbled_output_returns_empty_tuple_not_crash(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)
    monkeypatch.setattr(
        engine.subprocess,
        "run",
        lambda *_a, **_kw: _FakeCompletedProcess(returncode=0, stdout="not a device listing\n"),
    )
    monkeypatch.setattr(engine.os.path, "realpath", lambda p: p)

    assert engine.probe_formats("/dev/video0") == ()


def _fake_monitor(monkeypatch, output: str) -> None:
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)
    monkeypatch.setattr(
        engine.subprocess,
        "run",
        lambda *_a, **_kw: _FakeCompletedProcess(returncode=0, stdout=output),
    )
    monkeypatch.setattr(engine.os.path, "realpath", lambda p: p)


def test_probe_formats_parses_the_pipewire_provider_transcript(monkeypatch):
    """The spelling the reference host actually emits must parse (t9 finding).

    Exercises all three divergences at once: the node path arrives as
    ``api.v4l2.path``, caps carry no type annotations, and every framerate is
    a list that has to be expanded into one VideoFormat per rate.
    """
    _fake_monitor(monkeypatch, _PIPEWIRE_DEVICE_MONITOR_OUTPUT)

    formats = engine.probe_formats("/dev/video0")

    assert formats == (
        engine.VideoFormat(pixel_format="YUYV", width=640, height=480, fps=30.0),
        engine.VideoFormat(pixel_format="YUYV", width=640, height=480, fps=15.0),
        engine.VideoFormat(pixel_format="MJPG", width=1280, height=720, fps=30.0),
        engine.VideoFormat(pixel_format="MJPG", width=1280, height=720, fps=5.0),
    )


def test_probe_formats_matches_object_path_spelling(monkeypatch):
    """`object.path = v4l2:/dev/videoN` alone is enough to identify the device."""
    _fake_monitor(monkeypatch, _OBJECT_PATH_ONLY_OUTPUT)

    formats = engine.probe_formats("/dev/video0")

    assert [fmt.pixel_format for fmt in formats] == ["YUYV", "YUYV", "MJPG", "MJPG"]


def test_probe_formats_pipewire_transcript_isolates_devices(monkeypatch):
    _fake_monitor(monkeypatch, _PIPEWIRE_DEVICE_MONITOR_OUTPUT)

    assert engine.probe_formats("/dev/video2") == (
        engine.VideoFormat(pixel_format="MJPG", width=1920, height=1080, fps=30.0),
    )


def test_probe_formats_skips_unannotated_range_valued_caps(monkeypatch):
    """A range is not an enumeration — it must be skipped, never guessed at."""
    _fake_monitor(monkeypatch, _PIPEWIRE_DEVICE_MONITOR_OUTPUT)

    formats = engine.probe_formats("/dev/video0")

    # The range line would have yielded 640x480 entries with a bogus fps.
    assert all(fmt.fps in (30.0, 15.0, 5.0) for fmt in formats)
    assert len(formats) == 4


def test_probe_formats_binary_missing_raises_typed_env_error(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", lambda _name: None)

    with pytest.raises(CliError) as exc_info:
        engine.probe_formats("/dev/video0")

    assert exc_info.value.code == EXIT_ENV_ERROR
    assert exc_info.value.remediation


def test_probe_formats_nonzero_exit_raises_typed_env_error(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)
    monkeypatch.setattr(
        engine.subprocess,
        "run",
        lambda *_a, **_kw: _FakeCompletedProcess(
            returncode=1, stdout="", stderr="no providers found\n"
        ),
    )

    with pytest.raises(CliError) as exc_info:
        engine.probe_formats("/dev/video0")

    assert exc_info.value.code == EXIT_ENV_ERROR


def test_probe_formats_timeout_raises_typed_env_error(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", _which_both_present)

    def _raise(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="gst-device-monitor-1.0", timeout=10)

    monkeypatch.setattr(engine.subprocess, "run", _raise)

    with pytest.raises(CliError) as exc_info:
        engine.probe_formats("/dev/video0")

    assert exc_info.value.code == EXIT_ENV_ERROR


# --- validate_negotiation() ---------------------------------------------------

_MJPG_720P = engine.VideoFormat(pixel_format="MJPG", width=1280, height=720, fps=30.0)
_MJPG_1080P_15 = engine.VideoFormat(pixel_format="MJPG", width=1920, height=1080, fps=15.0)
_YUYV_VGA = engine.VideoFormat(pixel_format="YUYV", width=640, height=480, fps=30.0)
_AVAILABLE = (_MJPG_720P, _MJPG_1080P_15, _YUYV_VGA)


def test_validate_negotiation_returns_exact_match():
    granted = engine.validate_negotiation(_YUYV_VGA, _AVAILABLE)
    assert granted == _YUYV_VGA


def test_validate_negotiation_none_picks_largest_mjpg_area():
    # 1920x1080 (2_073_600 px) beats 1280x720 (921_600 px); MJPG is preferred
    # over raw YUYV when present at all, per the documented default heuristic.
    granted = engine.validate_negotiation(None, _AVAILABLE)
    assert granted == _MJPG_1080P_15


def test_validate_negotiation_none_falls_back_to_raw_when_no_mjpg():
    available = (_YUYV_VGA,)
    granted = engine.validate_negotiation(None, available)
    assert granted == _YUYV_VGA


def test_validate_negotiation_unsatisfiable_raises_user_error_naming_alternatives():
    requested = engine.VideoFormat(pixel_format="MJPG", width=99999, height=99999, fps=999.0)

    with pytest.raises(CliError) as exc_info:
        engine.validate_negotiation(requested, _AVAILABLE)

    err = exc_info.value
    assert err.code == EXIT_USER_ERROR
    assert "99999" in err.message
    for fmt in _AVAILABLE:
        assert fmt.pixel_format in err.remediation
        assert str(fmt.width) in err.remediation


def test_validate_negotiation_never_silently_substitutes():
    # A close-but-not-identical fps must not be silently rounded to a match.
    requested = engine.VideoFormat(pixel_format="MJPG", width=1280, height=720, fps=29.0)
    with pytest.raises(CliError):
        engine.validate_negotiation(requested, _AVAILABLE)


def test_validate_negotiation_empty_available_raises_user_error():
    with pytest.raises(CliError) as exc_info:
        engine.validate_negotiation(None, ())
    assert exc_info.value.code == EXIT_USER_ERROR


# --- build_video_pipeline() ---------------------------------------------------


def test_build_video_pipeline_mjpg_shape():
    argv = engine.build_video_pipeline(
        "/dev/v4l/by-id/usb-046d_C270_HD_WEBCAM_200901010001-video-index0",
        _MJPG_720P,
        "filesink location=/tmp/out.mjpeg",
    )

    assert argv[0] == "gst-launch-1.0"
    assert argv[1] == "v4l2src"
    assert "device=/dev/v4l/by-id/usb-046d_C270_HD_WEBCAM_200901010001-video-index0" in argv
    assert "!" in argv
    caps = [tok for tok in argv if tok.startswith("image/jpeg")][0]
    assert "width=1280" in caps
    assert "height=720" in caps
    assert "framerate=30/1" in caps
    assert " " not in caps  # single argv token, no shell-splitting ambiguity
    assert argv[-2:] == ["filesink", "location=/tmp/out.mjpeg"]


def test_build_video_pipeline_raw_format_maps_to_gstreamer_name():
    argv = engine.build_video_pipeline("/dev/video0", _YUYV_VGA, "autovideosink")

    caps = [tok for tok in argv if tok.startswith("video/x-raw")][0]
    assert "format=YUY2" in caps  # V4L2 YUYV -> GStreamer YUY2
    assert "width=640" in caps
    assert "height=480" in caps
    assert argv[-1] == "autovideosink"


def test_build_video_pipeline_non_integer_fps_uses_fraction():
    fmt = engine.VideoFormat(pixel_format="YUYV", width=640, height=480, fps=7.5)
    argv = engine.build_video_pipeline("/dev/video0", fmt, "fakesink")
    caps = [tok for tok in argv if tok.startswith("video/x-raw")][0]
    assert "framerate=15/2" in caps


def test_build_video_pipeline_rejects_empty_sink():
    with pytest.raises(CliError) as exc_info:
        engine.build_video_pipeline("/dev/video0", _MJPG_720P, "   ")
    assert exc_info.value.code == EXIT_USER_ERROR


def test_build_video_pipeline_rejects_non_positive_dimensions():
    bad = engine.VideoFormat(pixel_format="MJPG", width=0, height=720, fps=30.0)
    with pytest.raises(CliError):
        engine.build_video_pipeline("/dev/video0", bad, "fakesink")


# --- build_audio_pipeline() ---------------------------------------------------


def test_build_audio_pipeline_shape():
    fmt = engine.AudioFormat(rate=44100, channels=2)
    argv = engine.build_audio_pipeline("hw:CARD=C270,DEV=0", fmt, "filesink location=/tmp/a.wav")

    assert argv[0] == "gst-launch-1.0"
    assert argv[1] == "alsasrc"
    assert "device=hw:CARD=C270,DEV=0" in argv
    caps = [tok for tok in argv if tok.startswith("audio/x-raw")][0]
    assert "rate=44100" in caps
    assert "channels=2" in caps
    assert argv[-2:] == ["filesink", "location=/tmp/a.wav"]


def test_build_audio_pipeline_rejects_non_hw_address():
    fmt = engine.AudioFormat(rate=44100, channels=2)
    with pytest.raises(CliError) as exc_info:
        engine.build_audio_pipeline("pulse:default", fmt, "fakesink")
    assert exc_info.value.code == EXIT_USER_ERROR
    assert "hw:" in exc_info.value.remediation


def test_build_audio_pipeline_rejects_non_positive_channels():
    fmt = engine.AudioFormat(rate=44100, channels=0)
    with pytest.raises(CliError):
        engine.build_audio_pipeline("hw:CARD=C270,DEV=0", fmt, "fakesink")


# --- build_av_pipeline() -------------------------------------------------------


def test_build_av_pipeline_shape_uses_matroskamux():
    argv = engine.build_av_pipeline(
        "/dev/video0",
        _MJPG_720P,
        "hw:CARD=C270,DEV=0",
        engine.AudioFormat(rate=44100, channels=2),
        "filesink location=/tmp/out.mkv",
    )

    assert argv[0] == "gst-launch-1.0"
    assert "v4l2src" in argv
    assert "device=/dev/video0" in argv
    assert "alsasrc" in argv
    assert "device=hw:CARD=C270,DEV=0" in argv
    assert "matroskamux" in argv
    assert "name=mux" in argv
    assert argv.count("mux.") == 2  # one request pad per branch
    assert argv[-2:] == ["filesink", "location=/tmp/out.mkv"]

    # matroskamux must come after both branches feed "mux." and before the sink.
    mux_idx = argv.index("matroskamux")
    assert argv[mux_idx + 1] == "name=mux"
    assert argv[mux_idx + 2] == "!"
    assert argv[mux_idx + 3 :] == ["filesink", "location=/tmp/out.mkv"]


def test_build_av_pipeline_rejects_bad_alsa_address():
    afmt = engine.AudioFormat(rate=44100, channels=2)

    with pytest.raises(CliError) as exc_info:
        engine.build_av_pipeline("/dev/video0", _MJPG_720P, "not-an-alsa-address", afmt, "fakesink")
    assert exc_info.value.code == EXIT_USER_ERROR


def test_build_av_pipeline_argv_never_a_single_shell_string():
    argv = engine.build_av_pipeline(
        "/dev/video0",
        _MJPG_720P,
        "hw:CARD=C270,DEV=0",
        engine.AudioFormat(rate=44100, channels=2),
        "filesink location=/tmp/out.mkv",
    )
    assert isinstance(argv, list)
    assert all(isinstance(tok, str) for tok in argv)
    assert all(" ! " not in tok for tok in argv)  # never a pre-joined shell string


# --- output_reports_device_busy() ---------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ERROR: from element GstV4l2Src:v4l2src0: Device '/dev/video0' is busy",
        "Call to S_FMT failed for MJPG @ 1280x960: Device or resource busy",
        "Could not open audio device for recording. Device is being used by another "
        "application.\nresource busy",
    ],
)
def test_output_reports_device_busy_recognises_the_engine_wording(text: str) -> None:
    """Wordings captured verbatim from gst-launch-1.0 on the reference host."""
    assert engine.output_reports_device_busy(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ERROR: from element v4l2src0: Internal data stream error.",
        "streaming stopped, reason not-negotiated (-4)",
    ],
)
def test_output_reports_device_busy_does_not_over_claim(text: str) -> None:
    assert engine.output_reports_device_busy(text) is False


def test_warmup_seconds_converts_frames_through_fps() -> None:
    assert engine.warmup_seconds(30, 30.0) == 1.0
    assert engine.warmup_seconds(30, 5.0) == 6.0
    assert engine.warmup_seconds(0, 30.0) == 0.0
    assert engine.warmup_seconds(30, 0.0) == 0.0
