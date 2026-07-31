"""``webcam learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.

Beyond the rubric, this text carries the one fact an agent needs *before* it
invokes anything here: which invocations switch a camera or microphone on. A
tool that can open a camera is a surveillance surface, so "did that touch the
hardware?" must be answerable from the surface alone, and the answer is stated
here first rather than buried in a verb's ``--help``.
"""

from __future__ import annotations

import argparse

from webcam_cli import __version__, activation
from webcam_cli.cli._output import emit_result

_PURPOSE = (
    "Own the local USB capture devices — cameras and microphones — and the act of "
    "getting frames and samples off them: enumerate what is attached, report honestly "
    "what can be opened, then hand a consumer a live stream or a bounded recorded file "
    "with metadata complete enough that nothing has to be re-derived."
)

_TEXT = """\
webcam — enumerate, stream, and record the local USB cameras and microphones.

The installed command is `webcam`. (The project and PyPI distribution are named
webcam-cli and the import package is webcam_cli; neither is ever typed.)

Purpose
-------
Own the local USB capture devices, video and audio, and the act of getting
frames and samples off them: enumerate what is attached, report honestly what
each device can do and whether it can be opened right now, then hand a second
process either a live stream or a bounded recorded file — with metadata complete
enough that the consumer never has to re-derive what was captured or from where.
Interpreting what is *in* a frame is a vision model's job, not this tool's.

What touches the hardware — read this before invoking anything
--------------------------------------------------------------
`stream` and `record` share one three-level split, and which level you are on is
readable from the flags alone:

  (no flag)  Dry run. Resolves the device, validates the request, prints the
             plan it would run. Opens no device. Logs nothing.
  --probe    Enumerates the device's real formats. This OPENS the camera, and is
             itself written to the activation log.
  --apply    Opens the device and streams or records. Written to the activation
             log. For `stream`, implies --probe.

`list` opens nothing beyond a single non-blocking permission probe per node.

Commands
--------
  webcam list                    Attached devices: stable id, nodes, mic, access.
  webcam stream overview         Describe the stream verb group.
  webcam stream video <device>   Live camera attachment point (unbounded).
  webcam stream audio <device>   Live microphone attachment point (unbounded).
  webcam stream av <device>      Camera plus its own mic, muxed (unbounded).
  webcam record <device> <path>  Bounded clip to one file (--kind video|audio|av).
  webcam whoami                  Identity from culture.yaml.
  webcam learn                   This self-teaching prompt.
  webcam explain <path>...       Markdown docs for any noun/verb path.
  webcam overview                Descriptive snapshot of the agent.
  webcam doctor                  Check the agent-identity invariants.
  webcam cli overview            Describe the CLI surface itself.

Naming a device
---------------
Pass the stable id from /dev/v4l/by-id (or a unique substring of it), never
/dev/videoN: node numbers are plug order and the reference host's two cameras
have already swapped, so an index is not a reproducible instruction. `webcam
list --json` prints the id to use. Access failures distinguish absent from
present-but-forbidden and name the fix per subsystem — a headless agent
typically loses every camera (no logind seat ACL) while keeping its microphones
('audio' group).

Bounds
------
`record` is bounded by construction: a duration cap always applies (default 30s,
ceiling 3600s) and no flag combination expresses "forever". `stream` is
unbounded by construction: there is no --duration; stop it with SIGINT/SIGTERM.

What comes out
--------------
Every capture path produces Matroska — a container, not a bare byte stream — so
the artifact carries its own geometry, frame rate and sample rate and you never
have to be told them out of band. `record` picks the codec from the negotiated
source format: an MJPG camera format is carried through as Motion JPEG
(passthrough, no re-encode), a raw pixel format such as YUYV is encoded to VP8,
audio is encoded to Opus (so --rate must be one Opus carries: 8000, 12000,
16000, 24000 or 48000 Hz, and --channels at most 8), and --kind av muxes both
branches as the device delivers them.
`stream` serves the same container over tcpserversink, with --encode choosing
between the device's own format and VP8/Opus on the wire. A missing encoder or
muxer is a typed environment error naming what to install, never a silent
fallback to some other format.

Warm-up
-------
The first frames off a UVC sensor are unsettled while auto-exposure converges,
so both verbs discard a pre-roll before any frame reaches you. The default is
30 frames, converted through the negotiated fps (1.0s at 30fps, 6.0s at 5fps);
`stream audio` adds 200ms of ring-fill, and audio-only recording warms up not at
all. Override with --warmup-frames/--warmup-ms (stream) or --warmup (record); 0
disables. The frame count is measured on this project's reference C270 — settle
held at 13-15 frames at both 30fps and 5fps, so it tracks frames rather than
seconds — with a 2x margin that is deliberate and not itself measured.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, unknown device, unsatisfiable format)
  2 environment error (no capture engine, forbidden device) — not retryable
    without a config/environment fix
  3 device busy (EBUSY) — retryable; another process holds the device
  4+ reserved

Consent
-------
Every activation is appended to the activation log (default
~/.local/state/webcam-cli/activations.jsonl; override $WEBCAM_ACTIVATION_LOG),
and a capture writes only to the path you name — no hidden buffer, never to
stdout. A hardware activity light CANNOT be promised: that is device firmware,
outside this tool's control. This tool records activations;
it does not prevent covert use.

More detail
-----------
  webcam explain webcam
  webcam explain list
  webcam explain stream
  webcam explain record
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "webcam-cli",
        "command": "webcam",
        "import_package": "webcam_cli",
        "version": __version__,
        "purpose": _PURPOSE,
        "commands": [
            {
                "path": ["list"],
                "summary": "Attached capture devices: stable id, nodes, paired mic, access.",
            },
            {"path": ["stream"], "summary": "Live attachment points (noun group)."},
            {"path": ["stream", "overview"], "summary": "Describe the stream verb group."},
            {"path": ["stream", "video"], "summary": "Serve a live camera stream."},
            {"path": ["stream", "audio"], "summary": "Serve a live microphone stream."},
            {"path": ["stream", "av"], "summary": "Serve camera plus its own mic, muxed."},
            {"path": ["record"], "summary": "Record a bounded clip to one file."},
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
            {"path": ["cli"], "summary": "CLI-surface introspection (noun group)."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
        ],
        "hardware_activation": {
            "default": "dry run — resolves and validates, opens no device, logs nothing",
            "--probe": "enumerates real formats; OPENS the camera; written to the activation log",
            "--apply": "opens the device and streams/records; written to the activation log",
            "list": "opens nothing beyond one non-blocking permission probe per node",
        },
        "device_selector": (
            "the stable id from /dev/v4l/by-id (or a unique substring); a bare /dev/videoN "
            "is refused because node numbering is plug-order, not identity"
        ),
        "bounds": {
            "record": "bounded by construction — a duration cap always applies (default 30s, "
            "ceiling 3600s); no flag means 'forever'",
            "stream": "unbounded by construction — there is no --duration; stop with "
            "SIGINT/SIGTERM",
        },
        "output_format": {
            "container": "matroska",
            "record_video_mjpg": "carried through as Motion JPEG — passthrough, no re-encode",
            "record_video_raw": "a raw pixel format (YUYV and friends) is encoded to VP8; "
            "H.264 is not offered because x264enc is absent on the reference host",
            "record_audio": "encoded to Opus, so --rate must be one Opus carries "
            "(8000/12000/16000/24000/48000 Hz) and --channels at most 8",
            "record_av": "both branches muxed as the device delivers them, no per-branch "
            "re-encode",
            "stream": "the same container over tcpserversink, with --encode choosing "
            "between the device's own format and VP8/Opus on the wire",
            "gating": "a missing encoder or muxer is a typed environment error naming what "
            "to install, never a silent fallback to another format",
        },
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment error — not retryable without a config fix",
            "3": "device busy (EBUSY) — retryable",
        },
        "consent": {
            "activation_log": str(activation.log_path()),
            "activation_log_env": activation.ENV_LOG_PATH,
            "bytes_written": "only to the path named on the command line — no hidden buffer",
            "activity_light": (
                "cannot be promised — a hardware activity LED is device firmware, outside "
                "this tool's control; this tool records activations, it does not prevent "
                "covert use"
            ),
        },
        "json_support": True,
        "explain_pointer": "webcam explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
