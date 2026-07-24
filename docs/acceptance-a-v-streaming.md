# On-host acceptance: A/V streaming

Task **t9** of the `a-v-streaming` build plan. This is the evidence artifact
for the one task in that plan authorised to open real hardware — every earlier
task was built and tested hardware-free by operator decision (deviation `d1`).

Everything below was run on the operator's host on **2026-07-24**, headless
(`tty` reported `not a tty`), against the Logitech C270 **only**. The Arducam
and the Reachy Mini microphone were never opened: out of scope, confirmed
boundary claim `c27`.

Reproduce with:

```bash
scripts/acceptance/run.sh                    # the acceptance run itself
scripts/acceptance/run.sh --measure-warmup   # plus the settle-curve measurement
```

## Privacy posture of this run

Every media artifact was written to a `mktemp -d` directory under `$TMPDIR`,
never inside the repository or the worktree, and deleted on exit by the
script's `EXIT` trap — including on failure. What survives here is byte counts,
frame counts, durations, decoder caps and timings. No frame, still, thumbnail
or audio sample is reproduced in this document, in any test fixture, or in any
commit. Total camera-on time across the whole task was on the order of a
minute, in bursts of 2-8 seconds.

A hardware activity light still cannot be promised — that is device firmware.
What this run *does* verify is the other half of the claim: every activation
landed in the activation log, and the log is now complete enough to audit (see
[finding 4](#finding-4--the-activation-log-silently-dropped-every-late-fact)).

## Before-state

`main` is at **`52aa9fd`** — *"Merge pull request #2 from
agentculture/docs/init-runtime-prompt"*, 2026-07-24. Verified, not asserted;
step 0 of the acceptance script re-checks it on every run and fails if it ever
stops being true:

```console
$ git ls-tree -r --name-only 52aa9fd -- webcam_cli
webcam_cli/__init__.py
webcam_cli/__main__.py
webcam_cli/cli/__init__.py
webcam_cli/cli/_commands/__init__.py
webcam_cli/cli/_commands/cli.py
webcam_cli/cli/_commands/doctor.py
webcam_cli/cli/_commands/explain.py
webcam_cli/cli/_commands/learn.py
webcam_cli/cli/_commands/overview.py
webcam_cli/cli/_commands/whoami.py
webcam_cli/cli/_errors.py
webcam_cli/cli/_output.py
webcam_cli/explain/__init__.py
webcam_cli/explain/catalog.py
```

No `devices.py`, `access.py`, `engine.py`, `activation.py`, `list_devices.py`,
`stream.py` or `record.py`. The six template verbs and the error/output
contract, and nothing else: at the before-state this repository could not
enumerate a camera, let alone open one.

## Host

| | |
|---|---|
| GStreamer | 1.24.2 (`gst-launch-1.0`, `gst-inspect-1.0`, `gst-device-monitor-1.0`) |
| Absent | `ffmpeg`, `v4l2-ctl`, `fswebcam` |
| Device provider | **PipeWire's**, which hides GStreamer's own `v4l2deviceprovider` |
| Target | `usb-046d_C270_HD_WEBCAM_200901010001` |
| Resolved to | `/dev/video0` (2 nodes), `hw:CARD=WEBCAM,DEV=0` (`/dev/snd/pcmC1D0c`) |
| Access | video `ok`, audio `ok` (logind seat ACL present in this session) |

## The blind-consumer contract

The criterion: *a process that has never inspected this host must be able to
consume the stream using nothing but the `--json` payload.*

That is enforced structurally rather than promised.
`scripts/acceptance/blind-consumer.sh` takes exactly one argument — the path to
the payload file. It never runs `webcam`, never reads `/dev`, `/sys` or
`/proc/asound`, and is never told the device's stable id. Its helper,
`scripts/acceptance/payload.py`, does not import `webcam_cli`.

**Result: it worked from the payload alone. Nothing was needed that the payload
did not announce** — after the two defects in [finding 2](#finding-2--no-consumer-could-decode-an-av-stream-at-all)
and [finding 1](#finding-1--format-probing-was-broken-on-this-host-nothing-hardware-facing-worked-at-all)
were fixed. Before those fixes the contract failed outright, and both failures
are reported below rather than papered over.

`webcam stream av <stable-id> --apply --port 5000 --json` announced:

```text
uri        tcp://127.0.0.1:5000
container  matroska
negotiated MJPG 1280x960 @ 30 fps
warm-up    1000.0 ms / 30 frames
consumer   gst-launch-1.0 tcpclientsrc host=127.0.0.1 port=5000 \
             ! matroskademux name=demux \
               demux. ! queue ! jpegdec ! videoconvert ! fakesink sync=false \
               demux. ! queue ! audioconvert ! fakesink sync=false
```

Five announced attachment forms were exercised, each verbatim from the payload:

| # | What was run | Result |
|---|---|---|
| 1 | `attach.consumer.gst_launch_str`, verbatim | ran clean for 4 s against the live stream |
| 2 | `attach.consumer.generic` (`decodebin`), verbatim | ran clean for 4 s |
| 3 | `attach.consumer.raw_socket` — a bare TCP socket to the announced host/port | first 4 bytes were `1a 45 df a3`, the EBML magic the payload names |
| 4 | the announced command, instrumented with `identity` counters | **video: 60 buffers, `video/x-raw` I420 1280x960; audio: 400 buffers, `audio/x-raw` S16LE 48000 Hz mono** — over 4 s, live |
| 5 | `attach.consumer.save_to_file`, verbatim | captured **6 050 748 bytes** in 4 s; decoding those bytes with the announced chain yielded 60 video and 399 audio buffers |

Row 4 is the live decode the criterion asks for. It is the announced command
with `identity silent=false` spliced in front of each `fakesink` and `-v` added
— a mechanical string substitution on the announced command, needed only
because `fakesink` reports no counts. Row 1 runs the unmodified command
separately, so the contract check and the measurement are not the same run.

The table above is one run. The script was run end-to-end twice on the final
tree, and the two agreed to within a few percent on every figure (6 050 748 vs
6 187 016 captured bytes; identical buffer counts to ±1).

### `list` → `record`, same stable id, same JSON-only path

```text
dry run   wrote nothing; would_write = [<tmp>/clip.mkv]
apply     4 153 600 bytes, stopped_reason=duration
          video MJPG 1280x960@30, audio 48000 Hz mono
          warm-up 1.0 s / 30 frames
decode    40 video frames + 259 audio buffers
```

Exactly one artifact, non-empty, decodable. The dry run wrote nothing, as the
three-level hardware split promises.

## Busy-device probe (criterion 3)

While the `av` stream held the C270, a second `--apply` was attempted against
the same device:

| medium | exit | elapsed | error |
|---|---|---|---|
| video | 2 | **1.36 s** | `video device /dev/video0 is busy (held by gst-launch-1.0, pid 809029)` |
| audio | 2 | **0.31 s** | `audio device /dev/snd/pcmC1D0c is busy (held by gst-launch-1.0, pid 809029)` |

Both are the typed busy error, both name the holding process, both are bounded
well under two seconds, and neither hangs. The video case only behaves this way
because of [finding 3](#finding-3--v4l2-busy-was-not-the-typed-busy-error); as
shipped it produced a generic "pipeline exited during warm-up" instead.

## r3 — the warm-up defaults, measured

`stream` used 30 frames of video warm-up (~1 s at 30 fps) and 200 ms of audio;
`record` used a flat 2.0 s of video and 0.0 s of audio. Nobody had measured the
C270, so the two disagreed for no defensible reason.

**Method.** Open the camera with warm-up disabled, capture a burst of MJPEG
frames, decode each to GRAY8, take the mean luma per frame, and call it settled
at the first frame after which mean luma stays within 2% of its final value for
the rest of the burst. Each burst is preceded by 100 s of idle so the sensor is
genuinely cold — a warm sensor keeps its converged exposure and shows almost no
ramp, which understates what a stream's first consumer sees. Reproducible with
`scripts/acceptance/warmup-measure.py`.

| run | resolution | fps | settle (frames) | settle (wall clock) | final mean luma |
|---|---|---|---|---|---|
| cold, ad-hoc | 640x480 | 30 | 15 | 0.50 s | 88.5 |
| cold, ad-hoc | 640x480 | **5** | 13 | **2.60 s** | 88.4 |
| cold, independent (main agent) | 1280x720 | 30 | 14 | 0.47 s | 85.4 |
| cold, `warmup-measure.py` | 640x480 | 30 | 12 | 0.40 s | 74.0 |
| cold, `warmup-measure.py` | 640x480 | **5** | 15 | **3.00 s** | 112.6 |

A control run on a *warm* sensor at 640x480/30 settled at frame 9 with a total
excursion of only ~3%, confirming that the cold opens are the ones that matter.

**The 5 fps runs are the finding.** Settle stayed in a 12-15 frame band across
five cold opens, two frame rates, two resolutions and two independent
implementations of the measurement — while the wall-clock interval ranged from
0.40 s to 3.00 s. Auto-exposure converges over a roughly constant *number of
frames*, not a constant interval, so a warm-up default expressed in seconds is
correct at exactly one frame rate and under-warms at every lower one.
`record`'s flat 2.0 s discarded 60 frames at 30 fps (four times what is needed)
and only 10 frames at 5 fps — fewer than the measured settle.

The two `warmup-measure.py` bursts also landed on visibly different scenes
(final mean luma 74.0 and 112.6, a ~1.5x difference in brightness) and the
opening excursion in the darker of them was **-28.3%** — the "first frames are
dark" failure the brief warns about, observed directly. Settle held at 12-15
frames anyway, which is some evidence the frame count is not purely an artefact
of one illumination. It is not enough evidence to call the *margin* measured.

**What changed.** One measured constant,
`webcam_cli.engine.DEFAULT_WARMUP_FRAMES = 30`, now backs both verbs, converted
through the *negotiated* fps. `stream` keeps its behaviour at 30 fps and gains
correctness at other rates; `record`'s default becomes 1.0 s at 30 fps (down
from 2.0 s) and 6.0 s at 5 fps (up from 2.0 s). `record --json` now reports
`warmup_frames` and `warmup_basis` alongside `warmup_s`. The number is
documented in `README.md`, `learn`, and the `stream` and `record` catalog
entries, all deriving from the same constant.

**What is *not* measured** is the 2x margin. All five runs were in the same
room over one evening; a genuinely dark scene should be expected to settle more
slowly than anything observed here. 30 frames is a cushion over what was seen,
not a characterization, and the code comment, `learn`, the catalog entries and
the `--json` `warmup.basis` field all say so rather than implying otherwise.

## MJPEG into `matroskamux` — verdict

Verified on hardware: **it works, no `jpegparse` needed.** `v4l2src !
image/jpeg,... ! matroskamux ! filesink` produced a container whose JPEG track
carried `parsed=(boolean)true` caps into `jpegdec`, which decoded it to I420.
The same holds over `tcpserversink` in the live runs above. No change was made,
and `_sink_chain` stays as t6 built it.

## Findings

### Finding 1 — format probing was broken on this host; nothing hardware-facing worked at all

The first `--probe` against the real camera failed:

```json
{"code": 1, "message": "no formats available to negotiate against",
 "remediation": "probe the device's formats before requesting a stream"}
```

`engine.probe_formats` parses `gst-device-monitor-1.0` output, and it was
written (necessarily, under `d1`) against GStreamer's own `v4l2deviceprovider`
spelling. On this host PipeWire is running, and PipeWire's device provider
calls `gst_device_provider_hide_provider("v4l2deviceprovider")` — so *its*
spelling is the only one emitted, and it diverges in three independent ways:

1. **no `device.path` property at all.** The node path arrives as
   `api.v4l2.path = /dev/video0` and as `object.path = v4l2:/dev/video0`. The
   parser matched only `device.path`, so no device block ever matched and the
   result was always `()`.
2. **caps carry no type annotations** — `width=640`, not `width=(int)640`. The
   `width=\(int\)(\d+)` regexes could not match.
3. **every framerate is a list** — `framerate={ (fraction)30/1, (fraction)25/1,
   … }`, not the single fraction the annotated spelling carries.

Any one of these was fatal. Together they meant **`--probe` and `--apply` were
non-functional on this host for both `stream` and `record`**, because every
hardware path negotiates through `probe_formats` first.

Fixed: the parser now accepts all three device-path keys (stripping
`object.path`'s `v4l2:` scheme), accepts annotated *and* bare values, and
expands list-valued fields into the cross product of their discrete
alternatives. Ranges (`[1, 640]`) are still skipped rather than guessed at — a
range is a continuum, not an enumeration. After the fix the C270 enumerates
**198 formats** (114 MJPG, 84 YUYV). Four regression tests replay the real
PipeWire transcript.

### Finding 2 — no consumer could decode an `av` stream at all

`webcam stream av --apply` announced a working-looking attachment point that
delivered **0 video buffers and 1 audio buffer** to a consumer over 8 seconds.
The consumer ran without error; it simply received nothing.

Bisected against hardware:

| what was varied | video / audio buffers |
|---|---|
| live `stream av`, announced consumer | 0 / 1 |
| `matroskamux streamable=true` on the server | 0 / 1 |
| per-branch queues on the *server* | 0 / 1 |
| consumer attached at t=0 instead of t=3 s | 0 / 1 |
| `stream video` (single medium), announced consumer | **59** / – |
| a known-good file from `webcam record --kind av`, announced consumer shape | 0 / 1 |
| that same file, announced shape **plus a `queue` per branch** | **40 / 268** |

The last two rows isolate it: the server and the muxed bytes were fine all
along. The defect was in the **announced consumer command**. A demuxer fanning
out to two branches runs both from a single streaming thread unless a `queue`
puts a thread boundary in each; without them the pipeline stalls. Single-medium
consumers have one branch and are unaffected, which is why only `av` was
broken and why nothing hardware-free could have caught it.

Fixed: `_consumer()` now emits `demux. ! queue ! …` on both branches. The live
`av` decode after the fix is row 4 of the blind-consumer table — 60 video and
399 audio buffers in 4 s.

### Finding 3 — V4L2 busy was not the typed busy error

Q4 asks for a typed error naming the holder when a device is already open. That
worked for ALSA and did not work for V4L2, for a reason that is a property of
the kernel rather than of this code: **uvcvideo permits several `open()`s of
the same `/dev/video*` node** and only refuses at `S_FMT`/`STREAMON`. Measured
directly while a stream held the C270:

```text
check_access('/dev/video0','video')      -> ok        holder None
check_access('/dev/snd/pcmC1D0c','audio')-> busy      holder gst-launch-1.0 pid 716176
find_holder('/dev/video0')               -> gst-launch-1.0 pid 716176
```

So the access gate passed and the pipeline then died, surfacing as a generic
`the capture pipeline exited during warm-up with status 1: …` with the real
cause buried in an embedded gst-launch transcript and a remediation that only
guessed at it. Typed and bounded, but not *the* busy error, and no holder
named — even though `find_holder` could name it perfectly well.

Fixed: `engine.output_reports_device_busy()` recognises the engine's own
wording, and `access.busy_error()` builds the same typed error
`require_access` would have produced, holder lookup included. `stream`'s
warm-up and supervision paths and `record`'s artifact check all route through
it. Result: the 1.36 s typed busy error in the table above (1.33 s on the other run).

### Finding 4 — the activation log silently dropped every late fact

`activation_scope` copies the `detail` mapping it is handed. `stream` mutated
its own local dict after entering the scope, so **the negotiated format, the
applied warm-up and the pipeline pid never reached the log**:

```json
{"verb": "stream av", "target": "tcp://127.0.0.1:5000",
 "detail": {"mode": "apply", "medium": "av", "video_node": "/dev/video0", …}}
```

No `negotiated`, no `warmup_ms`, no `pid`. For a consent record, "the camera
was opened" without "in what format, for how long, by which process" is most of
the value gone. (`record` was unaffected — it already wrote to `act.detail`.)

Fixed: `stream` writes to `act.detail`, and the misleading comment claiming the
dict was shared by reference is corrected. Step 4 of the acceptance script now
asserts that every applied activation carries `ended_at`, `pid`, `negotiated`
and `warmup_ms`.

### Finding 5 — the C270 does not deliver the frame rate it advertises (not fixed)

Default negotiation picks the largest MJPG mode, `1280x960@30`. The device
enumerates it, `v4l2src` accepts the caps, and no error is raised — but it
delivers roughly **11-15 fps**, not 30:

- 30 buffers at 1280x960 took 2.77 s (≈ 10.8 fps);
- the live `av` stream delivered 60 frames in 4 s (≈ 15 fps);
- the 3 s recording contained 40 frames (≈ 13.3 fps).

The payload reports `negotiated.fps: 30.0`, which is true of the *caps that
were accepted* and false of the frames that arrive. This is not a silent
format fallback — no substitution happens — but a consumer sizing a buffer or a
timeline from `negotiated.fps` will be wrong by 2x.

**Left unfixed deliberately.** Reporting delivered fps means counting frames
during warm-up and adding a field, and the more interesting question — whether
`_pick_default_format` should prefer the largest *sustainable* mode over the
largest advertised one — is a design decision for the operator, not something
to slip into an acceptance run. Recommended follow-up: measure delivered fps
during the existing warm-up window (the frames are already being discarded) and
report it alongside `negotiated`.

### Finding 6 — `attach.consumer.generic` decodes only one stream of an `av` pair (not fixed)

The announced `generic` fallback is `tcpclientsrc … ! decodebin ! fakesink`.
Against a two-track stream `decodebin` exposes two source pads and only the
first is linked, so it decodes video and silently drops audio (verified: 40
video buffers, no audio branch). It runs clean and reports no error, which is
the part worth flagging — it is labelled "generic", but for `av` it is
*partial*, and the payload does not say so. Small, and fixing it means either
announcing a two-branch `decodebin name=d …` form for `av` or documenting the
limitation in the payload; either is a surface change worth deciding on
deliberately.

## Verification

Run at the end of the acceptance script, on this branch:

```console
$ uv run pytest -n auto
359 passed, 6 warnings in 1.35s

$ uv run teken cli doctor . --strict
PASS         doctor_check_shape: every check (2) carries the required keys
PASS         doctor_remediation_when_unhealthy: doctor reports healthy; remediation contract trivially satisfied
```

Suite was 336 tests before this task; the 23 added cover the four fixes — the
PipeWire transcript, the per-branch queues, the V4L2 busy mapping, the
activation-record completeness, and the frame-count warm-up basis.

```text
verdict: 14 passed, 0 failed
```

## Criteria

| # | Criterion | Status |
|---|---|---|
| 1 | headless, no TTY; consumer attaches from the `--json` payload alone and decodes live video *and* audio | **met** — 60 video + 399 audio buffers over 4 s, `tty` = not a tty |
| 2 | `list`→`stream`→attach and `list`→`record` from stable ids and JSON only, no manual host inspection between steps | **met** — the only host fact supplied is the stable id; the script fails if a payload does not carry what the next step needs |
| 3 | a second open of the held C270 returns the typed busy error within a bounded time | **met** — 1.36 s (video), 0.31 s (audio), both naming the holder; required finding 3's fix |
| 4 | before-state cited from git history, C270 only, run ends with the suite and the rubric green | **met** — `52aa9fd` verified in-script, 359 tests green, rubric green |

Findings 5 and 6 are reported unfixed by choice; both are surface decisions for
the operator rather than defects this task should have settled alone.
