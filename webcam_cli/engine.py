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

# --- measured sensor behaviour ------------------------------------------------

#: Frames of sensor warm-up to discard before frames are handed to a consumer.
#:
#: **Measured on the reference host's Logitech C270** (task t9, five cold
#: opens), by capturing MJPEG with warm-up disabled, decoding each frame to
#: GRAY8, and taking the frame at which mean luma first stays within 2% of its
#: final value. Reproduce with ``scripts/acceptance/warmup-measure.py``:
#:
#: ======================  ===  ===============  ===================
#: run                     fps  settle (frames)  settle (wall clock)
#: ======================  ===  ===============  ===================
#: 640x480, cold            30   15               0.50 s
#: 640x480, cold            30   12               0.40 s
#: 1280x720, cold           30   14               0.47 s
#: 640x480, cold             5   13               2.60 s
#: 640x480, cold             5   15               3.00 s
#: ======================  ===  ===============  ===================
#:
#: The 5 fps runs are the load-bearing ones: settle stayed in a 12-15 *frame*
#: band while wall-clock time ranged from 0.40 s to 3.00 s. **Auto-exposure
#: converges over a roughly constant number of frames, not a constant
#: interval**, so a warm-up default expressed in seconds is wrong at any frame
#: rate but the one it was tuned for — at 5 fps a 2 s default discards 10
#: frames and still ships unsettled ones. Both ``stream`` and ``record``
#: therefore derive their default from this frame count and the *negotiated*
#: fps, from this one constant, so they cannot drift apart again.
#:
#: 30 frames is about 2x the slowest settle measured. The margin is deliberate
#: and is *not* itself measured: all five runs were in one room over one
#: evening, and a genuinely dark scene should be expected to settle more
#: slowly. Callers who know their conditions can override
#: (``--warmup-frames`` / ``--warmup-ms`` on ``stream``, ``--warmup`` on
#: ``record``); 0 disables warm-up entirely.
DEFAULT_WARMUP_FRAMES = 30

#: fps assumed when a warm-up interval must be reported before any format has
#: been negotiated (an on-paper dry run). Always reported as assumed.
WARMUP_FPS_ASSUMPTION = 30.0


def warmup_seconds(frames: int, fps: float) -> float:
    """Convert a warm-up frame count into seconds at ``fps``."""
    if frames <= 0 or fps <= 0:
        return 0.0
    return frames / fps


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
        plugins = dict.fromkeys(_ALL_ELEMENTS, False)

    core_present = all(plugins[element] for element in _CORE_ELEMENTS)
    available = gst_launch is not None and core_present

    return Capability(
        gst_launch=gst_launch,
        gst_inspect=gst_inspect,
        plugins=plugins,
        available=available,
    )


#: How ``gst-launch-1.0`` says "somebody else already has this device".
#: Observed verbatim on the reference host when a second client opened a
#: camera the first was streaming from (task t9)::
#:
#:     ERROR: from element .../GstV4l2Src:v4l2src0: Device '/dev/video0' is busy
#:     Call to S_FMT failed for MJPG @ 1280x960: Device or resource busy
#:
#: This matters because **V4L2 exclusivity is invisible to ``open(2)``**:
#: uvcvideo permits several opens of the same node and only refuses at
#: ``S_FMT``/``STREAMON``, so a permission-style probe reports a camera that
#: another process is streaming from as perfectly openable. The engine's own
#: output is the only place the fact surfaces. (ALSA is the opposite — it
#: returns ``EBUSY`` from ``open`` — which is why audio never needed this.)
_DEVICE_BUSY_RE = re.compile(
    r"(?i)(device or resource busy|device\s+'[^']*'\s+is\s+busy|resource\s+busy)"
)


def output_reports_device_busy(text: str) -> bool:
    """True when engine output says the capture device was already held."""
    return bool(_DEVICE_BUSY_RE.search(text or ""))


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

# Trailing content is captured without a second `\s*` glued to it: `\s*`
# immediately followed by `(.*)` (or `.+`) is two quantifiers over
# overlapping character classes (`.` matches whitespace too), which is the
# super-linear-backtracking shape python:S8786 flags. Both callers already
# `.strip()` the piece they pull out, so dropping the redundant trailing
# `\s*` changes nothing about what ends up in `caps_lines` / property values
# — only how deterministically the regex engine gets there.
_CAPS_HEADER_RE = re.compile(r"^\s*caps\s*:(.*)$")
_PROPERTIES_HEADER_RE = re.compile(r"^\s*properties\s*:\s*$")

# Matches only the `key =` prefix of a properties line; the value is
# whatever text follows the match (see `_device_paths`), sliced in plain
# Python rather than captured by a second, overlapping-quantifier group.
_PROPERTY_RE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_.\-]+)\s*=\s*")

# Property keys that can carry the device's ``/dev/videoN`` path. Which of
# them appear depends on *which device provider* answered, and that is not a
# choice this tool gets to make:
#
# * ``device.path`` — GStreamer's own ``v4l2deviceprovider``;
# * ``api.v4l2.path`` / ``object.path`` — PipeWire's provider, which calls
#   ``gst_device_provider_hide_provider("v4l2deviceprovider")`` and therefore
#   *replaces* the GStreamer one wherever PipeWire is running (verified on the
#   reference host, GStreamer 1.24.2 — task t9).
#
# All three are accepted, so the probe works with or without PipeWire.
_DEVICE_PATH_KEYS = ("device.path", "api.v4l2.path", "object.path")

# ``object.path`` is namespaced: ``v4l2:/dev/video0``. Strip the scheme.
_OBJECT_PATH_SCHEME_RE = re.compile(r"^[A-Za-z0-9_.\-]+:(?=/)")

# A leading GStreamer type annotation: ``(int)640``, ``(string)YUY2``,
# ``(fraction)30/1``, ``(GstValueList){ ... }``.
_TYPE_ANNOTATION_RE = re.compile(r"^\([^)]*\)\s*")

_INT_VALUE_RE = re.compile(r"^\d+$")
_FRACTION_VALUE_RE = re.compile(r"^(?P<num>\d+)\s*/\s*(?P<den>\d+)$")


def _normalize_path(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside ``{...}`` or ``[...]``."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        if char == "," and depth <= 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _strip_type(value: str) -> str:
    return _TYPE_ANNOTATION_RE.sub("", value.strip()).strip()


def _expand_value(raw: str) -> list[str] | None:
    """Expand one serialized caps value into its discrete alternatives.

    Handles both spellings this tool has seen in the wild — annotated
    (``(int)640``) and bare (``640``) — and expands a list
    (``{ (fraction)30/1, (fraction)15/1 }``) into one entry per member,
    because a device that advertises a list really does support every rate
    in it.

    Returns ``None`` for a *range* (``[1, 640]``): a range is a continuum,
    not an enumeration, and inventing discrete points inside it would be
    exactly the silent guess this module refuses to make.
    """
    text = _strip_type(raw)
    if text.startswith("["):
        return None
    if text.startswith("{"):
        inner = text[1:-1] if text.endswith("}") else text[1:]
        return [_strip_type(item) for item in _split_top_level(inner)]
    return [text]


def _caps_fields(rest: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in _split_top_level(rest):
        key, separator, value = part.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def _ints(values: list[str] | None) -> list[int] | None:
    if values is None:
        return None
    parsed = [int(value) for value in values if _INT_VALUE_RE.match(value)]
    return parsed or None


def _framerates(values: list[str] | None) -> list[float] | None:
    if values is None:
        return None
    parsed: list[float] = []
    for value in values:
        matched = _FRACTION_VALUE_RE.match(value)
        if matched is None:
            continue
        num, den = int(matched.group("num")), int(matched.group("den"))
        if den == 0:
            continue
        parsed.append(round(num / den, 3))
    return parsed or None


def _pixel_formats(media: str, fields: dict[str, str]) -> list[str] | None:
    if media == "image/jpeg":
        return ["MJPG"]
    if media != "video/x-raw":
        return None
    values = _expand_value(fields.get("format", ""))
    if not values:
        return None
    return [_GST_TO_V4L2_FORMAT.get(value, value) for value in values if value]


def _parse_caps_line(line: str) -> tuple[VideoFormat, ...]:
    """Parse one ``gst-device-monitor-1.0`` caps line into zero or more formats.

    One caps line can describe many concrete formats, because any field may
    be list-valued; the result is the cross product of every field's discrete
    alternatives. Defensive by design: a line this module does not recognise
    (an unsupported media type, or any field given as a range rather than a
    discrete value) yields an empty tuple rather than a guess — never a
    crash, never a wrong answer.
    """
    media, _, rest = line.strip().partition(",")
    media = media.strip()

    fields = _caps_fields(rest)
    pixel_formats = _pixel_formats(media, fields)
    if not pixel_formats:
        return ()

    widths = _ints(_expand_value(fields.get("width", "")))
    heights = _ints(_expand_value(fields.get("height", "")))
    rates = _framerates(_expand_value(fields.get("framerate", "")))
    if not (widths and heights and rates):
        return ()

    return tuple(
        VideoFormat(pixel_format=pixel_format, width=width, height=height, fps=fps)
        for pixel_format in pixel_formats
        for width in widths
        for height in heights
        for fps in rates
    )


def _device_paths(line: str) -> list[str]:
    """Every ``/dev/...`` path a properties line claims for this device."""
    matched = _PROPERTY_RE.match(line)
    if matched is None or matched.group("key") not in _DEVICE_PATH_KEYS:
        return []
    raw_value = line[matched.end() :].strip()
    value = _OBJECT_PATH_SCHEME_RE.sub("", raw_value)
    return [value] if value.startswith("/") else []


def _classify_block_line(line: str, in_caps: bool) -> tuple[bool, str | None, str | None]:
    """Decide what one transcript line means, given the current caps-section state.

    A device block is a tiny state machine: outside a caps section, a line
    either opens one (``caps  : ...``) or is a candidate properties line;
    inside one, a line either closes it (``properties:``) or is a
    continuation of the caps listing. Returns
    ``(next_in_caps, caps_text, device_path_line)``:

    * ``caps_text`` is a non-empty caps fragment to record, or ``None``.
    * ``device_path_line`` is the raw line to scan for a device path
      property, or ``None`` when this line was consumed as caps text or a
      section boundary instead.
    """
    if in_caps:
        if _PROPERTIES_HEADER_RE.match(line):
            return False, None, None
        stripped = line.strip()
        return True, (stripped or None), None

    caps_match = _CAPS_HEADER_RE.match(line)
    if caps_match:
        first = caps_match.group(1).strip()
        return True, (first or None), None

    return False, None, line


def _parse_device_block(block: str) -> tuple[list[str], list[str]]:
    """Split one "Device found:" block into its device-path and caps lines."""
    device_paths: list[str] = []
    caps_lines: list[str] = []
    in_caps = False

    for line in block.splitlines():
        in_caps, caps_text, path_line = _classify_block_line(line, in_caps)
        if caps_text is not None:
            caps_lines.append(caps_text)
        if path_line is not None:
            device_paths.extend(_device_paths(path_line))

    return device_paths, caps_lines


def _dedup_formats(caps_lines: Sequence[str]) -> tuple[VideoFormat, ...]:
    """Parse every caps line and drop duplicate (format, geometry, fps) entries."""
    formats: list[VideoFormat] = []
    seen: set[tuple[str, int, int, float]] = set()
    for caps_line in caps_lines:
        for parsed in _parse_caps_line(caps_line):
            key = (parsed.pixel_format, parsed.width, parsed.height, parsed.fps)
            if key in seen:
                continue
            seen.add(key)
            formats.append(parsed)
    return tuple(formats)


def _parse_device_monitor_output(output: str, node_path: str) -> tuple[VideoFormat, ...]:
    """Find the block matching ``node_path`` and return its parsed formats.

    Splits ``output`` on "Device found:" headers, then for each block checks
    whether any of its device-path properties resolve to ``node_path`` (see
    :func:`_parse_device_block`, :func:`_device_paths`). The first matching
    block's caps lines are parsed and de-duplicated (:func:`_dedup_formats`).
    Returns an empty tuple when no block matches.
    """
    blocks = _DEVICE_BLOCK_SPLIT_RE.split(output)[1:]
    target = _normalize_path(node_path)

    for block in blocks:
        device_paths, caps_lines = _parse_device_block(block)
        if not any(_normalize_path(path) == target for path in device_paths):
            continue
        return _dedup_formats(caps_lines)

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
