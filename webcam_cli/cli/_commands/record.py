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
Default dry-run (no ``--apply`` and no ``--probe``) is on-paper only: it
resolves the device, reports access via a *non-opening* filesystem check
(:func:`_paper_access_report` — ``os.path.exists``/``os.access`` bit tests,
never ``open()``), and structurally validates any requested format by calling
the *same* pure pipeline builders (:func:`webcam_cli.engine.
build_video_pipeline` et al.) real capture would use — those never shell out.
A plain ``os.access`` check cannot tell whether a node that exists and looks
permitted is already held open by another process: V4L2 exclusivity is only
enforced at ``S_FMT``/``STREAMON``, not at ``open(2)`` (see
:func:`webcam_cli.access.busy_error`'s docstring), so that case is reported as
``"unknown"``, never ``"ok"`` — the payload says plainly that ``--probe`` or
``--apply`` is what actually finds out. Default dry-run never calls
:func:`webcam_cli.access.check_access` (an ``open()``/``close()`` pair) or
:func:`webcam_cli.engine.probe_formats` (which shells out to
``gst-device-monitor-1.0``) — both really touch the device, so both stay off
the no-flag path entirely, matching "no flag = opens nothing, logs nothing".
``--probe`` opts a dry run into real enumeration *and* the real
``access.check_access`` open/close for the access block, and — because it
energizes the sensor — is itself wrapped in
:func:`webcam_cli.activation.activation_scope` like any other activation.
``--apply`` always negotiates for real (it is about to activate the device
anyway), so ``--probe`` is accepted-but-ignored alongside ``--apply`` rather
than rejected as a conflicting flag.

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
nothing), before the real recording phase opens the device again for the
bounded recording window. That phase is built with ``mux=False``: it
reproduces the negotiated source caps exactly — the sensor has to settle at
the resolution and frame rate it will record at — but appends no container
tail, because every frame it produces is thrown away and encoding discarded
pixels to VP8 buys nothing (see :func:`_warmup_pipeline`).

The default is :data:`webcam_cli.engine.DEFAULT_WARMUP_FRAMES` frames
converted through the **negotiated fps** — the same measured constant
``webcam stream`` uses, so the two verbs cannot drift apart as they did while
both were guesses (30 frames vs a flat 2.0 s). The unit matters: settle was
measured to track frame count, not wall-clock time, so a fixed-seconds default
under-warms at low frame rates. Audio-only defaults to
:data:`_DEFAULT_WARMUP_AUDIO_S` (0.0 s), since a microphone has no exposure to
settle. ``--warmup SECONDS`` overrides, and ``--warmup 0`` is allowed on
purpose — unlike the recording bound, a pre-roll discard of zero is a
perfectly ordinary, finite choice, not an unbounded one.
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

# Video warm-up is expressed in *frames* and converted through the negotiated
# fps, because that is what was measured: see
# `webcam_cli.engine.DEFAULT_WARMUP_FRAMES` for the runs. The constant is
# shared with `webcam stream` so the two verbs cannot drift apart again — they
# disagreed (30 frames vs a flat 2.0 s) for as long as neither was measured.
# `_DEFAULT_WARMUP_VIDEO_S` remains only as the fallback used when no format
# has been negotiated yet and there is no fps to convert through.
_DEFAULT_WARMUP_VIDEO_S = engine.warmup_seconds(
    engine.DEFAULT_WARMUP_FRAMES, engine.WARMUP_FPS_ASSUMPTION
)
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


def _resolve_warmup(
    warmup: float | None, kind: str, fmt: engine.VideoFormat | None = None
) -> float:
    """Seconds of pre-roll to discard: explicit, else the measured frame count.

    An explicit ``--warmup`` is honoured verbatim. The default is
    :data:`webcam_cli.engine.DEFAULT_WARMUP_FRAMES` frames converted through
    the *negotiated* fps, so a 5 fps recording warms up for the same number of
    frames as a 30 fps one — which is what the measurement says the sensor
    needs. Audio has no exposure to settle and defaults to zero.
    """
    if warmup is not None:
        return warmup
    if kind not in ("video", "av"):
        return _DEFAULT_WARMUP_AUDIO_S
    if fmt is None:
        return _DEFAULT_WARMUP_VIDEO_S
    return engine.warmup_seconds(engine.DEFAULT_WARMUP_FRAMES, fmt.fps)


def _warmup_frames(warmup: float | None, kind: str, fmt: engine.VideoFormat | None) -> int | None:
    """How many frames the resolved warm-up actually discards, when knowable."""
    if kind not in ("video", "av"):
        return None
    seconds = _resolve_warmup(warmup, kind, fmt)
    fps = fmt.fps if fmt is not None else engine.WARMUP_FPS_ASSUMPTION
    return round(seconds * fps)


_WARMUP_BASIS = (
    f"default = {engine.DEFAULT_WARMUP_FRAMES} frames converted through the negotiated "
    "fps (about 2x the 13-15 frame auto-exposure settle measured on the reference C270 "
    "at both 30 fps and 5 fps). Settle tracks frames, not wall-clock seconds, so a "
    "fixed-seconds default under-warms at low frame rates. --warmup SECONDS overrides; "
    "--warmup 0 disables. `webcam stream` uses the same measured constant."
)


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


# --- non-opening access check, for the no-flag dry-run path only -----------
#
# A plain ``webcam record`` dry-run must not energize hardware at all — see
# the module docstring's "Dry-run / --probe split" and CLAUDE.md's
# three-level hardware rule ("no flag = opens nothing, logs nothing"). The
# real ``access.check_access`` above performs an ``os.open()``/``os.close()``
# pair to answer its question, which *is* a hardware touch (a UVC camera can
# power on or light its LED on open) — safe for ``--probe``/``--apply``,
# which already document themselves as touching hardware, but wrong for a
# bare dry-run. These helpers answer the same "can this node be opened"
# question using only ``os.path.exists``/``os.access`` bit tests, which never
# call ``open(2)``. That means one thing is structurally undeterminable here:
# EBUSY. V4L2 exclusivity is enforced at ``S_FMT``/``STREAMON``, not at
# ``open(2)`` (see :func:`webcam_cli.access.busy_error`'s docstring), so a
# node that exists and looks permitted is reported ``"unknown"``, never
# ``"ok"`` — only a real open (``--probe``/``--apply``) can tell the two
# apart.


def _paper_absent_remediation(kind: str, path: str) -> str:
    if kind == "audio":
        return (
            f"{path} does not exist — check the microphone/card is plugged in and "
            "listed by 'arecord -l' or /proc/asound/cards; ALSA card numbers renumber "
            "on replug, so a stale path is the most common cause. (checked without "
            "opening the device: pass --probe or --apply to actually try.)"
        )
    return (
        f"{path} does not exist — check the camera is plugged in and listed under "
        "/dev/v4l/by-id/ (or /dev/videoN); V4L2 node numbers renumber on replug, so a "
        "stale path is the most common cause. (checked without opening the device: "
        "pass --probe or --apply to actually try.)"
    )


def _paper_forbidden_remediation(kind: str, path: str) -> str:
    if kind == "audio":
        return (
            f"permission denied opening {path} — ALSA capture devices are gated by "
            "'audio'-group membership; add the invoking user to the 'audio' group and "
            "re-login (unlike /dev/video*, there is no seat-ACL path for ALSA on this "
            "host). (checked without opening the device: the permission bits alone say "
            "this, --probe/--apply would fail the same way.)"
        )
    return (
        f"permission denied opening {path} — on an active desktop session, logind "
        f"grants a per-seat ACL (see 'getfacl {path}'); a headless, containerized, or "
        "systemd-unit session receives no seat and will not get that ACL even though a "
        "human's desktop login would. Run this from an active graphical/logind seat, or "
        "add the invoking user to the 'video' group and re-login as a fallback. (checked "
        "without opening the device: the permission bits alone say this, --probe/--apply "
        "would fail the same way.)"
    )


def _paper_unknown_remediation(path: str) -> str:
    return (
        f"{path} exists and its permission bits look sufficient, but this is a dry-run "
        "and it never opens the device, so whether it is already held open by another "
        "process (EBUSY) genuinely cannot be determined here — pass --probe or --apply "
        "to find out for real"
    )


def _paper_state_for(path: str, kind: str) -> tuple[str, str]:
    """Non-opening access check: ``(state, remediation)`` without any ``os.open()`` call.

    ``state`` is one of ``"absent"`` (no node at ``path``), ``"forbidden"``
    (the node exists but the permission bits deny the access this ``kind``
    needs), or ``"unknown"`` (the node exists and looks permitted, but
    busy/EBUSY is not determinable without opening — see the section banner
    above). There is no ``"ok"``/``"busy"`` outcome here on purpose: this
    function never opens anything, so it can never actually confirm either.
    """
    if not os.path.exists(path):
        return "absent", _paper_absent_remediation(kind, path)
    needed = os.R_OK | os.W_OK if kind == "video" else os.R_OK
    if not os.access(path, needed):
        return "forbidden", _paper_forbidden_remediation(kind, path)
    return "unknown", _paper_unknown_remediation(path)


def _paper_report(path: str, kind: str) -> dict[str, object]:
    state, remediation = _paper_state_for(path, kind)
    return {"state": state, "path": path, "remediation": remediation}


def _paper_access_report(
    device: LogicalDevice, kind: str, capture_node: str | None
) -> dict[str, object | None]:
    """The no-flag dry-run's access block: same shape as :func:`_access_report`.

    Never calls :func:`webcam_cli.access.check_access` (or anything else that
    opens a device) — see the section banner above for why.
    """
    video_report = None
    audio_report = None
    if kind in ("video", "av") and capture_node is not None:
        video_report = _paper_report(capture_node, "video")
    if kind in ("audio", "av") and device.audio is not None:
        audio_path = _audio_node_path(device.audio)
        audio_report = _paper_report(audio_path, "audio")
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


def _start_phase_process(
    argv: list[str], factory: Callable[..., subprocess.Popen]
) -> subprocess.Popen:
    """Launch the child, translating a failed exec into the typed env-error contract."""
    try:
        return factory(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"failed to start {argv[0]}: {exc}",
            remediation=_INSTALL_HINT,
        ) from exc


def _bound_exceeded(
    elapsed_s: float, deadline_s: float, max_bytes: int | None, output_path: str | None
) -> str | None:
    """Which cap — if any — has just been crossed: ``"size"``, ``"duration"``, or ``None``.

    Size is checked first, matching the original single-function precedence:
    a phase that is already over ``max_bytes`` the instant the deadline also
    trips is still reported as a size stop.
    """
    size_hit = (
        max_bytes is not None
        and output_path is not None
        and _current_size(output_path) >= max_bytes
    )
    if size_hit:
        return "size"
    if elapsed_s >= deadline_s:
        return "duration"
    return None


def _terminate_phase(proc: subprocess.Popen, grace_s: float) -> tuple[str, str]:
    """SIGINT the child (gst-launch-1.0's documented clean-stop signal), then SIGKILL.

    Waits up to ``grace_s`` for the SIGINT to be honoured (EOS, finalize,
    exit) before escalating. This is what keeps the bound holding even
    against a child that ignores SIGINT outright: the wait here is itself
    bounded, and the follow-up ``kill()`` cannot be ignored by the target
    process.
    """
    proc.send_signal(signal.SIGINT)
    try:
        return proc.communicate(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()


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
    ``output_path`` reaches that size, :func:`_bound_exceeded` reports which
    cap tripped and :func:`_terminate_phase` sends SIGINT, escalating to
    SIGKILL after ``grace_s``. This holds even if the child never responds to
    SIGINT: the wall clock lives in this function, not the child, so it
    always returns within ``deadline_s + grace_s`` (plus a bounded
    ``communicate()`` after the kill, which cannot be ignored by the target
    process).

    ``popen_factory``/``clock`` default to :data:`subprocess.Popen`/
    :func:`time.monotonic`, looked up fresh on every call (not bound at
    import time) so tests can monkeypatch ``record.subprocess.Popen`` /
    ``record.time.monotonic`` directly, or inject fakes via the parameters.
    """
    proc = _start_phase_process(argv, popen_factory or subprocess.Popen)
    now = clock or time.monotonic

    start = now()
    reason = "completed"
    stdout = stderr = ""
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=poll_s)
            break
        except subprocess.TimeoutExpired:
            hit = _bound_exceeded(now() - start, deadline_s, max_bytes, output_path)
            if hit is None:
                continue
            reason = hit
            stdout, stderr = _terminate_phase(proc, grace_s)
            break

    returncode = proc.returncode if proc.returncode is not None else -1
    return _PhaseResult(
        returncode=returncode, stopped_reason=reason, stdout=stdout or "", stderr=stderr or ""
    )


def _require_artifact(
    output_path: str, result: _PhaseResult, *, busy_path: str | None = None
) -> int:
    """Confirm exactly the named artifact was written; never trust a return code alone.

    When the pipeline's own output says the capture device was already held,
    the caller gets the typed *busy* error naming the holder rather than a
    generic "produced no output". That mapping is necessary because V4L2
    exclusivity never reaches ``open(2)`` — see
    :func:`webcam_cli.access.busy_error`. ``busy_path`` is the video node to
    attribute a busy failure to; ``None`` for audio-only recordings, whose
    busy state ALSA already reports from ``require_access``.
    """
    engine_output = f"{result.stderr or ''}\n{result.stdout or ''}"
    if busy_path is not None and engine.output_reports_device_busy(engine_output):
        raise access.busy_error(busy_path, "video")

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


#: ``gst-launch-1.0 -e`` — "force EOS on sources before shutting the pipeline
#: down", which its own ``--help`` recommends "to make sure muxers create
#: readable files when a muxing pipeline is shut down forcefully via
#: Control-C". That is exactly this module's stop mechanism: every recording
#: ends because :func:`_terminate_phase` sends SIGINT at the bound, never
#: because the source ran out.
#:
#: Measured on the reference host, capturing until SIGINT and then reading the
#: container's own Duration back with ``gst-discoverer-1.0``:
#:
#: ==============  ===========  ==========  =====================
#: route           frames kept  without -e  with -e
#: ==============  ===========  ==========  =====================
#: MJPG 1280x960   578          19.224 s    19.298 s   (both fine)
#: VP8 640x480      63           0.731 s     4.267 s   (only -e is right)
#: ==============  ===========  ==========  =====================
#:
#: The frame count is identical either way — no samples are lost without it —
#: but on the VP8 route the Duration written into the Segment header is
#: nonsense, so the artifact still misreports itself to any player or
#: follow-on tool that reads it. The cost is bounded and small: finalizing a
#: 51 MB file took ~20 ms, far inside the ``_GRACE_S`` window after which
#: SIGINT escalates to SIGKILL, so the "never hangs" guarantee is unchanged.
#:
#: Not applied to the warm-up phase: it sinks to ``fakesink`` and has no
#: container to finalize.
_EOS_ON_SHUTDOWN = "-e"


def _with_eos_on_shutdown(argv: list[str]) -> list[str]:
    """Insert ``-e`` after the binary so SIGINT finalizes the container properly."""
    if _EOS_ON_SHUTDOWN in argv:
        return argv
    return [argv[0], _EOS_ON_SHUTDOWN, *argv[1:]]


def _warmup_pipeline(
    capture_node: str | None, video_fmt: engine.VideoFormat | None, warmup_s: float
) -> list[str] | None:
    """The pre-roll pipeline: the same source and caps, sunk straight to ``fakesink``.

    Built with ``mux=False`` on purpose. Warm-up exists to run the sensor
    until auto-exposure settles and *discard* every frame it produces, so
    appending the recording pipeline's container tail would encode pixels
    nothing will ever read — on the raw-video route that is a full VP8 encode
    per discarded frame. What warm-up has to reproduce faithfully is the
    negotiated source caps (the sensor settles at the resolution and frame
    rate it is about to record at), and that half is shared with the real
    phase because both come from the same builder and the same
    :class:`~webcam_cli.engine.VideoFormat`.
    """
    if warmup_s <= 0:
        return None
    return engine.build_video_pipeline(capture_node, video_fmt, "fakesink", mux=False)


def _build_pipelines(
    kind: str,
    capture_node: str | None,
    video_fmt: engine.VideoFormat | None,
    audio_address: str | None,
    audio_fmt: engine.AudioFormat | None,
    sink: str,
    warmup_s: float,
    cap: engine.Capability | None = None,
) -> tuple[list[str], list[str] | None]:
    if kind == "video":
        record_argv = engine.build_video_pipeline(capture_node, video_fmt, sink, cap=cap)
        warmup_argv = _warmup_pipeline(capture_node, video_fmt, warmup_s)
    elif kind == "audio":
        record_argv = engine.build_audio_pipeline(audio_address, audio_fmt, sink, cap=cap)
        warmup_argv = None
    else:  # av
        record_argv = engine.build_av_pipeline(
            capture_node, video_fmt, audio_address, audio_fmt, sink, cap=cap
        )
        warmup_argv = _warmup_pipeline(capture_node, video_fmt, warmup_s)
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
    warmup: float | None,
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

    # Detected *before* the preview is built, not after, so the preview is
    # checked against the same host facts --apply would use: a plan that names
    # an encoder this host does not have is a typed error at dry-run time
    # rather than a surprise at capture time. detect() probes with
    # `gst-inspect-1.0 --exists`, which opens no device, so this stays inside
    # the "a no-flag dry-run opens nothing" guarantee.
    cap = engine.detect()

    sink = f"filesink location={shlex.quote(output_path)}"
    pipeline_preview: list[str] | None = None
    if kind == "video" and planned_video is not None:
        pipeline_preview = engine.build_video_pipeline(capture_node, planned_video, sink, cap=cap)
    elif kind == "audio" and planned_audio is not None:
        pipeline_preview = engine.build_audio_pipeline(audio_address, planned_audio, sink, cap=cap)
    elif kind == "av" and planned_video is not None and planned_audio is not None:
        pipeline_preview = engine.build_av_pipeline(
            capture_node, planned_video, audio_address, planned_audio, sink, cap=cap
        )
    if pipeline_preview is not None:
        # Show the same behavioural flags --apply would pass, so the preview
        # is a promise about the run rather than an approximation of it. The
        # one thing it deliberately does not copy is _pin_executable's
        # absolute binary path: which gst-launch-1.0 gets resolved is an
        # environment fact, not part of the pipeline being previewed.
        pipeline_preview = _with_eos_on_shutdown(pipeline_preview)

    warmup_s = _resolve_warmup(warmup, kind, planned_video)

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
        "warmup_frames": _warmup_frames(warmup, kind, planned_video),
        "warmup_basis": _WARMUP_BASIS,
        "output_path": output_path,
        "would_write": [output_path],
        "access": (
            _access_report(device, kind, capture_node)
            if probe
            else _paper_access_report(device, kind, capture_node)
        ),
        "engine": {"available": cap.available, "gst_launch_present": cap.gst_launch is not None},
        "timestamps": {"resolved_at": _now_iso()},
    }


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _require_access_for_kind(device: LogicalDevice, kind: str, capture_node: str | None) -> None:
    """Raise the typed busy/forbidden error before any pipeline starts, not mid-recording."""
    if kind in ("video", "av"):
        access.require_access(capture_node, "video")
    if kind in ("audio", "av"):
        access.require_access(_audio_node_path(device.audio), "audio")


def _negotiate_apply_formats(
    kind: str,
    capture_node: str | None,
    requested_video: engine.VideoFormat | None,
    requested_audio: engine.AudioFormat | None,
) -> tuple[engine.VideoFormat | None, bool, engine.AudioFormat | None]:
    """Negotiate the real formats ``--apply`` will record at.

    Video is probed for real (unlike a plain dry-run) because ``--apply`` is
    about to energize the device anyway. Returns
    ``(negotiated_video, video_probed, negotiated_audio)``.
    """
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
    return negotiated_video, video_probed, negotiated_audio


@dataclass(frozen=True)
class _RecordRun:
    """What actually happened while running the (optional warm-up +) bounded record phase."""

    record_argv: list[str]
    result: _PhaseResult
    started_at: str
    warmup_started_at: str | None
    recording_started_at: str
    ended_at: str


def _run_record_phases(
    *,
    kind: str,
    capture_node: str | None,
    negotiated_video: engine.VideoFormat | None,
    audio_address: str | None,
    negotiated_audio: engine.AudioFormat | None,
    output_path: str,
    warmup_s: float,
    bound: Bound,
    cap: engine.Capability,
) -> _RecordRun:
    """Build the pipeline(s), run the warm-up phase (if any), then the bounded recording."""
    sink = f"filesink location={shlex.quote(output_path)}"
    record_argv, warmup_argv = _build_pipelines(
        kind,
        capture_node,
        negotiated_video,
        audio_address,
        negotiated_audio,
        sink,
        warmup_s,
        cap=cap,
    )
    gst_launch = cap.gst_launch
    record_argv = _with_eos_on_shutdown(_pin_executable(record_argv, gst_launch))
    if warmup_argv is not None:
        warmup_argv = _pin_executable(warmup_argv, gst_launch)

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

    return _RecordRun(
        record_argv=record_argv,
        result=result,
        started_at=started_at,
        warmup_started_at=warmup_started_at,
        recording_started_at=recording_started_at,
        ended_at=ended_at,
    )


def _apply_video_format_dict(
    kind: str,
    requested_video: engine.VideoFormat | None,
    negotiated_video: engine.VideoFormat | None,
    video_probed: bool,
) -> dict[str, object] | None:
    if kind not in ("video", "av"):
        return None
    return {
        "requested": _video_fmt_dict(requested_video),
        "negotiated": _video_fmt_dict(negotiated_video),
        "probed": video_probed,
    }


def _apply_audio_format_dict(
    kind: str,
    requested_audio: engine.AudioFormat | None,
    negotiated_audio: engine.AudioFormat | None,
) -> dict[str, object] | None:
    if kind not in ("audio", "av"):
        return None
    return {
        "requested": _audio_fmt_dict(requested_audio),
        "negotiated": _audio_fmt_dict(negotiated_audio),
        "probed": False,
    }


def _apply_body(
    act: activation.Activation,
    *,
    device: LogicalDevice,
    kind: str,
    capture_node: str | None,
    audio_address: str | None,
    requested_video: engine.VideoFormat | None,
    requested_audio: engine.AudioFormat | None,
    bound: Bound,
    warmup: float | None,
    output_path: str,
) -> dict[str, object]:
    """The activation-scoped body of ``record --apply``: negotiate, record, verify, report."""
    cap = engine.require_engine()
    _require_access_for_kind(device, kind, capture_node)

    negotiated_video, video_probed, negotiated_audio = _negotiate_apply_formats(
        kind, capture_node, requested_video, requested_audio
    )
    warmup_s = _resolve_warmup(warmup, kind, negotiated_video)

    run = _run_record_phases(
        kind=kind,
        capture_node=capture_node,
        negotiated_video=negotiated_video,
        audio_address=audio_address,
        negotiated_audio=negotiated_audio,
        output_path=output_path,
        warmup_s=warmup_s,
        bound=bound,
        cap=cap,
    )

    size = _require_artifact(
        output_path,
        run.result,
        busy_path=capture_node if kind in ("video", "av") else None,
    )

    act.detail.update(
        {
            "kind": kind,
            "output_path": output_path,
            "video_format": _video_fmt_dict(negotiated_video),
            "audio_format": _audio_fmt_dict(negotiated_audio),
            "bound": bound.as_dict(),
            "warmup_s": warmup_s,
            "bytes_written": size,
            "stopped_reason": run.result.stopped_reason,
        }
    )

    return {
        "mode": "apply",
        "apply": True,
        "device": device.as_dict(),
        "kind": kind,
        "capture_node": capture_node,
        "audio_address": audio_address,
        "video_format": _apply_video_format_dict(
            kind, requested_video, negotiated_video, video_probed
        ),
        "audio_format": _apply_audio_format_dict(kind, requested_audio, negotiated_audio),
        "bound": bound.as_dict(),
        "warmup_s": warmup_s,
        "warmup_frames": _warmup_frames(warmup, kind, negotiated_video),
        "warmup_basis": _WARMUP_BASIS,
        "output_path": output_path,
        "bytes_written": size,
        "stopped_reason": run.result.stopped_reason,
        "timestamps": {
            "started_at": run.started_at,
            "warmup_started_at": run.warmup_started_at,
            "recording_started_at": run.recording_started_at,
            "ended_at": run.ended_at,
        },
        "pipeline": run.record_argv,
    }


def _apply(
    *,
    device: LogicalDevice,
    kind: str,
    capture_node: str | None,
    audio_address: str | None,
    requested_video: engine.VideoFormat | None,
    requested_audio: engine.AudioFormat | None,
    bound: Bound,
    warmup: float | None,
    output_path: str,
) -> dict[str, object]:
    def _do(act: activation.Activation) -> dict[str, object]:
        return _apply_body(
            act,
            device=device,
            kind=kind,
            capture_node=capture_node,
            audio_address=audio_address,
            requested_video=requested_video,
            requested_audio=requested_audio,
            bound=bound,
            warmup=warmup,
            output_path=output_path,
        )

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
            warmup=args.warmup,
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
            warmup=args.warmup,
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
            f"settle. Default: {engine.DEFAULT_WARMUP_FRAMES} frames converted through the "
            f"negotiated fps ({_DEFAULT_WARMUP_VIDEO_S:g}s at "
            f"{engine.WARMUP_FPS_ASSUMPTION:g}fps) for video/av, "
            f"{_DEFAULT_WARMUP_AUDIO_S:g}s for audio-only. 0 is allowed -- this is a "
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
