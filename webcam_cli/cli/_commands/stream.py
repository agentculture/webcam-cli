"""``webcam stream`` — live video / audio / muxed A-V attachment points.

This module owns one question: *how does a second process get live frames or
samples off an attached capture device, and what must it be told to do so.* It
composes the wave-1 modules and adds no capture logic of its own:
:mod:`webcam_cli.devices` resolves the stable identity, :mod:`webcam_cli.access`
produces the typed absent/forbidden/busy errors, :mod:`webcam_cli.engine`
detects capability and builds the ``gst-launch-1.0`` argv, and
:mod:`webcam_cli.activation` records that the hardware was energized.

Three sub-verbs, one shape each::

    webcam stream video <device>   # camera only
    webcam stream audio <device>   # that device's own microphone only
    webcam stream av <device>      # both, muxed into one container

What each invocation touches
----------------------------
This is the first thing an agent needs to know and it is stated in the help
text of every sub-verb, because "did that open the camera?" must be answerable
from the surface alone:

* **default (dry-run)** — touches *nothing*. Resolves the device from the
  filesystem, checks the request structurally, prints the plan it would run.
  No device is opened, no engine binary is executed, nothing is logged.
* **``--probe``** — opts into real format enumeration via
  :func:`webcam_cli.engine.probe_formats`, which shells out to
  ``gst-device-monitor-1.0`` and briefly opens the camera. Because that
  energizes the sensor it is written to the activation log exactly like a
  capture.
* **``--apply``** — opens the device and streams. Implies ``--probe`` (a
  stream must negotiate against the enumerated set), applies warm-up, emits
  the attachment payload, then holds the device until it is stopped.

Attachment mechanism
--------------------
``--apply`` serves the stream from a **GStreamer ``tcpserversink`` bound to
127.0.0.1**, with the media wrapped in a **Matroska** container. Both halves of
that choice are about the consumer:

* the byte stream is *self-describing* — caps travel inside the container, so a
  consumer can attach with ``tcpclientsrc ! matroskademux ! …`` (or plain
  ``decodebin``) and does not have to reconstruct caps by hand, which is the
  failure mode of a raw shared-memory sink where a caps mismatch yields silent
  garbage;
* ``tcpserversink`` is in the set of elements
  :class:`webcam_cli.engine.Capability` actually probes, so its presence is
  *verified* rather than assumed — ``shmsink`` lives in plugins-bad and is not
  in the engine's probe list, so choosing it would mean guessing.

``matroskamux`` is one of the engine's *required* core elements, so when
``require_engine()`` succeeds the container is guaranteed available.

The honest cost of TCP-on-loopback is that it is reachable by any process on
this host that can connect to the port, which is why the payload states that
exposure plainly rather than implying privacy. The stream is never bound to a
non-loopback address; there is deliberately no ``--host`` flag.

Warm-up
-------
The first frames off a UVC sensor are dark while auto-exposure settles — the
brief calls grab-one-frame the single most common way a webcam tool ships a
black image. At stream start the pipeline therefore runs for a warm-up interval
*before* the attachment point is announced, and ``tcpserversink`` drops buffers
while no client is connected, so those frames are discarded rather than queued.
The default is :data:`DEFAULT_WARMUP_FRAMES` frames for video (converted through
the negotiated fps) and :data:`DEFAULT_AUDIO_WARMUP_MS` ms for audio, both
overridable (``--warmup-frames`` / ``--warmup-ms``, ``--warmup-frames 0``
disables). **The video default is provisional**: the reference host's C270
settle time has not been measured yet (that needs hardware, which is task t9's
job), so a conservative ~1 s was chosen over a guess that risks shipping dark
frames.

Unbounded by design
-------------------
A stream has no duration cap and no ``--duration`` flag: it runs until the
process is stopped or the pipeline exits. Oversight comes from the activation
log, not from a timer. (The sibling ``record`` verb is the opposite — bounded by
construction. The two must not be blurred.)

Zero runtime dependencies: standard library only.
"""

from __future__ import annotations

import argparse
import re
import shlex
import signal
import socket
import subprocess  # nosec B404 - shelling out to gst-launch-1.0 is the engine posture
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType
from typing import IO

from webcam_cli import access, activation, devices, engine
from webcam_cli.cli._commands.overview import render_text
from webcam_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from webcam_cli.cli._output import emit_result

# --- constants ---------------------------------------------------------------

#: The stream is served on loopback only. Deliberately not a flag: a webcam
#: stream on a routable address is a different (and much larger) consent
#: question than this tool is entitled to answer.
STREAM_HOST = "127.0.0.1"

#: Canonical default port. Fixed rather than random so a dry-run plan is
#: concrete and reproducible; ``--port 0`` auto-picks a free port instead.
DEFAULT_PORT = 5000

#: The GStreamer sink that serves the attachment point (see module docstring).
SINK_ELEMENT = "tcpserversink"

#: Container for every stream shape, so the wire format is self-describing.
CONTAINER = "matroska"

#: Video warm-up default, in frames. Provisional: ~1 s at 30 fps, chosen
#: conservatively because the C270's auto-exposure settle time is *unmeasured*
#: (plan risk r3 — measuring it needs hardware, which is task t9). A stream is
#: unbounded, so a one-off second of warm-up costs nothing per stream, whereas
#: too-short a warm-up hands the first consumer dark frames.
DEFAULT_WARMUP_FRAMES = 30

#: Audio warm-up default, in milliseconds. Frames are meaningless for ALSA, and
#: there is no auto-exposure equivalent — this covers ring-buffer fill only, so
#: it is far shorter than the video default.
DEFAULT_AUDIO_WARMUP_MS = 200.0

#: fps assumed when a warm-up interval must be reported before any format has
#: been negotiated (an on-paper dry run). Reported as ``fps_assumed: true``.
WARMUP_FPS_ASSUMPTION = 30.0

_TERMINATE_TIMEOUT_S = 5
_OUTPUT_TAIL_BYTES = 2000

#: Encoder element required per ``--encode`` choice. Routed through
#: ``Capability.plugins``, never assumed: on the reference host ``x264enc`` is
#: absent, so H.264/MP4 is not offered at all, and VP8/Opus is the encoded path.
_ENCODER_ELEMENTS = {"vp8": "vp8enc", "opus": "opusenc"}

_ALSA_DEV_RE = re.compile(r"DEV=(\d+)")

_INSTALL_SINK_HINT = (
    "install the GStreamer TCP plugin (it ships in gstreamer1.0-plugins-base), "
    "e.g. 'sudo apt install gstreamer1.0-plugins-base', then re-run"
)

_ACTIVITY_LIGHT_NOTE = (
    "cannot be promised — a hardware activity LED is device firmware and outside this "
    "tool's control. What is promised: every activation is appended to the activation "
    "log, and no bytes go anywhere except the announced attachment point"
)


# --- request / plan model ----------------------------------------------------


@dataclass(frozen=True)
class _Context:
    """Everything the plan and the run need, resolved before either happens."""

    args: argparse.Namespace
    medium: str
    device: devices.LogicalDevice
    node: str | None
    alsa: str | None
    request: dict[str, object]
    cap: engine.Capability | None
    port: int
    port_source: str
    apply_mode: bool
    probe_mode: bool
    json_mode: bool

    @property
    def verb(self) -> str:
        return f"stream {self.medium}"

    @property
    def encode(self) -> str:
        return str(getattr(self.args, "encode", "passthrough"))

    @property
    def uri(self) -> str:
        return f"tcp://{STREAM_HOST}:{self.port}"


# --- subprocess seam ---------------------------------------------------------


@dataclass
class _StreamProcess:
    """The running pipeline child, with its output captured off our streams.

    The child's stdout **and** stderr go to an unlinked temporary file, never
    to ours: gst-launch-1.0 chats on stdout, and a raw media stream must never
    share stdout with the JSON payload. Capturing into a file rather than a
    pipe also means a long-running stream cannot deadlock by filling a pipe
    buffer nobody is draining.
    """

    proc: subprocess.Popen[bytes]
    log: IO[bytes]

    @property
    def pid(self) -> int:
        return self.proc.pid

    def poll(self) -> int | None:
        return self.proc.poll()

    def wait(self) -> int | None:
        return self.proc.wait()

    def terminate(self) -> None:
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=_TERMINATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - needs a wedged child
            self.proc.kill()
            self.proc.wait()

    def output_tail(self, limit: int = _OUTPUT_TAIL_BYTES) -> str:
        try:
            self.log.flush()
            size = self.log.seek(0, 2)
            self.log.seek(max(0, size - limit))
            return self.log.read().decode("utf-8", errors="replace").strip()
        except OSError:  # pragma: no cover - defensive
            return ""


def _spawn(argv: Sequence[str]) -> _StreamProcess:
    """Start the pipeline child. Replaced wholesale in tests (deviation d1)."""
    log = tempfile.TemporaryFile(mode="w+b")
    try:
        proc = subprocess.Popen(  # nosec B603 - fixed argv, no shell, built by engine
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
    except OSError as exc:
        log.close()
        raise CliError(
            EXIT_ENV_ERROR,
            f"could not start the capture pipeline: {exc}",
            remediation="check that gst-launch-1.0 is installed and executable",
        ) from exc
    return _StreamProcess(proc=proc, log=log)


def _sleep(seconds: float) -> None:
    """Warm-up wait. A seam so tests never actually sleep."""
    time.sleep(seconds)


class _Interrupted(Exception):
    """Raised by the SIGINT/SIGTERM handler so the child is reaped cleanly."""


def _supervise(proc: _StreamProcess) -> int | None:
    """Hold the stream open until the child exits or we are asked to stop.

    The CLI supervises for the stream's whole lifetime rather than detaching:
    the activation log's ``ended_at`` is only honest if somebody is still
    watching, and a detached child would outlive its own audit record.
    """
    previous: dict[int, object] = {}

    def _stop(signum: int, frame: FrameType | None) -> None:
        raise _Interrupted()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, _stop)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass
    try:
        return proc.wait()
    except (_Interrupted, KeyboardInterrupt):
        proc.terminate()
        return 0
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)  # type: ignore[arg-type]
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass


# --- activation logging ------------------------------------------------------


@contextmanager
def _logged_activation(
    *,
    device_id: str,
    verb: str,
    target: str,
    detail: dict[str, object],
) -> Iterator[activation.Activation]:
    """:func:`activation_scope`, with its raw ``OSError`` typed into a CliError.

    ``record_activation`` deliberately lets a failed write propagate as the
    underlying ``OSError`` — a silently dropped line would break the consent
    guarantee. That is the right contract for the log module and the wrong
    thing to show an agent, so it is wrapped here.

    Every ``OSError`` this sees comes from the log write, because the body
    below converts its own OS-level failures (``_spawn``) into ``CliError``
    first. ``detail`` is mutated in place by the body; the scope writes the
    same dict object on exit, so late-resolved facts (the negotiated format,
    the child pid) still land in the record.
    """
    try:
        with activation.activation_scope(
            device_id=device_id,
            verb=verb,
            target=target,
            detail=detail,
        ) as act:
            yield act
    except OSError as exc:
        path = activation.log_path()
        raise CliError(
            EXIT_ENV_ERROR,
            f"could not write the activation log at {path}: {exc}",
            remediation=(
                f"point ${activation.ENV_LOG_PATH} at a writable file, or make "
                f"{path.parent} a writable directory — every activation must be "
                "recordable before a device is opened"
            ),
        ) from exc


# --- device / request resolution ---------------------------------------------


def _sources(device: devices.LogicalDevice, medium: str) -> tuple[str | None, str | None]:
    """Pick the node and/or ALSA address this medium needs, or fail typed.

    A/V sets are not 1:1 on real hardware (a mic-only capture device, a camera
    with no mic), so this refusal is a normal outcome, not an edge case.
    """
    node: str | None = None
    alsa: str | None = None

    if medium in ("video", "av"):
        if device.capture_node is None:
            raise CliError(
                EXIT_USER_ERROR,
                f"device {device.stable_id!r} has no camera — it publishes no video node",
                remediation=(
                    "run `webcam list --json` and pick a device with a capture_node, or "
                    "use `webcam stream audio` for this microphone-only device"
                ),
            )
        node = device.capture_node

    if medium in ("audio", "av"):
        if device.audio is None:
            raise CliError(
                EXIT_USER_ERROR,
                (
                    f"device {device.stable_id!r} has no microphone — no ALSA capture card "
                    "shares its USB parent"
                ),
                remediation=(
                    "run `webcam list --json` to see which devices carry audio, or use "
                    "`webcam stream video` for this camera-only device"
                ),
            )
        alsa = device.audio.alsa_address

    return node, alsa


def _alsa_node_path(card: devices.AudioCard) -> str:
    """The ``/dev/snd`` node behind an ALSA capture address, for access checks.

    Derived at use time from the *currently enumerated* card, never persisted:
    the card index is plug-order and has already moved once on the reference
    host. Identity stays the stable id; this is only the path to open.
    """
    match = _ALSA_DEV_RE.search(card.alsa_address)
    device_index = int(match.group(1)) if match else 0
    return f"/dev/snd/pcmC{card.index}D{device_index}c"


def _positive(value: int | float | None, flag: str) -> None:
    if value is None:
        return
    if value <= 0:
        raise CliError(
            EXIT_USER_ERROR,
            f"{flag} must be greater than zero (got {value})",
            remediation=f"pass a positive {flag} value, or omit it to let the device decide",
        )


def _request(args: argparse.Namespace, medium: str) -> dict[str, object]:
    """The request exactly as asked, structurally validated, nothing invented.

    Unspecified video fields stay ``None`` rather than being filled with a
    hidden default — inventing a resolution would be the silent assumption the
    brief forbids. Audio has no capability probe to negotiate against, so its
    defaults are explicit and reported.
    """
    video: dict[str, object] | None = None
    audio: dict[str, object] | None = None

    if medium in ("video", "av"):
        _positive(args.width, "--width")
        _positive(args.height, "--height")
        _positive(args.fps, "--fps")
        pixel_format = args.format.upper() if args.format else None
        video = {
            "pixel_format": pixel_format,
            "width": args.width,
            "height": args.height,
            "fps": float(args.fps) if args.fps is not None else None,
        }

    if medium in ("audio", "av"):
        _positive(args.rate, "--rate")
        _positive(args.channels, "--channels")
        audio = {"rate": args.rate, "channels": args.channels}

    return {"video": video, "audio": audio}


def _capability(args: argparse.Namespace) -> engine.Capability:
    """Require the engine, then gate every element this run will actually use."""
    cap = engine.require_engine()

    if not cap.plugins.get(SINK_ELEMENT, False):
        raise CliError(
            EXIT_ENV_ERROR,
            f"the attachment sink element {SINK_ELEMENT} is not installed",
            remediation=_INSTALL_SINK_HINT,
        )

    encode = str(getattr(args, "encode", "passthrough"))
    element = _ENCODER_ELEMENTS.get(encode)
    if element is not None and not cap.plugins.get(element, False):
        raise CliError(
            EXIT_ENV_ERROR,
            f"--encode {encode} needs the GStreamer element {element}, which is not installed",
            remediation=(
                f"install the plugin set carrying {element} (gstreamer1.0-plugins-good "
                "for vp8enc, gstreamer1.0-plugins-base for opusenc), or drop --encode "
                "to stream the device's own format through untouched"
            ),
        )
    return cap


# --- port selection ----------------------------------------------------------


def _free_port() -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((STREAM_HOST, 0))
            return int(sock.getsockname()[1])
    except OSError as exc:  # pragma: no cover - loopback bind failing is pathological
        raise CliError(
            EXIT_ENV_ERROR,
            f"could not reserve a loopback port for the attachment point: {exc}",
            remediation="pass an explicit --port <N>",
        ) from exc


def _require_free_port(port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((STREAM_HOST, port))
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"attachment port {port} on {STREAM_HOST} is already in use",
            remediation=(
                "pass --port <N> to serve on another port, or --port 0 to auto-pick a "
                "free one; a stream already running from this tool is the usual holder"
            ),
        ) from exc
    finally:
        sock.close()


def _port(args: argparse.Namespace, *, apply_mode: bool) -> tuple[int, str]:
    """Resolve the attachment port.

    A dry run does not check availability: it prints a plan, and a plan does
    not reserve anything. ``--apply`` checks, so a doomed bind fails with a
    typed error naming the fix instead of dying inside gst-launch.
    """
    if args.port == 0:
        return _free_port(), "auto-picked (not reserved until --apply binds it)"
    if apply_mode:
        _require_free_port(args.port)
    return int(args.port), "explicit"


# --- negotiation -------------------------------------------------------------


def _fmt_dict(fmt: engine.VideoFormat) -> dict[str, object]:
    return {
        "pixel_format": fmt.pixel_format,
        "width": fmt.width,
        "height": fmt.height,
        "fps": fmt.fps,
    }


def _describe(fmt: engine.VideoFormat) -> str:
    return f"{fmt.pixel_format} {fmt.width}x{fmt.height}@{fmt.fps}fps"


def _requested_format(request: dict[str, object] | None) -> engine.VideoFormat | None:
    """A :class:`VideoFormat` only when all four fields were given."""
    if request is None:
        return None
    if any(request[key] is None for key in ("pixel_format", "width", "height", "fps")):
        return None
    return engine.VideoFormat(
        pixel_format=str(request["pixel_format"]),
        width=int(request["width"]),  # type: ignore[arg-type]
        height=int(request["height"]),  # type: ignore[arg-type]
        fps=float(request["fps"]),  # type: ignore[arg-type]
    )


def _constrained(request: dict[str, object] | None) -> bool:
    return request is not None and any(value is not None for value in request.values())


def _matching(
    available: Sequence[engine.VideoFormat],
    request: dict[str, object],
) -> list[engine.VideoFormat]:
    """Enumerated formats consistent with every field the caller *did* give.

    This is a constrained selection, not a fallback: everything returned
    satisfies the request. What is never done is handing back a format that
    contradicts an explicit field.
    """
    fields = ("pixel_format", "width", "height")
    wanted_fps = request["fps"]
    matches: list[engine.VideoFormat] = []
    for fmt in available:
        if any(request[key] is not None and getattr(fmt, key) != request[key] for key in fields):
            continue
        if wanted_fps is not None and float(fmt.fps) != float(wanted_fps):  # type: ignore[arg-type]
            continue
        matches.append(fmt)
    return matches


_NEGOTIATION_POLICY = (
    "an unsatisfiable request is a typed user error; this tool never substitutes a "
    "format the caller did not ask for"
)


def _audio_negotiation(request: dict[str, object]) -> dict[str, object]:
    return {
        "status": "unvalidated",
        "validated_against": (
            "nothing — the engine exposes no ALSA capability probe, so rate/channels are "
            "applied as an exact caps filter: an unsupported pair fails loudly when the "
            "pipeline starts rather than being silently substituted"
        ),
        "requested": dict(request),
        "negotiated": None,
        "planned": dict(request),
        "available": None,
        "exact_match": None,
        "constrained_selection": False,
        "silent_fallback": False,
        "policy": _NEGOTIATION_POLICY,
    }


def _paper_negotiation(
    request: dict[str, object],
    requested: engine.VideoFormat | None,
) -> dict[str, object]:
    if requested is not None:
        status = "unvalidated"
        validated = (
            "nothing — the request was checked structurally only. Pass --probe to "
            "validate it against the device's enumerated formats (that opens the camera "
            "and is written to the activation log)"
        )
    else:
        status = "deferred"
        validated = (
            "nothing — a concrete format has to come from the device's enumerated set. "
            "Pass --probe (or --apply) to negotiate it; either one opens the camera and "
            "is written to the activation log"
        )
    return {
        "status": status,
        "validated_against": validated,
        "requested": dict(request) if _constrained(request) else None,
        "negotiated": None,
        "planned": _fmt_dict(requested) if requested is not None else None,
        "available": None,
        "exact_match": None,
        "constrained_selection": False,
        "silent_fallback": False,
        "policy": _NEGOTIATION_POLICY,
    }


def _probe_negotiation(
    node: str,
    request: dict[str, object],
) -> tuple[dict[str, object], engine.VideoFormat]:
    available = engine.probe_formats(node)
    requested = _requested_format(request)
    constrained = _constrained(request)

    if requested is not None:
        # Raises the typed user error, naming the alternatives, when the exact
        # combination is not enumerated. Never falls back.
        granted = engine.validate_negotiation(requested, available)
        exact, narrowed = True, False
    else:
        pool = _matching(available, request) if constrained else list(available)
        if constrained and not pool:
            alternatives = ", ".join(_describe(fmt) for fmt in available)
            raise CliError(
                EXIT_USER_ERROR,
                "no format this device enumerates matches the requested constraints "
                f"({_describe_request(request)})",
                remediation=f"choose from the enumerated formats: {alternatives}",
            )
        granted = engine.validate_negotiation(None, pool)
        exact, narrowed = False, constrained

    return (
        {
            "status": "granted",
            "validated_against": "the device's enumerated formats (gst-device-monitor-1.0)",
            "requested": dict(request) if constrained else None,
            "negotiated": _fmt_dict(granted),
            "planned": _fmt_dict(granted),
            "available": [_fmt_dict(fmt) for fmt in available],
            "exact_match": exact,
            "constrained_selection": narrowed,
            "silent_fallback": False,
            "policy": _NEGOTIATION_POLICY,
        },
        granted,
    )


def _describe_request(request: dict[str, object]) -> str:
    parts = [f"{key}={value}" for key, value in request.items() if value is not None]
    return ", ".join(parts) if parts else "no constraints"


def _negotiate(ctx: _Context) -> tuple[dict[str, object], engine.VideoFormat | None]:
    if ctx.medium == "audio":
        audio = ctx.request["audio"]
        assert isinstance(audio, dict)
        return _audio_negotiation(audio), None

    video = ctx.request["video"]
    assert isinstance(video, dict)
    if not ctx.probe_mode:
        return _paper_negotiation(video, _requested_format(video)), _requested_format(video)

    assert ctx.node is not None
    return _probe_negotiation(ctx.node, video)


# --- pipeline construction (composed from engine, never re-implemented) ------


def _audio_format(ctx: _Context) -> engine.AudioFormat:
    audio = ctx.request["audio"]
    assert isinstance(audio, dict)
    return engine.AudioFormat(rate=int(audio["rate"]), channels=int(audio["channels"]))


def _require_jpeg_decoder(ctx: _Context) -> None:
    """Gate ``jpegdec`` through the probed ``jpegenc``.

    ``Capability.plugins`` probes ``jpegenc`` but not ``jpegdec``; both ship in
    the same plugins-good ``jpeg`` plugin, so the probed encoder is used as the
    documented proxy for the pair rather than assuming the decoder exists.
    """
    if ctx.cap is None or ctx.cap.plugins.get("jpegenc", False):
        return
    raise CliError(
        EXIT_ENV_ERROR,
        "encoding an MJPEG source to VP8 needs the jpegdec element, which is not installed",
        remediation=(
            "install gstreamer1.0-plugins-good (it carries the 'jpeg' plugin: jpegdec and "
            "jpegenc), or drop --encode to stream MJPEG through untouched"
        ),
    )


def _sink_chain(ctx: _Context, fmt: engine.VideoFormat | None) -> str:
    """The element chain appended after the source caps.

    Everything downstream of the source is expressed here as the engine's
    free-form ``sink`` argument, which is how encoding and muxing are added
    without re-implementing any pipeline construction.
    """
    served = f"{SINK_ELEMENT} host={STREAM_HOST} port={ctx.port} sync=false"

    if ctx.medium == "av":
        # engine.build_av_pipeline already supplies matroskamux.
        return f"queue ! {served}"

    muxed = f"matroskamux streamable=true ! {served}"

    if ctx.medium == "video":
        if ctx.encode == "vp8":
            decode = ""
            if fmt is not None and fmt.pixel_format == "MJPG":
                _require_jpeg_decoder(ctx)
                decode = "jpegdec ! "
            return f"queue ! {decode}videoconvert ! vp8enc deadline=1 ! {muxed}"
        return f"queue ! {muxed}"

    if ctx.encode == "opus":
        return f"queue ! audioconvert ! audioresample ! opusenc ! {muxed}"
    return f"queue ! audioconvert ! {muxed}"


def _pipeline(ctx: _Context, fmt: engine.VideoFormat | None) -> list[str] | None:
    """Build the ``gst-launch-1.0`` argv, or ``None`` if the format is unknown."""
    if ctx.medium in ("video", "av") and fmt is None:
        return None
    sink = _sink_chain(ctx, fmt)
    if ctx.medium == "video":
        assert ctx.node is not None and fmt is not None
        return engine.build_video_pipeline(ctx.node, fmt, sink)
    if ctx.medium == "audio":
        assert ctx.alsa is not None
        return engine.build_audio_pipeline(ctx.alsa, _audio_format(ctx), sink)
    assert ctx.node is not None and ctx.alsa is not None and fmt is not None
    return engine.build_av_pipeline(ctx.node, fmt, ctx.alsa, _audio_format(ctx), sink)


def _caps_from(argv: Sequence[str] | None, prefixes: tuple[str, ...]) -> str | None:
    """Read a caps string back out of the built argv.

    The announced caps are taken from the pipeline that will actually run, so
    what the payload promises can never drift from what the engine built.
    """
    if argv is None:
        return None
    return next((token for token in argv if token.startswith(prefixes)), None)


# --- warm-up -----------------------------------------------------------------

_WARMUP_MECHANISM = (
    "the pipeline runs with no consumer attached for the warm-up interval and the "
    f"attachment point is announced only after it elapses; {SINK_ELEMENT} drops buffers "
    "while no client is connected, so those frames are discarded rather than queued"
)

_WARMUP_CAVEAT = (
    "a consumer that connects before the announcement (by guessing the port, say) can "
    "still observe pre-settle frames — the guarantee is the announcement ordering, not "
    "an enforced gate"
)

_WARMUP_OVERRIDES = (
    "--warmup-frames <N> (video/av, converted through the negotiated fps)",
    "--warmup-ms <MS> (any medium, exact interval)",
    "--warmup-frames 0 (disable warm-up entirely)",
)


def _warmup(ctx: _Context, fmt: engine.VideoFormat | None) -> dict[str, object]:
    explicit_ms = getattr(ctx.args, "warmup_ms", None)
    frames_flag = getattr(ctx.args, "warmup_frames", None)

    if ctx.medium == "audio":
        ms = float(explicit_ms) if explicit_ms is not None else DEFAULT_AUDIO_WARMUP_MS
        source = "--warmup-ms" if explicit_ms is not None else "default (audio ring fill)"
        return _warmup_dict(frames=None, ms=ms, fps=None, assumed=False, source=source)

    fps = fmt.fps if fmt is not None else WARMUP_FPS_ASSUMPTION
    if explicit_ms is not None:
        ms = float(explicit_ms)
        frames = round(ms / 1000.0 * fps)
        source = "--warmup-ms"
    else:
        frames = DEFAULT_WARMUP_FRAMES if frames_flag is None else int(frames_flag)
        ms = frames / fps * 1000.0
        source = "--warmup-frames" if frames_flag is not None else "default (--warmup-frames)"
    return _warmup_dict(
        frames=frames,
        ms=ms,
        fps=fps,
        assumed=fmt is None,
        source=source,
    )


def _warmup_dict(
    *,
    frames: int | None,
    ms: float,
    fps: float | None,
    assumed: bool,
    source: str,
) -> dict[str, object]:
    return {
        "frames": frames,
        "ms": round(float(ms), 3),
        "fps_basis": fps,
        "fps_assumed": assumed,
        "source": source,
        "applied": ms > 0,
        "mechanism": _WARMUP_MECHANISM,
        "caveat": _WARMUP_CAVEAT,
        "provisional": (
            "the video default is unmeasured on this hardware — it is a conservative "
            "~1s at 30fps pending an on-host measurement of the C270's auto-exposure "
            "settle time"
        ),
        "overrides": list(_WARMUP_OVERRIDES),
    }


def _warm_up(proc: _StreamProcess, warmup: dict[str, object]) -> None:
    """Wait out the warm-up interval, then assert the pipeline is still alive."""
    seconds = float(warmup["ms"]) / 1000.0  # type: ignore[arg-type]
    if seconds > 0:
        _sleep(seconds)
    status = proc.poll()
    if status is None:
        return
    tail = proc.output_tail()
    detail = f": {tail}" if tail else ""
    raise CliError(
        EXIT_ENV_ERROR,
        f"the capture pipeline exited during warm-up with status {status}{detail}",
        remediation=(
            "the requested format or audio parameters may not be accepted by the device, "
            "or another client took it — re-run with --probe to see what the device "
            "enumerates, and check `webcam list --json` for access state"
        ),
    )


# --- access ------------------------------------------------------------------


def _access(ctx: _Context) -> dict[str, object]:
    """Enforce access when applying, report it when probing.

    ``--apply`` must fail fast and typed (absent vs forbidden vs busy, each
    with its own fix); ``--probe`` is a reporting path, so it shows a bad
    device as bad and still exits 0.
    """
    state: dict[str, object] = {"video": None, "audio": None}

    if ctx.node is not None:
        if ctx.apply_mode:
            access.require_access(ctx.node, "video")
            state["video"] = "ok"
        else:
            state["video"] = access.check_access(ctx.node, "video").state.value

    if ctx.alsa is not None:
        assert ctx.device.audio is not None
        path = _alsa_node_path(ctx.device.audio)
        if ctx.apply_mode:
            access.require_access(path, "audio")
            state["audio"] = "ok"
        else:
            state["audio"] = access.check_access(path, "audio").state.value

    return state


# --- payload -----------------------------------------------------------------


def _video_stream_desc(
    ctx: _Context,
    fmt: engine.VideoFormat | None,
    argv: Sequence[str] | None,
) -> dict[str, object] | None:
    if ctx.medium == "audio":
        return None
    caps = _caps_from(argv, ("image/", "video/x-raw"))
    encoding = "vp8" if ctx.encode == "vp8" else "passthrough"
    if encoding == "vp8":
        wire, decoder = "VP8", "vp8dec"
    elif fmt is not None and fmt.pixel_format == "MJPG":
        wire, decoder = "MJPG", "jpegdec"
    elif fmt is not None:
        wire, decoder = fmt.pixel_format, "videoconvert"
    else:
        wire, decoder = None, "decodebin"
    return {
        "caps": caps,
        "negotiated": _fmt_dict(fmt) if fmt is not None else None,
        "encoding": encoding,
        "wire_codec": wire,
        "decode_with": decoder,
        "pending_negotiation": fmt is None,
    }


def _audio_stream_desc(ctx: _Context, argv: Sequence[str] | None) -> dict[str, object] | None:
    if ctx.medium == "video":
        return None
    encoding = "opus" if ctx.encode == "opus" else "passthrough"
    return {
        "caps": _caps_from(argv, ("audio/x-raw",)),
        "requested": ctx.request["audio"],
        "encoding": encoding,
        "wire_codec": "OPUS" if encoding == "opus" else "PCM",
        "decode_with": "opusdec" if encoding == "opus" else "audioconvert",
        "probed": False,
    }


def _consumer(ctx: _Context, video: dict[str, object] | None) -> dict[str, object]:
    """Exactly what a second process has to run. Nothing else to look up."""
    source = f"tcpclientsrc host={STREAM_HOST} port={ctx.port}"
    vdec = ""
    if video is not None:
        decoder = str(video["decode_with"])
        vdec = "" if decoder == "videoconvert" else f"{decoder} ! "

    if ctx.medium == "video":
        chain = f"matroskademux ! {vdec}videoconvert ! fakesink sync=false"
    elif ctx.medium == "audio":
        adec = "opusdec ! " if ctx.encode == "opus" else ""
        chain = f"matroskademux ! {adec}audioconvert ! fakesink sync=false"
    else:
        chain = (
            "matroskademux name=demux "
            f"demux. ! {vdec}videoconvert ! fakesink sync=false "
            "demux. ! audioconvert ! fakesink sync=false"
        )

    command = f"gst-launch-1.0 {source} ! {chain}"
    return {
        "gst_launch": shlex.split(command),
        "gst_launch_str": command,
        "generic": f"gst-launch-1.0 {source} ! decodebin ! fakesink sync=false",
        "save_to_file": f"gst-launch-1.0 {source} ! filesink location=stream.mkv",
        "raw_socket": (
            f"connect a TCP socket to {STREAM_HOST}:{ctx.port} and read — the bytes are a "
            "Matroska container (EBML magic 1a 45 df a3), with no handshake and no framing "
            "outside the container"
        ),
        "notes": (
            "attach any time while the stream is live; each consumer receives the stream "
            "from the moment it connects, and caps travel in the container so nothing has "
            "to be re-derived from this payload"
        ),
    }


def _attach(
    ctx: _Context,
    fmt: engine.VideoFormat | None,
    argv: Sequence[str] | None,
) -> dict[str, object]:
    video = _video_stream_desc(ctx, fmt, argv)
    return {
        "mechanism": "gstreamer-tcpserversink",
        "transport": "tcp",
        "host": STREAM_HOST,
        "port": ctx.port,
        "port_source": ctx.port_source,
        "uri": ctx.uri,
        "container": CONTAINER,
        "streamable": True,
        "streams": {"video": video, "audio": _audio_stream_desc(ctx, argv)},
        "clients": (
            "multiple simultaneous consumers are supported; each is served from the "
            "moment it connects (no rewind, no buffered history)"
        ),
        "exposure": (
            f"loopback only ({STREAM_HOST}) — never a routable address. Any process on "
            "this host that can connect to the port can read the stream while it is live; "
            "this tool does not authenticate consumers"
        ),
        "consumer": _consumer(ctx, video),
    }


def _payload(
    ctx: _Context,
    *,
    negotiation: dict[str, object],
    fmt: engine.VideoFormat | None,
    warmup: dict[str, object],
    argv: Sequence[str] | None,
    access_state: dict[str, object] | None,
    started_at: str | None,
    pid: int | None,
) -> dict[str, object]:
    mode = "apply" if ctx.apply_mode else "dry-run"
    device = ctx.device.as_dict()
    device["selector"] = ctx.args.device
    return {
        "verb": ctx.verb,
        "medium": ctx.medium,
        "mode": mode,
        "applied": ctx.apply_mode,
        "probed": ctx.probe_mode,
        "hardware_touched": ctx.probe_mode,
        "engine_checked": ctx.cap is not None,
        "device": device,
        "source": {
            "video_node": ctx.node,
            "alsa_address": ctx.alsa,
            "audio_node": (
                _alsa_node_path(ctx.device.audio)
                if ctx.alsa is not None and ctx.device.audio is not None
                else None
            ),
        },
        "request": ctx.request,
        "negotiation": negotiation,
        "attach": _attach(ctx, fmt, argv),
        "warmup": warmup,
        "pipeline": list(argv) if argv is not None else None,
        "pipeline_str": " ".join(argv) if argv is not None else None,
        "bounded": False,
        "lifetime": (
            "unbounded — the stream runs until this process is stopped (SIGINT/SIGTERM) or "
            "the pipeline exits. There is no duration cap by design; oversight comes from "
            "the activation log, not a timer. Use `webcam record` for a bounded artifact"
        ),
        "exclusive_access": (
            f"while this stream runs it holds {ctx.node or ctx.alsa} open — V4L2 streaming "
            "is single-open, so another client gets the typed busy error naming this "
            "process until the stream stops"
        ),
        "access": access_state,
        "consent": {
            "activation_log": str(activation.log_path()),
            "logged": ctx.probe_mode,
            "activity_light": _ACTIVITY_LIGHT_NOTE,
            "bytes_written": (
                f"only to the announced attachment point {ctx.uri} — no file, no hidden "
                "buffer, and never to stdout"
                if ctx.apply_mode
                else "none — this is a plan"
            ),
        },
        "started_at": started_at,
        "pid": pid,
    }


def _pretty(value: object) -> str:
    """Render a format/parameter dict for human output, ``-`` when unset."""
    if not isinstance(value, dict):
        return "-"
    if "pixel_format" in value:
        fields = [
            str(value["pixel_format"] or "any"),
            f"{value['width'] or 'any'}x{value['height'] or 'any'}",
            f"@{value['fps'] or 'any'}fps",
        ]
        return " ".join(fields)
    return f"{value.get('rate')}Hz x{value.get('channels')}ch"


def _render_text(payload: dict[str, object]) -> str:
    attach = payload["attach"]
    assert isinstance(attach, dict)
    negotiation = payload["negotiation"]
    assert isinstance(negotiation, dict)
    warmup = payload["warmup"]
    assert isinstance(warmup, dict)
    device = payload["device"]
    assert isinstance(device, dict)
    source = payload["source"]
    assert isinstance(source, dict)
    consumer = attach["consumer"]
    assert isinstance(consumer, dict)

    lines = [
        f"# {payload['verb']} — attachment plan ({payload['mode']})",
        "",
        f"device: {device['stable_id']} ({device['label']})",
    ]
    if source["video_node"] is not None:
        lines.append(f"video node: {source['video_node']}")
    if source["alsa_address"] is not None:
        lines.append(f"alsa address: {source['alsa_address']} ({source['audio_node']})")
    lines += [
        f"hardware touched: {payload['hardware_touched']}",
        "",
        f"negotiation: {negotiation['status']}",
        f"  requested: {_pretty(negotiation['requested'])}",
        f"  negotiated: {_pretty(negotiation['negotiated'])}",
        f"  validated against: {negotiation['validated_against']}",
        "",
        f"attach: {attach['uri']} ({attach['container']} over {attach['transport']}, "
        f"{attach['mechanism']})",
        f"  consumer: {consumer['gst_launch_str']}",
        f"  exposure: {attach['exposure']}",
        "",
        f"warm-up: {warmup['ms']} ms ({warmup['frames']} frames, {warmup['source']})",
        f"lifetime: {payload['lifetime']}",
        f"activation log: {payload['consent']['activation_log']} "  # type: ignore[index]
        f"(logged: {payload['consent']['logged']})",  # type: ignore[index]
    ]
    pipeline = payload["pipeline_str"]
    lines += ["", f"pipeline: {pipeline}" if pipeline else "pipeline: pending negotiation"]
    return "\n".join(lines)


def _emit(payload: dict[str, object], *, json_mode: bool) -> None:
    """The module's single stdout write. Flushed so a consumer can act at once."""
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(_render_text(payload), json_mode=False)
    try:
        sys.stdout.flush()
    except (OSError, ValueError):  # pragma: no cover - closed stdout
        pass


# --- the three media verbs ---------------------------------------------------


def _paper_run(ctx: _Context) -> int:
    """The default: resolve, check, print the plan. Nothing is touched."""
    negotiation, fmt = _negotiate(ctx)
    argv = _pipeline(ctx, fmt)
    warmup = _warmup(ctx, fmt)
    _emit(
        _payload(
            ctx,
            negotiation=negotiation,
            fmt=fmt,
            warmup=warmup,
            argv=argv,
            access_state=None,
            started_at=None,
            pid=None,
        ),
        json_mode=ctx.json_mode,
    )
    return 0


def _hardware_run(ctx: _Context) -> int:
    """``--probe`` or ``--apply``: the device is opened, so it is logged."""
    target = ctx.uri if ctx.apply_mode else f"probe://{ctx.node or ctx.alsa}"
    detail: dict[str, object] = {
        "mode": "apply" if ctx.apply_mode else "probe",
        "medium": ctx.medium,
        "video_node": ctx.node,
        "alsa_address": ctx.alsa,
        "request": ctx.request,
        "attachment": ctx.uri if ctx.apply_mode else None,
        "encode": ctx.encode,
    }

    with _logged_activation(
        device_id=ctx.device.stable_id,
        verb=ctx.verb,
        target=target,
        detail=detail,
    ) as act:
        access_state = _access(ctx)
        negotiation, fmt = _negotiate(ctx)
        warmup = _warmup(ctx, fmt)
        argv = _pipeline(ctx, fmt)
        detail["negotiated"] = negotiation["negotiated"]
        detail["warmup_ms"] = warmup["ms"]

        if not ctx.apply_mode:
            _emit(
                _payload(
                    ctx,
                    negotiation=negotiation,
                    fmt=fmt,
                    warmup=warmup,
                    argv=argv,
                    access_state=access_state,
                    started_at=act.started_at,
                    pid=None,
                ),
                json_mode=ctx.json_mode,
            )
            return 0

        assert argv is not None
        proc = _spawn(argv)
        detail["pid"] = proc.pid
        _warm_up(proc, warmup)
        _emit(
            _payload(
                ctx,
                negotiation=negotiation,
                fmt=fmt,
                warmup=warmup,
                argv=argv,
                access_state=access_state,
                started_at=act.started_at,
                pid=proc.pid,
            ),
            json_mode=ctx.json_mode,
        )
        status = _supervise(proc)
        if status:
            tail = proc.output_tail()
            detail["exit_status"] = status
            raise CliError(
                EXIT_ENV_ERROR,
                f"the stream pipeline exited with status {status}" + (f": {tail}" if tail else ""),
                remediation=(
                    "the device may have been unplugged or claimed by another client — "
                    "re-run with --probe to check what it enumerates"
                ),
            )
        return 0


def _stream(args: argparse.Namespace, medium: str) -> int:
    apply_mode = bool(getattr(args, "apply", False))
    probe_mode = bool(getattr(args, "probe", False)) or apply_mode

    device = devices.resolve(args.device)
    node, alsa = _sources(device, medium)
    request = _request(args, medium)
    cap = _capability(args) if probe_mode else None
    port, port_source = _port(args, apply_mode=apply_mode)

    ctx = _Context(
        args=args,
        medium=medium,
        device=device,
        node=node,
        alsa=alsa,
        request=request,
        cap=cap,
        port=port,
        port_source=port_source,
        apply_mode=apply_mode,
        probe_mode=probe_mode,
        json_mode=bool(getattr(args, "json", False)),
    )
    return _hardware_run(ctx) if probe_mode else _paper_run(ctx)


def cmd_stream_video(args: argparse.Namespace) -> int:
    return _stream(args, "video")


def cmd_stream_audio(args: argparse.Namespace) -> int:
    return _stream(args, "audio")


def cmd_stream_av(args: argparse.Namespace) -> int:
    return _stream(args, "av")


# --- overview ----------------------------------------------------------------


def stream_sections() -> list[dict[str, object]]:
    """Sections describing the ``stream`` noun (used by ``stream overview``)."""
    return [
        {
            "title": "Verbs",
            "items": [
                "stream video <device> — live camera attachment point",
                "stream audio <device> — live microphone attachment point (direct ALSA)",
                "stream av <device> — camera plus that device's own mic, muxed together",
                "stream overview — this description",
            ],
        },
        {
            "title": "What each invocation touches",
            "items": [
                "default (dry-run): nothing — resolves the device, checks the request "
                "structurally, prints the plan it would run",
                "--probe: opens the camera to enumerate its real formats "
                "(gst-device-monitor-1.0) and is written to the activation log",
                "--apply: opens the device and streams; implies --probe, applies warm-up, "
                "and is written to the activation log",
            ],
        },
        {
            "title": "Attachment",
            "items": [
                f"a GStreamer {SINK_ELEMENT} on {STREAM_HOST} serves {CONTAINER}-contained "
                "media; --port sets the port (default "
                f"{DEFAULT_PORT}, 0 auto-picks)",
                "the --json payload announces uri, container, caps and a ready-to-run "
                "consumer command — a second process needs nothing else",
                "loopback only, never a routable address; any local process that can "
                "connect to the port can read the stream while it is live",
            ],
        },
        {
            "title": "Warm-up",
            "items": [
                "the first frames off a UVC sensor are dark while auto-exposure settles; "
                "warm-up runs the pipeline before announcing the attachment point so "
                "those frames are discarded",
                f"defaults: {DEFAULT_WARMUP_FRAMES} frames (video/av) and "
                f"{DEFAULT_AUDIO_WARMUP_MS} ms (audio)",
                "override with --warmup-frames <N> or --warmup-ms <MS>; "
                "--warmup-frames 0 disables it",
            ],
        },
        {
            "title": "Contracts",
            "items": [
                "streams are unbounded: no duration flag, stop with SIGINT/SIGTERM "
                "(use `webcam record` for a bounded artifact)",
                "an unsupported (format, resolution, fps) is a typed user error naming "
                "the enumerated alternatives — never a silent fallback",
                "every activation is logged; a hardware activity light cannot be "
                "promised (device firmware), and this tool never implies it prevents "
                "covert use",
            ],
        },
    ]


def cmd_stream_overview(args: argparse.Namespace) -> int:
    # Mirrors overview.emit_overview but routes through this module's own
    # emit_result, keeping every stdout write in one seam.
    subject = "webcam stream"
    sections = stream_sections()
    if bool(getattr(args, "json", False)):
        emit_result({"subject": subject, "sections": sections}, json_mode=True)
    else:
        emit_result(render_text(subject, sections), json_mode=False)
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_stream_overview(args)


# --- registration ------------------------------------------------------------

_HARDWARE_EPILOG = (
    "Hardware: the default dry run touches nothing (it resolves, checks and prints "
    "the plan). --probe opens the camera to enumerate its real formats and is written "
    "to the activation log. --apply opens the device and streams, implies --probe, and "
    "is logged. Streams are unbounded: stop with SIGINT/SIGTERM."
)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "device",
        help="Stable device id (or a unique substring of one, or a /dev/v4l/by-id path). "
        "A bare /dev/videoN is refused: node numbering is plug-order, not identity.",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually open the device and serve the stream (implies --probe). "
        "Without it, the command is a dry run that touches no hardware.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Enumerate the device's real formats instead of checking the request on "
        "paper. This OPENS the camera and is written to the activation log.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        metavar="N",
        help=f"Loopback port for the attachment point (default {DEFAULT_PORT}; "
        "0 auto-picks a free port).",
    )


def _add_warmup(parser: argparse.ArgumentParser, *, frames: bool) -> None:
    group = parser.add_mutually_exclusive_group()
    if frames:
        group.add_argument(
            "--warmup-frames",
            type=int,
            default=None,
            metavar="N",
            help=f"Frames to discard while the sensor settles (default "
            f"{DEFAULT_WARMUP_FRAMES}; 0 disables warm-up).",
        )
    group.add_argument(
        "--warmup-ms",
        type=float,
        default=None,
        metavar="MS",
        help="Warm-up interval in milliseconds, instead of a frame count"
        + (f" (audio default {DEFAULT_AUDIO_WARMUP_MS})." if not frames else "."),
    )


def _add_video_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        metavar="FOURCC",
        default=None,
        help="Pixel format, V4L2 spelling (e.g. MJPG, YUYV). Omit to let negotiation "
        "choose from the device's enumerated set.",
    )
    parser.add_argument("--width", type=int, default=None, metavar="PX", help="Frame width.")
    parser.add_argument("--height", type=int, default=None, metavar="PX", help="Frame height.")
    parser.add_argument("--fps", type=float, default=None, metavar="N", help="Frame rate.")


def _add_audio_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--rate",
        type=int,
        default=48000,
        metavar="HZ",
        help="Sample rate (default 48000). Applied as an exact caps filter: an "
        "unsupported rate fails loudly, it is never silently substituted.",
    )
    parser.add_argument(
        "--channels",
        type=int,
        default=1,
        metavar="N",
        help="Channel count (default 1 — the C270's onboard mic is mono).",
    )


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "stream",
        help="Serve a live video / audio / muxed A-V attachment point (dry-run by default).",
        description="Expose a live attachment point another process can consume. "
        "Dry-run by default: nothing is opened until --apply.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=_no_verb, json=False)

    # parser_class must propagate, or this noun's parse errors bypass the
    # structured error contract and exit 2 instead of 1.
    noun_sub = p.add_subparsers(dest="stream_command", parser_class=type(p))

    overview_parser = noun_sub.add_parser(
        "overview",
        help="Describe the stream verb group (verbs, hardware split, attachment, warm-up).",
    )
    overview_parser.add_argument("--json", action="store_true", help="Emit structured JSON.")
    overview_parser.set_defaults(func=cmd_stream_overview)

    video = noun_sub.add_parser(
        "video",
        help="Serve a live camera stream (dry-run by default).",
        epilog=_HARDWARE_EPILOG,
    )
    _add_common(video)
    _add_video_flags(video)
    video.add_argument(
        "--encode",
        choices=("passthrough", "vp8"),
        default="passthrough",
        help="Wire codec. 'passthrough' (default) serves the device's own format; "
        "'vp8' re-encodes (requires the vp8enc element). H.264 is not offered: "
        "x264enc is absent on the reference host.",
    )
    _add_warmup(video, frames=True)
    video.set_defaults(func=cmd_stream_video)

    audio = noun_sub.add_parser(
        "audio",
        help="Serve a live microphone stream via direct ALSA (dry-run by default).",
        epilog=_HARDWARE_EPILOG,
    )
    _add_common(audio)
    _add_audio_flags(audio)
    audio.add_argument(
        "--encode",
        choices=("passthrough", "opus"),
        default="passthrough",
        help="Wire codec. 'passthrough' (default) serves raw PCM in Matroska; "
        "'opus' encodes (requires the opusenc element).",
    )
    _add_warmup(audio, frames=False)
    audio.set_defaults(func=cmd_stream_audio)

    av = noun_sub.add_parser(
        "av",
        help="Serve the camera and its own microphone muxed together (dry-run by default).",
        epilog=_HARDWARE_EPILOG
        + " Passthrough only: encoded A/V needs per-branch pipeline support the engine "
        "does not expose, so use `stream video --encode` / `stream audio --encode` for "
        "an encoded single medium.",
    )
    _add_common(av)
    _add_video_flags(av)
    _add_audio_flags(av)
    _add_warmup(av, frames=True)
    av.set_defaults(func=cmd_stream_av)
