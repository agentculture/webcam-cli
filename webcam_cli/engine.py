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
shell out. The builders stay pure even while enforcing "the elements I am
about to emit must exist on this host": they take an already-detected
:class:`Capability` as a keyword argument rather than detecting one
themselves (see :func:`require_container_elements`).

Every builder emits a **contained** stream by default: a Matroska container
is the last element before the caller's sink, so the artifact carries its own
geometry, pixel format, sample rate and frame rate. Before issue #5 the two
single-medium builders linked source caps straight to the sink, which shipped
headerless byte streams that a consumer could not decode without the original
command line. ``mux=False`` opts out, and exists for the two callers that
genuinely must not have a container appended — see the builders' docstrings.
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

# Core elements gate Capability.available: gst-launch-1.0 itself, the two
# capture sources this module knows how to open, and the container every
# pipeline shape it builds now terminates in.
#
# ``matroskamux``'s membership is now literally true of all three shapes —
# video, audio and muxed A-V each end in it (see the pipeline builders). It
# was *not* true when this list was written: the two single-medium builders
# linked source caps straight to the caller's sink and emitted headerless
# byte streams (issue #5), so the comment that used to sit here justified
# matroskamux with a claim only ``build_av_pipeline`` honoured. Fixing the
# builders made the claim true rather than aspirational.
#
# ``v4l2src``/``alsasrc`` are a *weaker* claim and always were: an audio-only
# capture never instantiates v4l2src, and a video-only capture never
# instantiates alsasrc. They are core because a host missing either can only
# do half of what this tool advertises, and saying so up front beats
# discovering it at the first failing capture — not because every shape needs
# both.
_CORE_ELEMENTS: tuple[str, ...] = ("v4l2src", "alsasrc", "matroskamux")

# Optional elements: surfaced in Capability.plugins so a caller can branch on
# them (e.g. this reference host lacks x264enc, so H.264/MP4 encoding is off
# the menu here and VP8+Opus-in-Matroska/WebM is the viable encoded path —
# see the module docstring in tests/test_engine.py and the build brief).
# Missing optional elements never make Capability.available False.
#
# The per-shape container elements (``jpegparse``, ``videoconvert``/``vp8enc``,
# ``audioconvert``/``opusenc``) live here on purpose: each is required only by
# the shape that emits it. A host that only ever captures MJPG needs
# jpegparse and matroskamux and genuinely does not need vp8enc or opusenc, so
# making them core would refuse a capture that host can perform perfectly
# well. They are instead required *conditionally*, at the moment a builder
# decides to emit them — see :func:`require_container_elements`.
_OPTIONAL_ELEMENTS: tuple[str, ...] = (
    "pulsesrc",
    "jpegparse",
    "jpegenc",
    "videoconvert",
    "vp8enc",
    "theoraenc",
    "audioconvert",
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

# Debian/Ubuntu package names for the two GStreamer plugin sets this module's
# pipeline elements are spread across. They are constants rather than inline
# literals because each one is the answer for *several* different elements
# (see _ELEMENT_PACKAGES) as well as for the blanket install hint below, and a
# package name that drifts between those answers sends a caller a shell command
# that does not fix their host. ``gstreamer1.0-tools`` and
# ``gstreamer1.0-plugins-bad`` stay inline: neither carries an element this
# module emits, so they are named only in the blanket hint and have nothing to
# drift against.
GST_PLUGINS_GOOD_PACKAGE = "gstreamer1.0-plugins-good"
GST_PLUGINS_BASE_PACKAGE = "gstreamer1.0-plugins-base"

_INSTALL_HINT = (
    "install GStreamer with the tools plus good/base plugin sets, e.g. "
    f"'sudo apt install gstreamer1.0-tools {GST_PLUGINS_GOOD_PACKAGE} "
    f"{GST_PLUGINS_BASE_PACKAGE} gstreamer1.0-plugins-bad'"
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


def _gst_inspect_supports_exists(gst_inspect: str) -> bool:
    """Detect, once per :func:`detect` call, whether ``--exists`` is available.

    Runs ``gst-inspect-1.0 --help`` and checks its stdout for the
    ``--exists`` option. ``--help`` was strace-verified (during this fix, on
    a live gst-inspect-1.0 1.24.2) to open zero ``/dev/video*`` / ``/dev/snd``
    nodes, so this check is itself safe to run unconditionally from a
    no-flag dry-run — unlike the plain per-element probe it exists to avoid
    (see :func:`_element_present`).

    Deliberately probed once and the result threaded through every
    per-element call in :func:`detect`, not re-checked per element — that
    would be 13 extra subprocess spawns for a fact that cannot change
    mid-sweep. Any failure to even run ``--help``, or a non-zero exit, is
    treated as "unsupported" so callers take the safe (if side-effecting)
    fallback path rather than assume a flag that might not exist.
    """
    try:
        result = subprocess.run(
            [gst_inspect, "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_S,
            text=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return "--exists" in (result.stdout or "")


def _element_present(gst_inspect: str, element: str, use_exists: bool) -> bool:
    """Probe whether one GStreamer element/plugin is present.

    Two forms, selected by ``use_exists`` (see :func:`_gst_inspect_supports_exists`):

    * ``gst-inspect-1.0 --exists <element>`` (preferred): exit 0 when
      present, exit 1 when absent, no stdout/stderr detail either way.
    * ``gst-inspect-1.0 <element>`` (fallback, plain form): exit 0 with
      details on stdout when present; a non-zero exit (255 on this host)
      with "No such element or plugin '<name>'" on stderr when absent.

    **Why ``--exists`` is preferred is not style — it is the fix for a
    device-open bug.** The plain form's introspection of ``v4l2src`` opens
    *every* ``/dev/video*`` node on the host as a side effect (strace-verified
    during this fix: probing plain ``v4l2src`` alone opened
    ``/dev/video0`` through ``/dev/video3``, all four nodes, including ones
    out of scope for this iteration). Because :func:`detect` runs
    unconditionally from ``record``'s no-flag dry-run path, that silently
    violated the "a no-flag dry-run opens nothing" consent guarantee this
    repo treats as its single most important rule. ``--exists`` was
    independently strace-verified, across all elements this module probes,
    to open zero ``/dev/video*`` / ``/dev/snd`` nodes. The plain-form
    fallback below still opens devices when it runs — it exists only for a
    ``gst-inspect-1.0`` old enough to predate ``--exists``, a genuinely rare
    case worth degrading for, not the common path.

    Any failure to even run the probe is treated as absent, never a crash.
    """
    argv = [gst_inspect, "--exists", element] if use_exists else [gst_inspect, element]
    try:
        result = subprocess.run(
            argv,
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
        use_exists = _gst_inspect_supports_exists(gst_inspect)
        plugins = {
            element: _element_present(gst_inspect, element, use_exists) for element in _ALL_ELEMENTS
        }
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


# --- container tails -------------------------------------------------------------
#
# Every pipeline this module builds ends in a Matroska container, so the
# artifact describes itself: geometry, pixel format, frame rate and sample
# rate travel *inside* the file instead of only in the `--json` payload of the
# command that produced it. Which elements get us there depends on what the
# source hands over, and the three routes are deliberately different:
#
# * **MJPG video** -> ``jpegparse ! matroskamux``. The camera already did the
#   compression in hardware; jpegparse only frames the byte stream into
#   per-picture buffers matroskamux can index. Nothing is decoded and nothing
#   is re-encoded, so this route is bit-exact on the pixel data and costs
#   almost no CPU. Re-encoding a hardware JPEG to VP8 would be a pointless
#   generation loss.
# * **raw video** (YUYV and friends) -> ``videoconvert ! vp8enc ! matroskamux``.
#   Matroska *can* carry raw YUY2, but at 640x480x15fps that is 9 MB/s of
#   undecodable-in-practice bulk; VP8 is the encoded path this project already
#   settled on (x264enc is absent on the reference host, so H.264 is not on
#   the menu). ``videoconvert`` bridges the source's pixel layout to one
#   vp8enc accepts.
# * **audio** -> ``audioconvert ! opusenc ! matroskamux``. Opus is the audio
#   half of the same settled VP8+Opus-in-Matroska decision.
#
# None of these elements is core (see _OPTIONAL_ELEMENTS): each is needed only
# by the route that emits it. require_container_elements() is what turns
# "this route needs vp8enc" into a typed, hinted exit-2 error instead of a
# silent degradation to some other codec or back to a headerless stream.

#: ``deadline=1`` puts vp8enc in realtime mode. This is not a micro-
#: optimisation: the source is a live camera whose frames arrive on a wall
#: clock and cannot be re-fetched, so an encoder slower than the sensor makes
#: the pipeline shed frames rather than take longer. Measured on the reference
#: host (C270, 640x480@15, 30 frames): 0.48 s CPU at the default deadline
#: versus 0.09 s at ``deadline=1``. ``webcam stream`` already used the same
#: setting for the same reason.
_VP8ENC_STAGE: tuple[str, ...] = ("vp8enc", "deadline=1")

#: Sample rates ``opusenc`` accepts, read off its own sink-pad caps on the
#: reference host (GStreamer 1.24.2). A request outside this set could only be
#: honoured by inserting ``audioresample``, which would record at a *different*
#: rate than the one the caller asked for and than the ``--json`` payload
#: reports — the silent substitution Q6 forbids. An unsupported rate is
#: therefore a typed user error naming the set, never a quiet resample.
OPUS_SAMPLE_RATES: tuple[int, ...] = (8000, 12000, 16000, 24000, 48000)

#: Channel ceiling for Opus *inside Matroska*: matroskamux's ``audio/x-opus``
#: sink caps stop at 8, below opusenc's own limit of 255. The container is the
#: binding constraint, so it is the one reported.
OPUS_MAX_CHANNELS = 8

CONTAINER_ELEMENT = "matroskamux"


def video_container_elements(fmt: VideoFormat) -> tuple[str, ...]:
    """Every element :func:`build_video_pipeline` emits after the source caps.

    Public so a caller can check host support for a *planned* format before
    committing to it, and so the capability requirement and the argv can never
    disagree — both are derived from this one function.
    """
    if fmt.pixel_format == "MJPG":
        return ("jpegparse", CONTAINER_ELEMENT)
    return ("videoconvert", _VP8ENC_STAGE[0], CONTAINER_ELEMENT)


def audio_container_elements() -> tuple[str, ...]:
    """Every element :func:`build_audio_pipeline` emits after the source caps."""
    return ("audioconvert", "opusenc", CONTAINER_ELEMENT)


#: Which Debian/Ubuntu package ships each element the container stages emit.
#: Module-level because it is pure constant data — rebuilding it per call bought
#: nothing — and because keeping it beside CONTAINER_ELEMENT and the two
#: *_container_elements() functions makes the three views of the same element
#: set (what we emit, what it costs to install, what the argv says) reviewable
#: side by side. An element absent from this map falls back to _INSTALL_HINT
#: rather than naming a package we are not sure about.
_ELEMENT_PACKAGES = {
    "jpegparse": GST_PLUGINS_GOOD_PACKAGE,
    "videoconvert": GST_PLUGINS_BASE_PACKAGE,
    "vp8enc": GST_PLUGINS_GOOD_PACKAGE,
    "audioconvert": GST_PLUGINS_BASE_PACKAGE,
    "opusenc": GST_PLUGINS_BASE_PACKAGE,
    CONTAINER_ELEMENT: GST_PLUGINS_GOOD_PACKAGE,
}


def _container_hint(missing: Sequence[str]) -> str:
    """An install hint naming the Debian/Ubuntu package each missing element ships in."""
    needed = sorted(
        {_ELEMENT_PACKAGES[element] for element in missing if element in _ELEMENT_PACKAGES}
    )
    if not needed:
        return _INSTALL_HINT
    return (
        f"install {' and '.join(needed)} — this capture cannot be written to a "
        "container without it, and this tool will not fall back to an "
        "undecodable headerless stream or silently pick a different codec"
    )


def require_container_elements(elements: Sequence[str], cap: Capability | None) -> None:
    """Raise a typed exit-2 error if ``cap`` says this host lacks any of ``elements``.

    ``cap=None`` means "no host information available" and skips the check —
    that is the *construction-only* mode the pure unit tests and the dry-run
    preview of an already-validated plan use, not a licence to guess. Callers
    that are about to actually run the pipeline pass the
    :class:`Capability` they already detected.

    Never degrades: a missing element is an error naming the element and the
    package that carries it. There is deliberately no "well, try the next best
    codec" path (settled design decision Q6), because a caller who asked for
    MJPG-in-Matroska and silently got VP8 — or, worse, the headerless raw
    stream this replaced — has been lied to about what it recorded.
    """
    if cap is None:
        return
    missing = [element for element in elements if not cap.plugins.get(element, False)]
    if not missing:
        return
    raise CliError(
        EXIT_ENV_ERROR,
        f"required GStreamer element(s) missing for this capture: {', '.join(missing)}",
        remediation=_container_hint(missing),
    )


def _require_opus_compatible(fmt: AudioFormat) -> None:
    """Reject an audio format Opus-in-Matroska cannot carry, before building anything."""
    if fmt.rate not in OPUS_SAMPLE_RATES:
        supported = ", ".join(str(rate) for rate in OPUS_SAMPLE_RATES)
        raise CliError(
            EXIT_USER_ERROR,
            f"sample rate {fmt.rate} Hz cannot be encoded as Opus for the Matroska container",
            remediation=(
                f"pass one of the rates Opus supports: {supported} (48000 is the default). "
                "this tool will not resample for you — that would record at a different "
                "rate than the one you asked for"
            ),
        )
    if fmt.channels > OPUS_MAX_CHANNELS:
        raise CliError(
            EXIT_USER_ERROR,
            f"{fmt.channels} channels exceeds the {OPUS_MAX_CHANNELS} that Opus-in-Matroska "
            "can carry",
            remediation=f"pass --channels between 1 and {OPUS_MAX_CHANNELS}",
        )


def _video_container_stages(fmt: VideoFormat) -> list[tuple[str, ...]]:
    """The container tail for ``fmt`` as one token group per element."""
    if fmt.pixel_format == "MJPG":
        return [("jpegparse",), (CONTAINER_ELEMENT,)]
    return [("videoconvert",), _VP8ENC_STAGE, (CONTAINER_ELEMENT,)]


def _audio_container_stages() -> list[tuple[str, ...]]:
    return [("audioconvert",), ("opusenc",), (CONTAINER_ELEMENT,)]


def _tail_tokens(stages: Sequence[Sequence[str]], sink: str) -> list[str]:
    """Interleave ``!`` between the container stages and the caller's sink."""
    tokens: list[str] = []
    for stage in stages:
        tokens.extend(stage)
        tokens.append("!")
    tokens.extend(_sink_tokens(sink))
    return tokens


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


def build_video_pipeline(
    node_path: str,
    fmt: VideoFormat,
    sink: str,
    *,
    mux: bool = True,
    cap: Capability | None = None,
) -> list[str]:
    """Build a ``gst-launch-1.0`` argv for a single video (``v4l2src``) stream.

    The stream is containerized by default: MJPG is framed with ``jpegparse``
    and stored losslessly, anything raw is encoded with ``vp8enc``, and either
    way the last element before ``sink`` is ``matroskamux`` — see the
    "container tails" section above for why each route is what it is.

    Args:
        mux: ``False`` appends nothing between the source caps and ``sink``,
            handing the whole downstream chain to the caller. Exactly two
            callers want this and both have a real reason: ``webcam stream``
            passes its own ``… ! matroskamux streamable=true ! tcpserversink``
            chain as ``sink`` (a second container here would nest one inside
            the other), and ``webcam record``'s warm-up phase sinks to
            ``fakesink`` to burn auto-exposure frames it then throws away —
            encoding those to VP8 would spend real CPU on pixels nothing ever
            reads. It is not an escape hatch for "the encoder is missing".
        cap: Already-detected host capability. When given, the elements this
            call is about to emit are checked against it and a missing one is
            a typed exit-2 error (:func:`require_container_elements`); when
            ``None``, construction proceeds unchecked — the pure mode tests
            and on-paper previews use. Never triggers detection itself, so
            this function still shells out to nothing.
    """
    _validate_video_format(fmt)
    stages: list[tuple[str, ...]] = []
    if mux:
        require_container_elements(video_container_elements(fmt), cap)
        stages = _video_container_stages(fmt)
    return [
        GST_LAUNCH,
        "v4l2src",
        f"device={node_path}",
        "!",
        _video_caps_string(fmt),
        "!",
        *_tail_tokens(stages, sink),
    ]


def build_audio_pipeline(
    alsa_address: str,
    fmt: AudioFormat,
    sink: str,
    *,
    mux: bool = True,
    cap: Capability | None = None,
) -> list[str]:
    """Build a ``gst-launch-1.0`` argv for a single audio (``alsasrc``) stream.

    ``alsa_address`` must be a direct ALSA ``hw:`` address (e.g.
    ``hw:CARD=C270,DEV=0``) — see ``arecord -l``. PulseAudio addressing is
    out of scope: on the reference host the target device's PipeWire profile
    is off, so direct ALSA is the only path that works.

    Containerized by default (``audioconvert ! opusenc ! matroskamux``), so
    the artifact records its own sample rate instead of being a headerless
    PCM blob. ``mux`` and ``cap`` mean exactly what they do on
    :func:`build_video_pipeline`; with ``mux=True`` the requested rate and
    channel count must additionally be ones Opus-in-Matroska can carry, which
    is a typed user error rather than a silent resample.
    """
    _validate_alsa_address(alsa_address)
    _validate_audio_format(fmt)
    stages: list[tuple[str, ...]] = []
    if mux:
        _require_opus_compatible(fmt)
        require_container_elements(audio_container_elements(), cap)
        stages = _audio_container_stages()
    return [
        GST_LAUNCH,
        "alsasrc",
        f"device={alsa_address}",
        "!",
        _audio_caps_string(fmt),
        "!",
        *_tail_tokens(stages, sink),
    ]


def build_av_pipeline(
    node_path: str,
    fmt: VideoFormat,
    alsa_address: str,
    afmt: AudioFormat,
    sink: str,
    *,
    cap: Capability | None = None,
) -> list[str]:
    """Build a ``gst-launch-1.0`` argv muxing video + audio into one sink.

    Uses the standard gst-launch named-pad idiom: both source branches link
    into ``mux.`` (a request pad on the element named ``mux``), and
    ``matroskamux name=mux`` is declared once, feeding ``sink``. Order is
    sources-then-muxer, which is the conventional and most portable spelling
    of this idiom, but is not order-sensitive to ``gst_parse_launch``.

    Unlike the single-medium builders this shape has **no per-branch codec
    tail**: matroskamux accepts ``image/jpeg`` and raw ``video/x-raw`` and
    ``audio/x-raw`` on its request pads directly, so both branches are stored
    as the device delivers them. That is the shape that has always worked and
    it is left alone deliberately — an encoded A-V variant needs per-branch
    pipeline support the ``stream``/``record`` surfaces do not currently
    expose. There is no ``mux`` parameter here because a muxer is the whole
    point of this builder: without it there is no single sink to feed.
    """
    _validate_video_format(fmt)
    _validate_audio_format(afmt)
    _validate_alsa_address(alsa_address)
    require_container_elements((CONTAINER_ELEMENT,), cap)
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
