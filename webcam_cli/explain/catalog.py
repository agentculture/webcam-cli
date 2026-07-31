"""Markdown catalog for ``webcam explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple,
``("webcam",)`` and the legacy ``("webcam-cli",)`` all resolve to the root entry
— an agent that guesses the distribution name still lands on the docs, even
though only ``webcam`` is ever typed.

Every path registered in :func:`webcam_cli.cli._build_parser` needs an entry
here; ``tests/test_cli.py`` walks the live parser tree and fails when one is
missing, so this file cannot silently fall behind the surface.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# webcam

Capture agent for the USB capture devices attached to this host — cameras *and*
microphones. It enumerates what is attached, reports honestly what can be opened
right now, and hands a consumer either a live stream or a bounded recorded file,
with metadata complete enough that nothing has to be re-derived. Interpreting
what is *in* a frame is a vision model's job, not this tool's.

Three names, one typable: the installed command is `webcam`, the import package
is `webcam_cli`, and the PyPI distribution is `webcam-cli`. Only `webcam` is
ever typed.

## Verbs

- `webcam list` — attached devices: stable id, nodes, paired microphone, access state.
- `webcam stream video|audio|av <device>` — serve a live attachment point (unbounded).
- `webcam record <device> <path>` — record a bounded clip to one file.
- `webcam whoami` — identity probe from `culture.yaml`.
- `webcam learn` — structured self-teaching prompt.
- `webcam explain <path>` — markdown docs for any noun/verb.
- `webcam overview` — descriptive snapshot of the agent.
- `webcam doctor` — check the agent-identity invariants.
- `webcam cli overview` — describe the CLI surface.

## What touches the hardware

`stream` and `record` share one three-level split, readable from the flags alone:

- **default (no flag)** — a dry run. Resolves the device, validates the request,
  prints the plan it would run. **No device is opened and nothing is logged.**
- **`--probe`** — enumerates the device's *real* formats. This **opens the
  camera**, so it is written to the activation log like any other activation.
- **`--apply`** — opens the device and streams or records. Written to the
  activation log.

`list` opens nothing beyond one non-blocking permission probe per node.

## Naming a device

Use the stable id from `/dev/v4l/by-id` (or a unique substring of it). A bare
`/dev/videoN` is refused on purpose: node numbers are plug order, and the two
cameras on the reference host have already swapped indices, so an index is not a
reproducible instruction. `webcam list --json` prints the id to use.

## Exit-code policy

- `0` success
- `1` user-input error (unknown device, unsatisfiable format, bad flag)
- `2` environment error (no capture engine, forbidden device) — not retryable
  without a config/environment fix
- `3` device busy (EBUSY) — retryable; another process holds the device, and
  waiting for it to release (or stopping it) is enough, no fix needed. Kept
  distinct from `2` on purpose: an agent that gets the same code for both
  cannot tell "wait and retry" from "this will never work" without
  string-matching the message.
- `4+` reserved

## Consent

Every activation is appended to the activation log — by default
`~/.local/state/webcam-cli/activations.jsonl` (XDG state dir; override with
`$WEBCAM_ACTIVATION_LOG`) — and a capture writes only to the path you name, with
no hidden buffer and never to stdout.

A hardware activity light **cannot** be promised: that is device firmware,
outside this tool's control. This tool records activations; it does not prevent
covert use, and nothing here should be read as claiming otherwise.

## See also

- `webcam explain list`
- `webcam explain stream`
- `webcam explain record`
"""

_LIST = """\
# webcam list

Every logical capture device attached to this host, with the access state of
each subsystem it offers. Read-only: no capture, no format enumeration, no
engine call. The only hardware touch is one non-blocking `open()`/`close()` per
node — that pair *is* the permission probe.

## Usage

    webcam list
    webcam list --json
    webcam list --root PATH    # resolve under a synthetic device tree (tests)

## What one entry means

- **`stable_id`** — the `/dev/v4l/by-id` handle, built by udev from the USB
  vendor/product/serial descriptors. This is the device's identity and the
  selector every other verb accepts.
- **`video_nodes`** — every `/dev/video*` node the device publishes. A UVC
  camera commonly publishes more than one, and only one yields frames, so
  counting `/dev/video*` overcounts cameras.
- **`capture_node`** — the node believed to yield frames, reported alongside
  `capture_node_is_heuristic: true`. This kernel exposes no `device_caps` in
  sysfs, so which node actually yields frames cannot be known without opening
  it. The flag is there so a guess is never presented as a fact.
- **`audio`** — the ALSA capture card sharing this device's USB parent, or
  `null`. Video and audio identifiers are unrelated; the only link is USB
  topology, and A/V sets are not 1:1 (a microphone with no camera enumerates as
  a device with no video nodes).
- **`video_access` / `audio_access`** — reported **separately**, never folded
  into one status, because the two subsystems fail for different reasons.

## Access states

`ok`, `absent`, `forbidden`, `busy` — each carrying its own `remediation`.

*absent* and *present-but-forbidden* are deliberately distinct: reporting a
forbidden camera as missing turns into "the agent says there is no camera" when
there plainly is one. On a desktop host, video access comes from a per-seat ACL
granted by logind — **not** from `video`-group membership — so a headless,
containerized, or systemd-unit agent receives no seat and loses every camera
while keeping its microphones, which are gated by the `audio` group instead.
`forbidden` names the fix for the subsystem that actually failed.

`list` never fails because a device is unhappy: an absent, forbidden, or busy
device is listed with that state and `list` still exits 0. Only capture verbs
turn a bad device into a non-zero exit.

## Scope limit

USB only. A capture card with no USB parent in sysfs (an analog line-in, a PCIe
capture card) has no USB descriptors to derive a stable identity from and cannot
be paired by topology, so it is skipped — "not listed" is not the same as "not
present".
"""

_STREAM = """\
# webcam stream

Serve a **live** attachment point another process can consume. Three media
sub-verbs plus the noun's own overview:

    webcam stream video <device>   # camera only
    webcam stream audio <device>   # that device's own microphone only
    webcam stream av <device>      # both, muxed into one container
    webcam stream overview         # describe this verb group

Bare `webcam stream` prints the overview.

## What each invocation touches

- **default (dry-run)** — nothing. Resolves the device, checks the request
  structurally, prints the plan it would run. Nothing is opened or logged.
- **`--probe`** — enumerates the device's real formats via
  `gst-device-monitor-1.0`, which **opens the camera**; written to the
  activation log.
- **`--apply`** — opens the device and streams. Implies `--probe` (a stream must
  negotiate against the enumerated set), applies warm-up, emits the attachment
  payload, then holds the device until stopped.

## Attachment

A GStreamer `tcpserversink` bound to `127.0.0.1` serves Matroska-contained
media. `--port N` sets the port (default 5000; `--port 0` auto-picks a free
one). Caps travel inside the container, so a consumer attaches with
`tcpclientsrc ! matroskademux ! …` — or plain `decodebin` — and never has to
reconstruct caps by hand. The `--json` payload carries the uri, the container,
the negotiated caps, and a ready-to-run consumer command; a follow-on tool needs
nothing else.

Loopback only, never a routable address, and there is deliberately no `--host`
flag. The honest cost of TCP-on-loopback: any process on this host that can
connect to the port can read the stream while it is live. This tool does not
authenticate consumers and does not claim the stream is private.

## Unbounded by construction

A stream has no duration cap and no `--duration` flag: it runs until the process
is stopped (SIGINT/SIGTERM) or the pipeline exits. Oversight comes from the
activation log, not from a timer. `webcam record` is the deliberate opposite —
bounded by construction — and the two must not be blurred.

## Warm-up

The first frames off a UVC sensor are unsettled while auto-exposure converges,
so the pipeline runs for a warm-up interval *before* the attachment point is
announced, with `tcpserversink` dropping buffers while no client is connected.
Defaults: **30 frames** for video and av (converted through the negotiated fps
— 1.0 s at 30 fps, 6.0 s at 5 fps) and **200 ms** for audio. Override with
`--warmup-frames N` or `--warmup-ms MS`; `--warmup-frames 0` disables it.

The frame count is measured, not guessed: on the reference C270 auto-exposure
settled after 13-15 frames at 30 fps *and* at 5 fps, so settle tracks frames
rather than wall-clock time, and a fixed-seconds default would under-warm at
low frame rates. 30 is about 2x the slowest settle seen; that margin is
deliberate and is not itself measured, since every run was under one indoor
lighting condition. `webcam record` derives its default from the same constant,
so the two verbs cannot drift apart.

The guarantee is the announcement ordering, not an enforced gate: a consumer
that connects before the announcement (by guessing the port) can still observe
pre-settle frames.

What is *not* measured is the margin. Every settle run was under a single
indoor lighting condition; a darker scene should be expected to converge more
slowly, so 30 frames is a 2x cushion over what was observed rather than a
characterization. Pass `--warmup-frames` if you know your conditions.

## Format negotiation

An unsatisfiable `(format, resolution, fps)` is a typed user error naming the
enumerated alternatives. This tool never substitutes a format the caller did not
ask for. Omit the video flags entirely to let negotiation choose from the
device's enumerated set.

## See also

- `webcam explain stream video`
- `webcam explain stream audio`
- `webcam explain stream av`
- `webcam explain record`
"""

_STREAM_OVERVIEW = """\
# webcam stream overview

Describe the `stream` verb group to an agent reader: its sub-verbs, which
invocations energize hardware, how the attachment point works, the warm-up
defaults, and the contracts (unbounded by construction, typed negotiation
failures, logged activations). Read-only; touches no device.

Bare `webcam stream` prints the same thing.

## Usage

    webcam stream overview
    webcam stream overview --json

The `--json` form emits the stable `{subject, sections}` shape shared with
`webcam overview` and `webcam cli overview`.

## See also

- `webcam explain stream`
"""

_STREAM_VIDEO = """\
# webcam stream video

Serve a live **camera** stream from one device. Dry-run by default.

## Usage

    webcam stream video <device>                     # plan only, opens nothing
    webcam stream video <device> --probe --json      # OPENS the camera to enumerate
    webcam stream video <device> --apply             # opens and streams

`<device>` is a stable id from `/dev/v4l/by-id`, a unique substring of one, or a
`by-id` path. A bare `/dev/videoN` is refused: node numbering is plug-order, not
identity.

## Flags

- `--format FOURCC`, `--width PX`, `--height PX`, `--fps N` — constrain the
  request. Anything omitted is left for negotiation rather than filled with a
  hidden default. An unsatisfiable combination is a typed user error naming the
  enumerated alternatives; there is no silent fallback.
- `--encode {passthrough,vp8}` — `passthrough` (default) serves the device's own
  format untouched; `vp8` re-encodes and requires the `vp8enc` element. H.264 is
  not offered because `x264enc` is absent on the reference host, and this tool
  probes for elements rather than assuming them.
- `--port N` — loopback port (default 5000; `0` auto-picks a free one).
- `--warmup-frames N` / `--warmup-ms MS` — sensor warm-up before the attachment
  point is announced. Default 30 frames, converted through the negotiated fps
  (1.0 s at 30 fps, 6.0 s at 5 fps) and measured on the reference C270;
  `--warmup-frames 0` disables it.
- `--probe`, `--apply`, `--json`.

## Output

The `--json` payload names the device (stable id *and* the ephemeral node), the
requested and negotiated format, the attachment uri and container, the exact
`gst-launch-1.0` pipeline, the warm-up actually applied, the access state, and
the consent block (activation-log path, what was written where). A dry run says
`hardware_touched: false`.

Unbounded: stop with SIGINT/SIGTERM. Use `webcam record` for a bounded file.
"""

_STREAM_AUDIO = """\
# webcam stream audio

Serve a live **microphone** stream from one device, straight off ALSA. Dry-run
by default.

## Usage

    webcam stream audio <device>            # plan only, opens nothing
    webcam stream audio <device> --apply    # opens and streams

The device is selected by the same stable id as its camera; the microphone is
the ALSA capture card sharing that device's USB parent. A device with no paired
microphone is a typed user error naming `webcam list --json` — A/V sets are not
1:1, and a camera with no mic is an ordinary outcome, not a bug.

## Flags

- `--rate HZ` (default 48000) and `--channels N` (default 1 — the C270's onboard
  mic is mono). Applied as an **exact caps filter**: an unsupported pair fails
  loudly when the pipeline starts rather than being silently substituted. There
  is no ALSA capability probe to validate against beforehand, and the payload
  says so (`negotiation.status: unvalidated`) instead of implying otherwise.
- `--encode {passthrough,opus}` — `passthrough` (default) serves raw PCM inside
  Matroska; `opus` requires the `opusenc` element.
- `--port N`, `--warmup-ms MS` (default 200 ms — ring-buffer fill, not exposure
  settling), `--probe`, `--apply`, `--json`.

## Access

Microphone access is gated by `audio`-group membership, not by the logind seat
ACL that governs `/dev/video*`. A headless agent that has lost every camera
usually still has its microphones; `forbidden` names that fix specifically.

Unbounded: stop with SIGINT/SIGTERM.
"""

_STREAM_AV = """\
# webcam stream av

Serve one device's **camera and its own microphone**, muxed into a single
Matroska stream. Dry-run by default.

## Usage

    webcam stream av <device>            # plan only, opens nothing
    webcam stream av <device> --apply    # opens both and streams

Pairing is by USB topology, not by number: the camera is a `/dev/video*` node,
the microphone an ALSA card, and nothing in either numbering connects them.
A device missing either half is a typed user error pointing at
`webcam stream video` or `webcam stream audio` instead.

## Flags

Accepts the video flags (`--format/--width/--height/--fps`), the audio flags
(`--rate/--channels`), `--port`, the warm-up flags, `--probe`, `--apply`, and
`--json`.

**Passthrough only.** There is no `--encode` here: encoded A/V needs per-branch
pipeline support the engine does not expose. For an encoded single medium use
`webcam stream video --encode` or `webcam stream audio --encode`.

## Consumer

The `--json` payload's consumer command demuxes both branches:

    gst-launch-1.0 tcpclientsrc host=127.0.0.1 port=5000 ! matroskademux name=demux \\
      demux. ! videoconvert ! fakesink sync=false \\
      demux. ! audioconvert ! fakesink sync=false

Unbounded: stop with SIGINT/SIGTERM. Use `webcam record --kind av` for a file.
"""

_RECORD = """\
# webcam record

Record a **bounded** video, audio, or muxed A/V clip from one capture device to
one file. Dry-run by default.

## Usage

    webcam record <device> <output-path>                  # plan only
    webcam record <device> clip.mkv --apply               # actually record
    webcam record <device> mic.mka --kind audio --apply
    webcam record <device> both.mkv --kind av --duration 5 --apply

## What each invocation touches

- **default (dry-run)** — resolves the device, checks access with a non-blocking
  probe, structurally validates the request against the same pure pipeline
  builders a real capture uses, and reports the single path it *would* write.
  **No hardware is energized and nothing is logged.**
- **`--probe`** — dry-run only: really enumerates the device's video formats,
  which **opens the camera**; written to the activation log. Accepted and
  ignored alongside `--apply`, which always negotiates for real.
- **`--apply`** — opens the device and records. Written to the activation log.

## Bounded by construction

A duration cap **always** applies: `--duration SECONDS` when given, otherwise a
default of 30 s, with a hard ceiling of 3600 s. There is deliberately no
`--forever` or `--no-limit`, and `0`, negative values, `inf` and `nan` are all
rejected before a bound is constructed — "unbounded" is not expressible here.
Use `webcam stream` when an unbounded live feed is what you want.

`--max-bytes B` layers an **additional** size cap on top; omitting it never
removes the duration cap. Bounding is enforced by supervising the pipeline
process from outside it, so the worst case is bounded even if the child ignores
its stop signal.

## Exactly one artifact

The sink is always a plain `filesink` at the path you named — never a splitting
sink — and the file is verified to exist and be non-empty before success is
reported. A pipeline that dies silently is a typed environment error, not a
phantom success. Warm-up frames go to `fakesink` and are written nowhere.

## Output container

Always **Matroska**, whatever you name the file — `.mkv` for video and av,
`.mka` for audio-only, by convention rather than by enforcement. The clip is a
container, not a bare byte stream, so it carries its own geometry, frame rate
and sample rate and a consumer never has to be told them out of band.

The codec follows the *negotiated source format* rather than a fixed house
choice:

- **video, MJPG negotiated** — carried through as Motion JPEG. Passthrough: the
  camera's own hardware JPEG, no re-encode and no quality loss.
- **video, a raw pixel format (YUYV and friends)** — encoded to VP8. H.264 is
  not offered because `x264enc` is absent on the reference host and this tool
  probes for elements rather than assuming them.
- **audio** — encoded to Opus. Opus constrains the request: `--rate` must be
  one of 8000, 12000, 16000, 24000 or 48000 Hz and `--channels` at most 8.
  Anything else is a typed user error naming the set, never a silent resample
  to a rate you did not ask for and the payload does not report.
- **`--kind av`** — both branches muxed into one Matroska file **as the device
  delivers them**, with no per-branch re-encode. An encoded A/V variant would
  need per-branch pipeline support this surface does not expose; use
  `webcam stream video --encode` / `webcam stream audio --encode` for an
  encoded single medium.

Every element this needs is capability-gated like the rest of the engine: a
missing encoder or muxer is a typed environment error naming the package to
install, never a silent fallback to some other format.

## Format

`--pixel-format/--width/--height/--fps` must be given together or not at all
(video/av); `--rate/--channels` likewise for audio (defaults 48000 Hz, 1
channel). An unsupported combination is a typed user error naming the
alternatives, never a silent fallback. Video flags with `--kind audio` (and vice
versa) are refused rather than ignored. `--kind audio` is additionally
constrained by the Opus encoder it is stored with — see **Output container**.

## Warm-up

`--warmup SECONDS` discards a pre-roll while auto-exposure settles, run as a
separate first phase sunk to `fakesink`. The default is **30 frames converted
through the negotiated fps** — 1.0 s at 30 fps, 6.0 s at 5 fps — and **0.0 s**
for audio-only, since a microphone has no exposure to settle. `--warmup 0` is
allowed: a pre-roll of zero is finite, unlike the recording bound.

The frame count is measured on the reference C270 (13-15 frames to settle at
30 fps *and* at 5 fps, so settle tracks frames rather than wall-clock time) and
is the *same constant* `webcam stream` uses, so the two verbs cannot disagree
as they did while both were guesses. `--json` reports `warmup_s`,
`warmup_frames`, and `warmup_basis` for every run.

## Other flags

`--root PATH` resolves the device under a synthetic tree (tests). `--json` emits
the full report: device identity, requested versus negotiated formats, the
bound, the warm-up, the output path, bytes written, and why the recording
stopped (`completed`, `duration`, or `size`).
"""

_WHOAMI = """\
# webcam whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only, and it touches no capture
hardware whatsoever.

The nick is `webcam-cli` — the project and distribution name — while the command
you type stays `webcam`.

## Usage

    webcam whoami
    webcam whoami --json
"""

_LEARN = """\
# webcam learn

Prints a structured self-teaching prompt covering purpose, the command map,
which invocations energize hardware, how devices are named, the bounded/
unbounded split between `record` and `stream`, the exit-code policy, `--json`
support, and the `explain` pointer.

Start here before invoking anything else: it is where the dry-run / `--probe` /
`--apply` rule is stated.

## Usage

    webcam learn
    webcam learn --json
"""

_EXPLAIN = """\
# webcam explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path, so a nested verb is
one call away without walking the parser.

## Usage

    webcam explain                     # root entry
    webcam explain webcam              # same
    webcam explain list
    webcam explain stream video
    webcam explain --json <path>

An unknown path exits 1 with a `hint:` line.
"""

_OVERVIEW = """\
# webcam overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, which invocations energize hardware, the identity/access contracts
the capture verbs obey, and the consent posture with its limits stated. Accepts
an ignored `target` argument so a stray path never hard-fails.

`webcam cli overview` is the sibling that describes the CLI surface itself, and
`webcam stream overview` describes the `stream` noun.

## Usage

    webcam overview
    webcam overview --json
"""

_DOCTOR = """\
# webcam doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`colleague` → `AGENTS.colleague.md`),
plus a skills-present check. Exits 1 when unhealthy.

This is an *identity* check, not a capture-readiness check: it says nothing about
whether a camera is attached, whether a capture engine is installed, or whether
this process can open a device. Use `webcam list` for that.

## Usage

    webcam doctor
    webcam doctor --json
"""

_CLI = """\
# webcam cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself — its verbs and the conventions every one of them obeys — as distinct
from the global `overview`, which describes the agent.

## Usage

    webcam cli overview
    webcam cli overview --json
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("webcam",): _ROOT,
    # Legacy alias: the distribution name, kept resolvable so a guess still
    # lands on the docs. Never advertised as something to type.
    ("webcam-cli",): _ROOT,
    ("list",): _LIST,
    ("stream",): _STREAM,
    ("stream", "overview"): _STREAM_OVERVIEW,
    ("stream", "video"): _STREAM_VIDEO,
    ("stream", "audio"): _STREAM_AUDIO,
    ("stream", "av"): _STREAM_AV,
    ("record",): _RECORD,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
}
