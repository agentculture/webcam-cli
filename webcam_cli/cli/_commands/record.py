"""``webcam record`` — bounded-by-construction clip/audio recording.

Composes the four wave-1 modules rather than reimplementing any of them:
:mod:`webcam_cli.devices` resolves the selector to a
:class:`~webcam_cli.devices.LogicalDevice`, :mod:`webcam_cli.access` answers
"can this node be opened right now", :mod:`webcam_cli.engine` builds and
negotiates GStreamer pipelines, and :mod:`webcam_cli.activation` logs every
hardware touch. This module adds exactly one thing none of them owns: turning
a recording *request* into a *bounded* recording *session*.

Bounded by construction
------------------------
The build brief's operator-approved requirement is blunt: "bounded by default
and must be bounded" — no flag combination may express "forever". This is
enforced at the type level, not by a runtime check someone could route around:
:class:`Bound` refuses to construct with a non-finite, non-positive, or
absent ``duration_s`` (``__post_init__`` raises), so *every* ``Bound`` that
exists is already valid. ``--duration`` is optional on the command line, but
:func:`_build_bound` always supplies :data:`_DEFAULT_DURATION_S` when it is
omitted — a duration cap is never actually absent, only ever explicit or
defaulted. ``--max-bytes`` is an *additional*, optional cap layered on top;
omitting it never removes the duration cap. Both caps are hard-ceilinged
(:data:`_MAX_DURATION_S`, :data:`_MAX_BYTES_CEILING`) so even an explicit
request cannot approximate "unbounded" with an absurdly large number, and the
custom argparse ``type=`` functions reject ``0``, negative values, ``inf``,
and ``nan`` before a :class:`Bound` is ever constructed — see
``tests/test_record.py`` for a test that deliberately tries every escape this
module could think of.

Bounding mechanism: external process supervision, not a GStreamer-internal
timer. :func:`_run_bounded_phase` polls the ``gst-launch-1.0`` child at a
short interval and escalates SIGINT (ask nicely: gst-launch-1.0 treats SIGINT
as "send EOS, finalize, exit") then SIGKILL the moment the wall clock or the
output file size crosses its limit. ``splitmuxsink``'s ``max-size-time`` /
``max-size-bytes`` properties were the documented alternative and were
rejected on purpose: splitmuxsink's entire job is to *split into more than
one file* once a bound trips, which would break the "exactly one artifact"
guarantee below. External supervision also holds even if the pipeline never
responds to SIGINT — the deadline lives in this process, not the child's, so
the worst case is bounded at ``deadline_s + grace_s``, a fixed constant,
regardless of how badly the child misbehaves.

Dry-run / --probe split (deviations d1, d2)
--------------------------------------------
Default dry-run (no ``--apply``) is on-paper only: it resolves the device,
does non-blocking access probes (:func:`webcam_cli.access.check_access` — an
``O_NONBLOCK`` open/close, not a capture, and safe to call from a dry run by
that module's own design), and structurally validates any requested format by
calling the *same* pure pipeline builders (:func:`webcam_cli.engine.
build_video_pipeline` et al.) real capture would use — those never shell out.
It never calls :func:`webcam_cli.engine.probe_formats`, which shells out to
``gst-device-monitor-1.0`` and briefly opens the camera. ``--probe`` opts a
dry run into that real enumeration, and — because it energizes the sensor —
is itself wrapped in :func:`webcam_cli.activation.activation_scope` like any
other activation. ``--apply`` always negotiates for real (it is about to
activate the device anyway), so ``--probe`` is accepted-but-ignored alongside
``--apply`` rather than rejected as a conflicting flag.

Exactly one artifact
---------------------
The recording pipeline's sink is always a literal ``filesink
location=<output path>`` — never ``splitmuxsink``/``multifilesink``, which
segment into more than one file. The warm-up phase (below) always sinks to
``fakesink``, which writes zero bytes anywhere. So the only file this module
ever creates is the one named on the command line, and :func:`_require_artifact`
verifies it actually exists and is non-empty before reporting success — a
pipeline that dies silently is a typed error, not a phantom "success".

Warm-up
-------
The first frames off a UVC camera are dark while auto-exposure settles
(``CLAUDE.md``, "Domain constraints"). Because :mod:`webcam_cli.engine`'s
pipeline builders take a single ``sink`` string each and this module does not
own that file, warm-up is implemented as a *separate, first* bounded phase of
the same pipeline shape sunk to ``fakesink`` (discarding frames, writing
nothing) for ``--warmup`` seconds, before the real recording phase opens the
device again for the bounded recording window. Default:
:data:`_DEFAULT_WARMUP_VIDEO_S` (2.0s) for any kind that captures video,
:data:`_DEFAULT_WARMUP_AUDIO_S` (0.0s) for audio-only, since a microphone has
no exposure to settle. ``--warmup 0`` is allowed on purpose — unlike the
recording bound, a pre-roll discard of zero is a perfectly ordinary, finite
choice, not an unbounded one.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shlex
import signal
import subprocess  # nosec B404 - shelling out to gst-launch-1.0 is the documented engine posture
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from webcam_cli import access, activation, devices, engine
from webcam_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from webcam_cli.cli._output import emit_result
from webcam_cli.devices import LogicalDevice

# --- tunables ----------------------------------------------------------------

# A "clip" default: generous enough for a real recording, short enough that
# an agent calling `record` with no flags never blocks for long on I/O it
# never asked for.
_DEFAULT_DURATION_S = 30.0
# Hard ceiling no --duration value, however large, can exceed. `record` is a
# bounded-clip verb; unbounded/very-long capture is `stream`'s lane, not this
# one's — see the module docstring and CLAUDE.md's "asymmetry" note.
_MAX_DURATION_S = 3600.0
# Hard ceiling for the optional --max-bytes cap.
_MAX_BYTES_CEILING = 4 * 1024**3  # 4 GiB
# Hard ceiling for --warmup; a pre-roll discard has no business being long.
_MAX_WARMUP_S = 60.0

_DEFAULT_WARMUP_VIDEO_S = 2.0
_DEFAULT_WARMUP_AUDIO_S = 0.0

_DEFAULT_AUDIO_RATE = 48000
_DEFAULT_AUDIO_CHANNELS = 1

# Poll interval while a bounded phase runs, and grace period between the
# SIGINT ask-nicely and the SIGKILL that always follows if it is ignored.
_POLL_S = 0.2
_GRACE_S = 2.0

_INSTALL_HINT = (
    "install GStreamer with the tools plus good/base plugin sets, e.g. "
    "'sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-good "
    "gstreamer1.0-plugins-base gstreamer1.0-plugins-bad'"
)

# "hw:CARD=WEBCAM,DEV=0" -> capture PCM device number, for the /dev/snd node path.
_ALSA_DEV_RE = re.compile(r"DEV=(?P<dev>\d+)")

_KINDS = ("video", "audio", "av")


# ---------------------------------------------------------------------------
# Bound — the type that makes "unbounded" inexpressible.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bound:
    """A recording bound: a mandatory finite duration cap, plus an optional size cap.

    There is no way to construct a ``Bound`` that permits an unbounded
    recording: ``duration_s`` is required, must be finite, must be strictly
    positive, and is capped at :data:`_MAX_DURATION_S`. An optional
    ``max_bytes`` layers an additional cap on top; leaving it ``None`` simply
    means "not separately size-capped", never "no cap at all", because the
    duration cap is never absent. Construction — not a call site remembering
    to check something — is what refuses ``0``, negative numbers, ``inf`` and
    ``nan``, so bypassing the CLI's own ``argparse`` validation (as a test or
    a future caller might) still cannot produce an unbounded ``Bound``.
    """

    duration_s: float
    max_bytes: int | None
    duration_is_default: bool

    def __post_init__(self) -> None:
        if (
            self.duration_s is None
            or not isinstance(self.duration_s, (int, float))
            or not math.isfinite(self.duration_s)
            or self.duration_s <= 0
        ):
            raise CliError(
                EXIT_USER_ERROR,
                f"invalid recording bound: duration must be a finite number > 0 seconds, "
                f"got {self.duration_s!r} — record cannot express an unbounded recording",
                remediation=f"pass --duration between >0 and <= {_MAX_DURATION_S:g} seconds",
            )
        if self.duration_s > _MAX_DURATION_S:
            raise CliError(
                EXIT_USER_ERROR,
                f"invalid recording bound: duration {self.duration_s!r}s exceeds the hard "
                f"ceiling of {_MAX_DURATION_S:g} seconds",
                remediation=f"pass --duration <= {_MAX_DURATION_S:g} seconds",
            )
        if self.max_bytes is not None:
            if (
                not isinstance(self.max_bytes, int)
                or isinstance(self.max_bytes, bool)
                or self.max_bytes <= 0
            ):
                raise CliError(
                    EXIT_USER_ERROR,
                    f"invalid recording bound: max_bytes must be a whole number > 0, "
                    f"got {self.max_bytes!r}",
                    remediation=f"pass --max-bytes between 1 and {_MAX_BYTES_CEILING} bytes",
                )
            if self.max_bytes > _MAX_BYTES_CEILING:
                raise CliError(
                    EXIT_USER_ERROR,
                    f"invalid recording bound: max_bytes {self.max_bytes!r} exceeds the hard "
                    f"ceiling of {_MAX_BYTES_CEILING} bytes",
                    remediation=f"pass --max-bytes <= {_MAX_BYTES_CEILING} bytes",
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "duration_s": self.duration_s,
            "duration_is_default": self.duration_is_default,
            "max_bytes": self.max_bytes,
        }


def _build_bound(duration: float | None, max_bytes: int | None) -> Bound:
    """A duration cap is *always* enforced: explicit, or :data:`_DEFAULT_DURATION_S`."""
    if duration is None:
        return Bound(duration_s=_DEFAULT_DURATION_S, max_bytes=max_bytes, duration_is_default=True)
    return Bound(duration_s=duration, max_bytes=max_bytes, duration_is_default=False)


# ---------------------------------------------------------------------------
# argparse type= functions — reject the escape hatches before Bound even sees them.
# ---------------------------------------------------------------------------


def _duration_type(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid --duration {raw!r}: must be a number of seconds"
        ) from exc
    if math.isnan(value) or math.isinf(value):
        raise argparse.ArgumentTypeError(
            f"invalid --duration {raw!r}: must be finite — inf/nan would mean unbounded, "
            "which record does not allow"
        )
    if value <= 0:
        raise argparse.ArgumentTypeError(
            f"invalid --duration {raw!r}: must be > 0 seconds — 0 or negative would mean "
            "unbounded, which record does not allow"
        )
    if value > _MAX_DURATION_S:
        raise argparse.ArgumentTypeError(
            f"invalid --duration {raw!r}: must be <= {_MAX_DURATION_S:g} seconds "
            "(record's hard ceiling for a single clip)"
        )
    return value


def _max_bytes_type(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid --max-bytes {raw!r}: must be a whole number of bytes"
        ) from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(
            f"invalid --max-bytes {raw!r}: must be > 0 — 0 or negative would mean unbounded, "
            "which record does not allow"
        )
    if value > _MAX_BYTES_CEILING:
        raise argparse.ArgumentTypeError(
            f"invalid --max-bytes {raw!r}: must be <= {_MAX_BYTES_CEILING} bytes "
            "(record's hard ceiling for a single artifact)"
        )
    return value


def _warmup_type(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid --warmup {raw!r}: must be a number of seconds"
        ) from exc
    if math.isnan(value) or math.isinf(value) or value < 0:
        raise argparse.ArgumentTypeError(
            f"invalid --warmup {raw!r}: must be a finite number >= 0 seconds"
        )
    if value > _MAX_WARMUP_S:
        raise argparse.ArgumentTypeError(f"invalid --warmup {raw!r}: must be <= {_MAX_WARMUP_S:g}")
    return value


def _positive_int_type(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} must be a whole number") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{raw!r} must be > 0")
    return value


def _positive_float_type(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"{raw!r} must be a finite number > 0")
    return value


def _pixel_format_type(raw: str) -> str:
    value = raw.strip().upper()
    if not value:
        raise argparse.ArgumentTypeError("--pixel-format must not be empty")
    return value


# ---------------------------------------------------------------------------
# request parsing / validation
# ---------------------------------------------------------------------------


def _requested_video_format(args: argparse.Namespace) -> engine.VideoFormat | None:
    fields = (args.pixel_format, args.width, args.height, args.fps)
    given = [f is not None for f in fields]
    if not any(given):
        return None
    if not all(given):
        raise CliError(
            EXIT_USER_ERROR,
            "incomplete video format request",
            remediation=(
                "pass all of --pixel-format/--width/--height/--fps together to request a "
                "specific format, or none of them to negotiate a default at capture time"
            ),
        )
    return engine.VideoFormat(
        pixel_format=args.pixel_format, width=args.width, height=args.height, fps=args.fps
    )


def _requested_audio_format(args: argparse.Namespace) -> engine.AudioFormat | None:
    fields = (args.rate, args.channels)
    given = [f is not None for f in fields]
    if not any(given):
        return None
    if not all(given):
        raise CliError(
            EXIT_USER_ERROR,
            "incomplete audio format request",
            remediation="pass both --rate and --channels together, or neither to use the default",
        )
    return engine.AudioFormat(rate=args.rate, channels=args.channels)


def _resolve_warmup(warmup: float | None, kind: str) -> float:
    if warmup is not None:
        return warmup
    return _DEFAULT_WARMUP_VIDEO_S if kind in ("video", "av") else _DEFAULT_WARMUP_AUDIO_S


def _validate_output_path(raw: str) -> str:
    path = os.path.abspath(raw)
    if os.path.isdir(path):
        raise CliError(
            EXIT_USER_ERROR,
            f"{path} is a directory",
            remediation="pass a file path to write the recording to, not a directory",
        )
    parent = os.path.dirname(path) or "/"
    if not os.path.isdir(parent):
        raise CliError(
            EXIT_USER_ERROR,
            f"parent directory {parent} does not exist",
            remediation=f"create {parent} first, or pass a path inside an existing directory",
        )
    return path


def _capture_targets(device: LogicalDevice, kind: str) -> tuple[str | None, str | None]:
    node = device.capture_node
    audio_address = device.audio.alsa_address if device.audio is not None else None
    if kind in ("video", "av") and node is None:
        raise CliError(
            EXIT_USER_ERROR,
            f"{device.stable_id} has no camera to record video from",
            remediation="pass --kind audio to record its microphone, or pick a different "
            "device (see 'webcam list --json')",
        )
    if kind in ("audio", "av") and audio_address is None:
        raise CliError(
            EXIT_USER_ERROR,
            f"{device.stable_id} has no paired microphone",
            remediation="pass --kind video to record its camera only, or pick a device with "
            "a microphone (see 'webcam list --json')",
        )
    return node, audio_address


def _audio_node_path(card: devices.AudioCard) -> str:
    """The ALSA capture PCM device node for ``card``, e.g. ``/dev/snd/pcmC1D0c``.

    Mirrors the sibling ``list`` verb's reconstruction: :class:`AudioCard`
    carries ``alsa_address`` (the handle worth persisting) but not the raw
    node path :func:`webcam_cli.access.check_access`/``require_access`` need.
    """
    matched = _ALSA_DEV_RE.search(card.alsa_address)
    dev = matched.group("dev") if matched else "0"
    return f"/dev/snd/pcmC{card.index}D{dev}c"


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------


def _video_fmt_dict(fmt: engine.VideoFormat | None) -> dict[str, object] | None:
    if fmt is None:
        return None
    return {
        "pixel_format": fmt.pixel_format,
        "width": fmt.width,
        "height": fmt.height,
        "fps": fmt.fps,
    }


def _audio_fmt_dict(fmt: engine.AudioFormat | None) -> dict[str, object] | None:
    if fmt is None:
        return None
    return {"rate": fmt.rate, "channels": fmt.channels}


def _report_access(report: access.AccessReport) -> dict[str, object]:
    return {"state": report.state.value, "path": report.path, "remediation": report.remediation}


def _access_report(
    device: LogicalDevice, kind: str, capture_node: str | None
) -> dict[str, object | None]:
    video_report = None
    audio_report = None
    if kind in ("video", "av") and capture_node is not None:
        video_report = _report_access(access.check_access(capture_node, "video"))
    if kind in ("audio", "av") and device.audio is not None:
        video_path = _audio_node_path(device.audio)
        audio_report = _report_access(access.check_access(video_path, "audio"))
    return {"video": video_report, "audio": audio_report}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render_text(report: dict[str, object]) -> str:
    bound = report["bound"]
    device = report["device"]
    lines = [
        f"record: {'apply' if report['apply'] else 'dry-run'}",
        f"device: {device['stable_id']} ({device['label']})",
        f"kind: {report['kind']}",
        f"output: {report['output_path']}",
        f"bound: duration<={bound['duration_s']:g}s"
        + (" (default)" if bound["duration_is_default"] else "")
        + (f", max<={bound['max_bytes']}B" if bound["max_bytes"] is not None else ""),
        f"warmup: {report['warmup_s']:g}s",
    ]
    if report["apply"]:
        lines.append(f"bytes_written: {report['bytes_written']}")
        lines.append(f"stopped_reason: {report['stopped_reason']}")
    else:
        lines.append(f"would_write: {report['would_write'][0]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# activation-log plumbing
# ---------------------------------------------------------------------------


def _with_activation(
    device: LogicalDevice,
    *,
    target: str,
    detail: dict[str, object],
    body: Callable[[activation.Activation], dict[str, object]],
) -> dict[str, object]:
    """Wrap ``body`` in one activation-log entry, translating a log-write failure.

    :func:`webcam_cli.activation.activation_scope` raises a raw ``OSError``
    if the log itself cannot be written (permission denied, missing parent,
    disk full, ...) — that must never reach the caller as an unwrapped
    exception, so it is converted to a typed, hinted :class:`CliError` here.
    """
    try:
        with activation.activation_scope(
            device_id=device.stable_id, verb="record", target=target, detail=dict(detail)
        ) as act:
            return body(act)
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"failed to write the activation log: {exc}",
            remediation=(
                f"ensure {activation.log_path()} is writable, or set "
                f"${activation.ENV_LOG_PATH} to a writable location"
            ),
        ) from exc


def _probed_formats(device: LogicalDevice, capture_node: str) -> tuple[engine.VideoFormat, ...]:
    return _with_activation(
        device,
        target=capture_node,
        detail={"action": "probe", "kind": "video"},
        body=lambda _act: engine.probe_formats(capture_node),
    )


# ---------------------------------------------------------------------------
# bounded execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PhaseResult:
    returncode: int
    stopped_reason: str  # "completed" | "duration" | "size"
    stdout: str
    stderr: str


def _current_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _run_bounded_phase(
    argv: list[str],
    *,
    deadline_s: float,
    max_bytes: int | None,
    output_path: str | None,
    popen_factory: Callable[..., subprocess.Popen] | None = None,
    clock: Callable[[], float] | None = None,
    poll_s: float = _POLL_S,
    grace_s: float = _GRACE_S,
) -> _PhaseResult:
    """Run one ``gst-launch-1.0`` phase, hard-bounded to ``deadline_s`` wall-clock seconds.

    See the module docstring for why this — external process supervision —
    was chosen over ``splitmuxsink``'s internal size/time properties. The
    child is polled every ``poll_s``; the moment elapsed time reaches
    ``deadline_s``, or (when ``max_bytes`` is set) the file at
    ``output_path`` reaches that size, SIGINT is sent (gst-launch-1.0's
    documented clean-stop signal — sends EOS, finalizes the container, exits)
    and given ``grace_s`` to comply before SIGKILL. This holds even if the
    child never responds to SIGINT: the wall clock lives in this function,
    not the child, so it always returns within ``deadline_s + grace_s``
    (plus a bounded ``communicate()`` after the kill, which cannot be
    ignored by the target process).

    ``popen_factory``/``clock`` default to :data:`subprocess.Popen`/
    :func:`time.monotonic`, looked up fresh on every call (not bound at
    import time) so tests can monkeypatch ``record.subprocess.Popen`` /
    ``record.time.monotonic`` directly, or inject fakes via the parameters.
    """
    factory = popen_factory or subprocess.Popen
    now = clock or time.monotonic

    try:
        proc = factory(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"failed to start {argv[0]}: {exc}",
            remediation=_INSTALL_HINT,
        ) from exc

    start = now()
    reason = "completed"
    stdout = stderr = ""
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=poll_s)
            break
        except subprocess.TimeoutExpired:
            elapsed = now() - start
            size_hit = (
                max_bytes is not None
                and output_path is not None
                and _current_size(output_path) >= max_bytes
            )
            if size_hit or elapsed >= deadline_s:
                reason = "size" if size_hit else "duration"
                proc.send_signal(signal.SIGINT)
                try:
                    stdout, stderr = proc.communicate(timeout=grace_s)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                break

    returncode = proc.returncode if proc.returncode is not None else -1
    return _PhaseResult(
        returncode=returncode, stopped_reason=reason, stdout=stdout or "", stderr=stderr or ""
    )


def _require_artifact(output_path: str, result: _PhaseResult) -> int:
    """Confirm exactly the named artifact was written; never trust a return code alone."""
    try:
        size = os.path.getsize(output_path)
    except OSError as exc:
        detail = (result.stderr or "").strip()
        message = f"recording pipeline produced no output at {output_path}"
        if detail:
            message = f"{message}: {detail[-500:]}"
        raise CliError(
            EXIT_ENV_ERROR,
            message,
            remediation="check the GStreamer install, device permissions, and pipeline stderr",
        ) from exc
    if size == 0:
        raise CliError(
            EXIT_ENV_ERROR,
            f"recording pipeline wrote an empty file at {output_path}",
            remediation="pipeline exited without producing data; check stderr and format support",
        )
    return size


def _pin_executable(argv: list[str], gst_launch: str | None) -> list[str]:
    """Replace ``argv[0]`` with the resolved absolute binary path, when known."""
    if gst_launch:
        return [gst_launch, *argv[1:]]
    return argv


def _build_pipelines(
    kind: str,
    capture_node: str | None,
    video_fmt: engine.VideoFormat | None,
    audio_address: str | None,
    audio_fmt: engine.AudioFormat | None,
    sink: str,
    warmup_s: float,
) -> tuple[list[str], list[str] | None]:
    if kind == "video":
        record_argv = engine.build_video_pipeline(capture_node, video_fmt, sink)
        warmup_argv = (
            engine.build_video_pipeline(capture_node, video_fmt, "fakesink")
            if warmup_s > 0
            else None
        )
    elif kind == "audio":
        record_argv = engine.build_audio_pipeline(audio_address, audio_fmt, sink)
        warmup_argv = None
    else:  # av
        record_argv = engine.build_av_pipeline(
            capture_node, video_fmt, audio_address, audio_fmt, sink
        )
        warmup_argv = (
            engine.build_video_pipeline(capture_node, video_fmt, "fakesink")
            if warmup_s > 0
            else None
        )
    return record_argv, warmup_argv


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def _dry_run(
    *,
    device: LogicalDevice,
    kind: str,
    capture_node: str | None,
    audio_address: str | None,
    requested_video: engine.VideoFormat | None,
    requested_audio: engine.AudioFormat | None,
    bound: Bound,
    warmup_s: float,
    output_path: str,
    probe: bool,
) -> dict[str, object]:
    planned_video: engine.VideoFormat | None = None
    video_probed = False
    if kind in ("video", "av"):
        if probe:
            available = _probed_formats(device, capture_node)
            planned_video = engine.validate_negotiation(requested_video, available)
            video_probed = True
        elif requested_video is not None:
            planned_video = requested_video

    planned_audio: engine.AudioFormat | None = None
    if kind in ("audio", "av"):
        planned_audio = requested_audio or engine.AudioFormat(
            rate=_DEFAULT_AUDIO_RATE, channels=_DEFAULT_AUDIO_CHANNELS
        )

    sink = f"filesink location={shlex.quote(output_path)}"
    pipeline_preview: list[str] | None = None
    if kind == "video" and planned_video is not None:
        pipeline_preview = engine.build_video_pipeline(capture_node, planned_video, sink)
    elif kind == "audio" and planned_audio is not None:
        pipeline_preview = engine.build_audio_pipeline(audio_address, planned_audio, sink)
    elif kind == "av" and planned_video is not None and planned_audio is not None:
        pipeline_preview = engine.build_av_pipeline(
            capture_node, planned_video, audio_address, planned_audio, sink
        )

    cap = engine.detect()

    return {
        "mode": "dry-run",
        "apply": False,
        "device": device.as_dict(),
        "kind": kind,
        "capture_node": capture_node,
        "audio_address": audio_address,
        "video_format": (
            {
                "requested": _video_fmt_dict(requested_video),
                "planned": _video_fmt_dict(planned_video),
                "probed": video_probed,
            }
            if kind in ("video", "av")
            else None
        ),
        "audio_format": (
            {
                "requested": _audio_fmt_dict(requested_audio),
                "planned": _audio_fmt_dict(planned_audio),
                "probed": False,
            }
            if kind in ("audio", "av")
            else None
        ),
        "pipeline_preview": pipeline_preview,
        "bound": bound.as_dict(),
        "warmup_s": warmup_s,
        "output_path": output_path,
        "would_write": [output_path],
        "access": _access_report(device, kind, capture_node),
        "engine": {"available": cap.available, "gst_launch_present": cap.gst_launch is not None},
        "timestamps": {"resolved_at": _now_iso()},
    }


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _apply(
    *,
    device: LogicalDevice,
    kind: str,
    capture_node: str | None,
    audio_address: str | None,
    requested_video: engine.VideoFormat | None,
    requested_audio: engine.AudioFormat | None,
    bound: Bound,
    warmup_s: float,
    output_path: str,
) -> dict[str, object]:
    def _do(act: activation.Activation) -> dict[str, object]:
        cap = engine.require_engine()

        if kind in ("video", "av"):
            access.require_access(capture_node, "video")
        if kind in ("audio", "av"):
            access.require_access(_audio_node_path(device.audio), "audio")

        negotiated_video: engine.VideoFormat | None = None
        video_probed = False
        if kind in ("video", "av"):
            available = engine.probe_formats(capture_node)
            negotiated_video = engine.validate_negotiation(requested_video, available)
            video_probed = True

        negotiated_audio: engine.AudioFormat | None = None
        if kind in ("audio", "av"):
            negotiated_audio = requested_audio or engine.AudioFormat(
                rate=_DEFAULT_AUDIO_RATE, channels=_DEFAULT_AUDIO_CHANNELS
            )

        sink = f"filesink location={shlex.quote(output_path)}"
        record_argv, warmup_argv = _build_pipelines(
            kind, capture_node, negotiated_video, audio_address, negotiated_audio, sink, warmup_s
        )
        record_argv = _pin_executable(record_argv, cap.gst_launch)
        if warmup_argv is not None:
            warmup_argv = _pin_executable(warmup_argv, cap.gst_launch)

        started_at = _now_iso()
        warmup_started_at: str | None = None
        if warmup_argv is not None:
            warmup_started_at = _now_iso()
            _run_bounded_phase(warmup_argv, deadline_s=warmup_s, max_bytes=None, output_path=None)

        recording_started_at = _now_iso()
        result = _run_bounded_phase(
            record_argv,
            deadline_s=bound.duration_s,
            max_bytes=bound.max_bytes,
            output_path=output_path,
        )
        ended_at = _now_iso()

        size = _require_artifact(output_path, result)

        act.detail.update(
            {
                "kind": kind,
                "output_path": output_path,
                "video_format": _video_fmt_dict(negotiated_video),
                "audio_format": _audio_fmt_dict(negotiated_audio),
                "bound": bound.as_dict(),
                "warmup_s": warmup_s,
                "bytes_written": size,
                "stopped_reason": result.stopped_reason,
            }
        )

        return {
            "mode": "apply",
            "apply": True,
            "device": device.as_dict(),
            "kind": kind,
            "capture_node": capture_node,
            "audio_address": audio_address,
            "video_format": (
                {
                    "requested": _video_fmt_dict(requested_video),
                    "negotiated": _video_fmt_dict(negotiated_video),
                    "probed": video_probed,
                }
                if kind in ("video", "av")
                else None
            ),
            "audio_format": (
                {
                    "requested": _audio_fmt_dict(requested_audio),
                    "negotiated": _audio_fmt_dict(negotiated_audio),
                    "probed": False,
                }
                if kind in ("audio", "av")
                else None
            ),
            "bound": bound.as_dict(),
            "warmup_s": warmup_s,
            "output_path": output_path,
            "bytes_written": size,
            "stopped_reason": result.stopped_reason,
            "timestamps": {
                "started_at": started_at,
                "warmup_started_at": warmup_started_at,
                "recording_started_at": recording_started_at,
                "ended_at": ended_at,
            },
            "pipeline": record_argv,
        }

    return _with_activation(
        device, target=output_path, detail={"action": "record", "kind": kind}, body=_do
    )


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def cmd_record(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    kind = args.kind

    requested_video = _requested_video_format(args)
    requested_audio = _requested_audio_format(args)

    if kind == "audio" and requested_video is not None:
        raise CliError(
            EXIT_USER_ERROR,
            "video format flags require --kind video or av",
            remediation="drop --pixel-format/--width/--height/--fps, or pass --kind video/av",
        )
    if kind == "video" and requested_audio is not None:
        raise CliError(
            EXIT_USER_ERROR,
            "audio format flags require --kind audio or av",
            remediation="drop --rate/--channels, or pass --kind audio/av",
        )
    if bool(args.probe) and not args.apply and kind == "audio":
        raise CliError(
            EXIT_USER_ERROR,
            "--probe has nothing to enumerate for --kind audio: the engine only probes "
            "video formats",
            remediation="drop --probe for an audio-only dry-run, or pass --kind video/av",
        )

    output_path = _validate_output_path(args.output)
    root = getattr(args, "root", None) or "/"
    device = devices.resolve(args.device, root=root)
    capture_node, audio_address = _capture_targets(device, kind)
    warmup_s = _resolve_warmup(args.warmup, kind)
    bound = _build_bound(args.duration, args.max_bytes)

    if args.apply:
        report = _apply(
            device=device,
            kind=kind,
            capture_node=capture_node,
            audio_address=audio_address,
            requested_video=requested_video,
            requested_audio=requested_audio,
            bound=bound,
            warmup_s=warmup_s,
            output_path=output_path,
        )
    else:
        report = _dry_run(
            device=device,
            kind=kind,
            capture_node=capture_node,
            audio_address=audio_address,
            requested_video=requested_video,
            requested_audio=requested_audio,
            bound=bound,
            warmup_s=warmup_s,
            output_path=output_path,
            probe=bool(args.probe),
        )

    emit_result(report if json_mode else _render_text(report), json_mode=json_mode)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "record",
        help="Record a bounded video/audio/AV clip from a capture device to a file.",
        description=(
            "Record a bounded clip from a resolved capture device. Dry-run by default: "
            "resolves the device and validates the request without energizing hardware. "
            "Pass --apply to actually record. A duration and/or size cap is always "
            "enforced -- see --duration/--max-bytes -- there is no flag that means 'forever'."
        ),
    )
    p.add_argument(
        "device", metavar="DEVICE", help="Stable device id or unique selector (see 'webcam list')."
    )
    p.add_argument("output", metavar="OUTPUT_PATH", help="File path to write the recording to.")
    p.add_argument(
        "--kind",
        choices=_KINDS,
        default="video",
        help="What to record: video, audio, or av (default: video).",
    )
    p.add_argument(
        "--pixel-format",
        type=_pixel_format_type,
        default=None,
        metavar="FOURCC",
        help=(
            "Requested video pixel format, e.g. MJPG "
            "(video/av only; needs --width/--height/--fps too)."
        ),
    )
    p.add_argument(
        "--width",
        type=_positive_int_type,
        default=None,
        help="Requested video width in pixels (video/av only).",
    )
    p.add_argument(
        "--height",
        type=_positive_int_type,
        default=None,
        help="Requested video height in pixels (video/av only).",
    )
    p.add_argument(
        "--fps",
        type=_positive_float_type,
        default=None,
        help="Requested video frame rate (video/av only).",
    )
    p.add_argument(
        "--rate",
        type=_positive_int_type,
        default=None,
        help=f"Requested audio sample rate in Hz (audio/av only; default {_DEFAULT_AUDIO_RATE}).",
    )
    p.add_argument(
        "--channels",
        type=_positive_int_type,
        default=None,
        help=f"Requested audio channel count (audio/av only; default {_DEFAULT_AUDIO_CHANNELS}).",
    )
    p.add_argument(
        "--duration",
        type=_duration_type,
        default=None,
        metavar="SECONDS",
        help=(
            f"Wall-clock recording cap in seconds, 0 < duration <= {_MAX_DURATION_S:g} "
            f"(default: {_DEFAULT_DURATION_S:g}). No value means unbounded -- there is no "
            "such value."
        ),
    )
    p.add_argument(
        "--max-bytes",
        type=_max_bytes_type,
        default=None,
        metavar="BYTES",
        help=(
            "Additional output-size cap in bytes, on top of --duration "
            f"(0 < max-bytes <= {_MAX_BYTES_CEILING}). Optional -- --duration alone always "
            "bounds the recording even when this is omitted."
        ),
    )
    p.add_argument(
        "--warmup",
        type=_warmup_type,
        default=None,
        metavar="SECONDS",
        help=(
            "Sensor warm-up discarded before the recorded window, letting auto-exposure "
            f"settle (default: {_DEFAULT_WARMUP_VIDEO_S:g}s for video/av, "
            f"{_DEFAULT_WARMUP_AUDIO_S:g}s for audio-only). 0 is allowed -- this is a "
            "pre-roll setting, not the recording bound."
        ),
    )
    p.add_argument(
        "--probe",
        action="store_true",
        help=(
            "Dry-run only: really enumerate the device's video formats (briefly energizes "
            "the sensor; logged as an activation). Ignored when combined with --apply, "
            "which always negotiates for real."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually record. Without this flag, record only resolves and validates (dry-run).",
    )
    p.add_argument(
        "--root",
        default="/",
        metavar="PATH",
        help=(
            "Filesystem root to resolve the device under (default: /); mainly for pointing "
            "at a synthetic device tree in tests."
        ),
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_record)
