"""GStreamer engine adapter: capability detection, format probing, negotiation
validation, and pipeline construction for video / audio / muxed A-V capture.

This module owns *what the capture stack can do and how to invoke it*. It does
not own device enumeration/pairing, permission checks, or CLI verbs — those are
sibling concerns built on top of this one. Every function here takes plain
path/address strings; nothing here imports ``webcam_cli.devices`` or
``webcam_cli.access``.

Zero runtime dependencies: everything shells out to the ``gst-launch-1.0`` /
``gst-inspect-1.0`` / ``gst-device-monitor-1.0`` binaries via
:mod:`subprocess`. No PyGObject/``gi`` import, ever — that would end the
zero-runtime-dependency posture ``pyproject.toml``'s ``dependencies = []``
depends on.

Pipeline builders (:func:`build_video_pipeline`, :func:`build_audio_pipeline`,
:func:`build_av_pipeline`) are pure string construction — no subprocess call —
so they are trivially unit-testable and never touch a device. They return an
argv list, never a shell string, because the project runs subprocesses
without a shell. Capability detection (:func:`detect`, :func:`require_engine`)
and format probing (:func:`probe_formats`) are the only functions here that
shell out.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess  # nosec B404 - shelling out to gst-* is the documented engine posture
from dataclasses import dataclass
from fractions import Fraction
from typing import Sequence

from webcam_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

GST_LAUNCH = "gst-launch-1.0"
GST_INSPECT = "gst-inspect-1.0"
GST_DEVICE_MONITOR = "gst-device-monitor-1.0"

# Core elements gate Capability.available: without gst-launch-1.0 itself plus
# a v4l2 source, an alsa source, and a muxer, none of the three pipeline
# shapes this module builds (video / audio / muxed A-V) can run at all.
_CORE_ELEMENTS: tuple[str, ...] = ("v4l2src", "alsasrc", "matroskamux")

# Optional elements: surfaced in Capability.plugins so a caller can branch on
# them (e.g. this reference host lacks x264enc, so H.264/MP4 encoding is off
# the menu here and VP8+Opus-in-Matroska/WebM is the viable encoded path —
# see the module docstring in tests/test_engine.py and the build brief).
# Missing optional elements never make Capability.available False.
_OPTIONAL_ELEMENTS: tuple[str, ...] = (
    "pulsesrc",
    "jpegenc",
    "vp8enc",
    "theoraenc",
    "opusenc",
    "wavenc",
    "lamemp3enc",
    "x264enc",
    "avimux",
    "mp4mux",
    "oggmux",
    "splitmuxsink",
    "multifilesink",
    "hlssink2",
    "tcpserversink",
    "fdsink",
)

_ALL_ELEMENTS: tuple[str, ...] = _CORE_ELEMENTS + _OPTIONAL_ELEMENTS

# Timeout (seconds) applied to every gst-* subprocess call in this module.
# Q4 in the build brief demands "fail fast, never hang" for device access;
# the same posture applies to capability/format probing.
_PROBE_TIMEOUT_S = 10

_INSTALL_HINT = (
    "install GStreamer with the tools plus good/base plugin sets, e.g. "
    "'sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-good "
    "gstreamer1.0-plugins-base gstreamer1.0-plugins-bad'"
)

# GStreamer raw-video format tokens differ from the V4L2/v4l2-ctl fourcc
# spelling this module's public VideoFormat.pixel_format uses (e.g. GStreamer
# "YUY2" vs V4L2 "YUYV" for the same byte layout). probe_formats() translates
# GStreamer -> V4L2 spelling on the way in; the pipeline builders translate
# back on the way out. Formats with no listed alias pass through unchanged.
_GST_TO_V4L2_FORMAT = {
    "YUY2": "YUYV",
    "I420": "YU12",
}
_V4L2_TO_GST_FORMAT = {v4l2: gst for gst, v4l2 in _GST_TO_V4L2_FORMAT.items()}


@dataclass(frozen=True)
class Capability:
    """What the GStreamer engine can do on this host."""

    gst_launch: str | None
    gst_inspect: str | None
    plugins: dict[str, bool]
    available: bool


@dataclass(frozen=True)
class VideoFormat:
    """A negotiable/negotiated video format: V4L2-style fourcc + geometry."""

    pixel_format: str
    width: int
    height: int
    fps: float


@dataclass(frozen=True)
class AudioFormat:
    """A negotiable/negotiated audio format."""

    rate: int
    channels: int


# --- capability detection ----------------------------------------------------


def _element_present(gst_inspect: str, element: str) -> bool:
    """Probe a single element via ``gst-inspect-1.0 <element>``.

    Observed on-host behaviour: exit 0 with details on stdout when the
    element/plugin is present; a non-zero exit (255 on this host) with
    "No such element or plugin '<name>'" on stderr when it is absent.
    Any failure to even run the probe is treated as absent, never a crash.
    """
    try:
        result = subprocess.run(
            [gst_inspect, element],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def detect() -> Capability:
    """Detect the GStreamer engine and report its capability set.

    Never raises: an absent engine is reported as
    ``Capability(available=False, ...)``, not an exception. Use
    :func:`require_engine` when absence should be a typed error.
    """
    gst_launch = shutil.which(GST_LAUNCH)
    gst_inspect = shutil.which(GST_INSPECT)

    if gst_inspect is not None:
        plugins = {element: _element_present(gst_inspect, element) for element in _ALL_ELEMENTS}
    else:
        plugins = {element: False for element in _ALL_ELEMENTS}

    core_present = all(plugins[element] for element in _CORE_ELEMENTS)
    available = gst_launch is not None and core_present

    return Capability(
        gst_launch=gst_launch,
        gst_inspect=gst_inspect,
        plugins=plugins,
        available=available,
    )


def require_engine() -> Capability:
    """Return the detected :class:`Capability`, or raise a typed exit-2 error.

    Raises:
        CliError: ``code=EXIT_ENV_ERROR`` when ``gst-launch-1.0`` or any core
            element (``v4l2src``, ``alsasrc``, ``matroskamux``) is missing.
            Carries an install hint.
    """
    cap = detect()
    if cap.available:
        return cap

    if cap.gst_launch is None:
        message = f"{GST_LAUNCH} is not installed"
    else:
        missing = [element for element in _CORE_ELEMENTS if not cap.plugins.get(element, False)]
        message = f"required GStreamer element(s) missing: {', '.join(missing)}"

    raise CliError(EXIT_ENV_ERROR, message, remediation=_INSTALL_HINT)


# --- format probing -----------------------------------------------------------

_DEVICE_BLOCK_SPLIT_RE = re.compile(r"(?m)^Device found:\s*$")
_CAPS_HEADER_RE = re.compile(r"^\s*caps\s*:\s*(.*)$")
_PROPERTIES_HEADER_RE = re.compile(r"^\s*properties\s*:\s*$")
_DEVICE_PATH_RE = re.compile(r"^\s*device\.path\s*=\s*(.+?)\s*$")

_WIDTH_RE = re.compile(r"width=\(int\)(\d+)")
_HEIGHT_RE = re.compile(r"height=\(int\)(\d+)")
_FRAMERATE_RE = re.compile(r"framerate=\(fraction\)(\d+)/(\d+)")
_RAW_FORMAT_RE = re.compile(r"format=\(string\)([A-Za-z0-9_]+)")


def _normalize_path(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _parse_caps_line(line: str) -> VideoFormat | None:
    """Parse one ``gst-device-monitor-1.0`` caps line into a VideoFormat.

    Defensive by design: a line this module does not recognise (an
    unsupported media type, or a range/list-valued field such as
    ``width=(int)[1, 640]`` instead of a discrete value) is skipped rather
    than guessed at — never a crash, never a wrong answer.
    """
    media, _, rest = line.strip().partition(",")
    media = media.strip()

    if media == "image/jpeg":
        pixel_format = "MJPG"
    elif media == "video/x-raw":
        fmt_match = _RAW_FORMAT_RE.search(rest)
        if fmt_match is None:
            return None
        gst_format = fmt_match.group(1)
        pixel_format = _GST_TO_V4L2_FORMAT.get(gst_format, gst_format)
    else:
        return None

    width_match = _WIDTH_RE.search(rest)
    height_match = _HEIGHT_RE.search(rest)
    fps_match = _FRAMERATE_RE.search(rest)
    if not (width_match and height_match and fps_match):
        return None

    num, den = int(fps_match.group(1)), int(fps_match.group(2))
    if den == 0:
        return None

    return VideoFormat(
        pixel_format=pixel_format,
        width=int(width_match.group(1)),
        height=int(height_match.group(1)),
        fps=round(num / den, 3),
    )


def _parse_device_monitor_output(output: str, node_path: str) -> tuple[VideoFormat, ...]:
    blocks = _DEVICE_BLOCK_SPLIT_RE.split(output)[1:]
    target = _normalize_path(node_path)

    for block in blocks:
        device_path: str | None = None
        caps_lines: list[str] = []
        in_caps = False

        for line in block.splitlines():
            if in_caps:
                if _PROPERTIES_HEADER_RE.match(line):
                    in_caps = False
                    continue
                stripped = line.strip()
                if stripped:
                    caps_lines.append(stripped)
                continue

            caps_match = _CAPS_HEADER_RE.match(line)
            if caps_match:
                in_caps = True
                first = caps_match.group(1).strip()
                if first:
                    caps_lines.append(first)
                continue

            path_match = _DEVICE_PATH_RE.match(line)
            if path_match:
                device_path = path_match.group(1).strip()

        if device_path is None or _normalize_path(device_path) != target:
            continue

        formats: list[VideoFormat] = []
        seen: set[tuple[str, int, int, float]] = set()
        for caps_line in caps_lines:
            parsed = _parse_caps_line(caps_line)
            if parsed is None:
                continue
            key = (parsed.pixel_format, parsed.width, parsed.height, parsed.fps)
            if key in seen:
                continue
            seen.add(key)
            formats.append(parsed)
        return tuple(formats)

    return ()


def probe_formats(node_path: str) -> tuple[VideoFormat, ...]:
    """Enumerate the video formats ``node_path`` actually supports.

    Shells out to ``gst-device-monitor-1.0 Video/Source`` (a single-shot
    listing by default — it only blocks watching for hotplug when passed
    ``-f/--follow``, which this call never does) and parses its "Device
    found:" transcript for the block whose ``device.path`` property
    resolves to the same file as ``node_path``.

    Returns an empty tuple when the tool ran fine but reported no matching
    device or no parseable caps for it — that is a legitimate "nothing here"
    answer, not a parse failure. Raises a typed environment error only when
    the probe itself could not be trusted (binary missing, non-zero exit,
    timeout, or the process could not be started).
    """
    gst_device_monitor = shutil.which(GST_DEVICE_MONITOR)
    if gst_device_monitor is None:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{GST_DEVICE_MONITOR} is not installed",
            remediation=_INSTALL_HINT,
        )

    try:
        result = subprocess.run(
            [gst_device_monitor, "Video/Source"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{GST_DEVICE_MONITOR} timed out probing device formats",
            remediation="check the device is connected and not wedged; retry",
        ) from exc
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"failed to run {GST_DEVICE_MONITOR}: {exc}",
            remediation=_INSTALL_HINT,
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        message = f"{GST_DEVICE_MONITOR} exited {result.returncode} probing device formats"
        if detail:
            message = f"{message}: {detail}"
        raise CliError(
            EXIT_ENV_ERROR,
            message,
            remediation="check the GStreamer install and device permissions",
        )

    return _parse_device_monitor_output(result.stdout, node_path)


# --- negotiation ---------------------------------------------------------------


def _describe_video_format(fmt: VideoFormat) -> str:
    return f"{fmt.pixel_format} {fmt.width}x{fmt.height}@{fmt.fps}fps"


def _pick_default_format(available: Sequence[VideoFormat]) -> VideoFormat:
    """Choose a sensible default when no format was requested.

    Heuristic, documented rather than hidden: prefer MJPG (compressed)
    entries when any exist, since USB webcams typically support higher
    resolution/fps in MJPG than in raw formats before hitting USB bandwidth
    limits; within the chosen pool, pick the largest frame area, tie-broken
    by the highest fps. Ties beyond that keep the first-enumerated entry
    (``max`` is stable on ties), so the choice is deterministic.
    """
    mjpg = [fmt for fmt in available if fmt.pixel_format == "MJPG"]
    pool = mjpg if mjpg else list(available)
    return max(pool, key=lambda fmt: (fmt.width * fmt.height, fmt.fps))


def validate_negotiation(
    requested: VideoFormat | None,
    available: Sequence[VideoFormat],
) -> VideoFormat:
    """Validate ``requested`` against ``available`` and return what is granted.

    ``requested=None`` means "pick a sensible default" (see
    :func:`_pick_default_format`). An unsatisfiable request is a typed
    ``CliError(EXIT_USER_ERROR, ...)`` naming the enumerated alternatives —
    this function never silently substitutes a different format.
    """
    if not available:
        raise CliError(
            EXIT_USER_ERROR,
            "no formats available to negotiate against",
            remediation="probe the device's formats before requesting a stream",
        )

    if requested is None:
        return _pick_default_format(available)

    if requested in available:
        return requested

    alternatives = ", ".join(_describe_video_format(fmt) for fmt in available)
    raise CliError(
        EXIT_USER_ERROR,
        f"requested format {_describe_video_format(requested)} is not supported by this device",
        remediation=f"choose one of the enumerated formats: {alternatives}",
    )


# --- pipeline construction ------------------------------------------------------


def _fps_fraction(fps: float) -> str:
    frac = Fraction(fps).limit_denominator(1001)
    return f"{frac.numerator}/{frac.denominator}"


def _validate_video_format(fmt: VideoFormat) -> None:
    if fmt.width <= 0 or fmt.height <= 0 or fmt.fps <= 0:
        raise CliError(
            EXIT_USER_ERROR,
            f"invalid video format {_describe_video_format(fmt)}: "
            "width, height and fps must all be positive",
            remediation="pass a format returned by probe_formats() or validate_negotiation()",
        )
    if not fmt.pixel_format:
        raise CliError(
            EXIT_USER_ERROR,
            "invalid video format: pixel_format must not be empty",
            remediation="pass a format returned by probe_formats() or validate_negotiation()",
        )


def _validate_audio_format(fmt: AudioFormat) -> None:
    if fmt.rate <= 0 or fmt.channels <= 0:
        raise CliError(
            EXIT_USER_ERROR,
            f"invalid audio format rate={fmt.rate} channels={fmt.channels}: "
            "both must be positive",
            remediation="pass a rate/channels pair the device actually supports",
        )


def _validate_alsa_address(alsa_address: str) -> None:
    if not alsa_address.strip().lower().startswith("hw:"):
        raise CliError(
            EXIT_USER_ERROR,
            f"unsupported ALSA address {alsa_address!r}: expected an 'hw:CARD=...' address",
            remediation=(
                "use a direct ALSA hw address, e.g. 'hw:CARD=C270,DEV=0' (see 'arecord -l')"
            ),
        )


def _sink_tokens(sink: str) -> list[str]:
    tokens = shlex.split(sink)
    if not tokens:
        raise CliError(
            EXIT_USER_ERROR,
            "sink description must not be empty",
            remediation="pass a gst-launch sink element, e.g. 'filesink location=/tmp/out.mkv'",
        )
    return tokens


def _video_caps_string(fmt: VideoFormat) -> str:
    fps = _fps_fraction(fmt.fps)
    if fmt.pixel_format == "MJPG":
        return f"image/jpeg,width={fmt.width},height={fmt.height},framerate={fps}"
    gst_format = _V4L2_TO_GST_FORMAT.get(fmt.pixel_format, fmt.pixel_format)
    return f"video/x-raw,format={gst_format},width={fmt.width},height={fmt.height},framerate={fps}"


def _audio_caps_string(fmt: AudioFormat) -> str:
    return f"audio/x-raw,rate={fmt.rate},channels={fmt.channels}"


def build_video_pipeline(node_path: str, fmt: VideoFormat, sink: str) -> list[str]:
    """Build a ``gst-launch-1.0`` argv for a single video (``v4l2src``) stream."""
    _validate_video_format(fmt)
    return [
        GST_LAUNCH,
        "v4l2src",
        f"device={node_path}",
        "!",
        _video_caps_string(fmt),
        "!",
        *_sink_tokens(sink),
    ]


def build_audio_pipeline(alsa_address: str, fmt: AudioFormat, sink: str) -> list[str]:
    """Build a ``gst-launch-1.0`` argv for a single audio (``alsasrc``) stream.

    ``alsa_address`` must be a direct ALSA ``hw:`` address (e.g.
    ``hw:CARD=C270,DEV=0``) — see ``arecord -l``. PulseAudio addressing is
    out of scope: on the reference host the target device's PipeWire profile
    is off, so direct ALSA is the only path that works.
    """
    _validate_alsa_address(alsa_address)
    _validate_audio_format(fmt)
    return [
        GST_LAUNCH,
        "alsasrc",
        f"device={alsa_address}",
        "!",
        _audio_caps_string(fmt),
        "!",
        *_sink_tokens(sink),
    ]


def build_av_pipeline(
    node_path: str,
    fmt: VideoFormat,
    alsa_address: str,
    afmt: AudioFormat,
    sink: str,
) -> list[str]:
    """Build a ``gst-launch-1.0`` argv muxing video + audio into one sink.

    Uses the standard gst-launch named-pad idiom: both source branches link
    into ``mux.`` (a request pad on the element named ``mux``), and
    ``matroskamux name=mux`` is declared once, feeding ``sink``. Order is
    sources-then-muxer, which is the conventional and most portable spelling
    of this idiom, but is not order-sensitive to ``gst_parse_launch``.
    """
    _validate_video_format(fmt)
    _validate_audio_format(afmt)
    _validate_alsa_address(alsa_address)
    return [
        GST_LAUNCH,
        "v4l2src",
        f"device={node_path}",
        "!",
        _video_caps_string(fmt),
        "!",
        "mux.",
        "alsasrc",
        f"device={alsa_address}",
        "!",
        _audio_caps_string(afmt),
        "!",
        "mux.",
        "matroskamux",
        "name=mux",
        "!",
        *_sink_tokens(sink),
    ]
