# Build Plan — a/v streaming

slug: `a-v-streaming` · status: `exported` · from frame: `a-v-streaming`

> webcam-cli streams live video, live microphone audio, and combined muxed A/V from attached USB capture devices, addressed by stable id, under the same agent-first JSON/error contract as the planned capture verbs

## Tasks

### t1 — Device identity core: enumerate logical devices from /dev/v4l/by-id + /sys USB topology + /proc/asound, collapse multi-node UVC devices, pair camera to mic via shared USB parent, resolve by stable id

- covers: c2, h2, c13, h8
- acceptance:
  - logical device list collapses the C270's two video nodes into one entry keyed by its /dev/v4l/by-id handle with serial
  - camera-to-mic pairing is derived from the shared sysfs USB parent and matches the C270 camera to its own mic on a fixture of this host's sysfs tree
  - resolution by stable id is index-independent: a fixture that renumbers videoN and ALSA cards still resolves the same logical device

### t2 — Typed device-access errors: busy (naming the holder via /proc scan when determinable), absent vs present-but-forbidden with per-subsystem fix naming, within the CliError contract

- covers: c7, h4, c8, h5
- acceptance:
  - opening a busy device raises a typed CliError naming the holder pid/command when determinable, within a bounded time, never hangs (tested against a deliberately-held device)
  - absent node yields an absent error; present-but-EACCES yields a forbidden error naming the seat-ACL/video-group fix for video and the audio-group fix for ALSA; the two are never conflated (unit-tested via fake fs)

### t3 — GStreamer engine adapter: capability detection (gst-launch-1.0 + plugin probe via gst-inspect-1.0), pipeline construction for v4l2src / alsasrc / muxed A-V, typed exit-2 install hint when absent — zero runtime deps

- covers: c5, h11
- acceptance:
  - engine detects gst-launch-1.0 and required plugins and reports the capability set; when absent, a typed exit-2 error carries an install hint
  - pipeline builder emits valid gst-launch pipeline strings for video (v4l2src), audio (alsasrc with hw:CARD address), and muxed A-V (matroskamux)
  - pyproject dependencies = [] is unchanged by the PR

### t4 — Activation log: append-only JSONL of every stream/record activation (device stable id, verb, target, timestamps, pid), location documented and echoed in --json

- covers: c15, h9
- acceptance:
  - every activation appends exactly one JSON line with device stable id, verb, named target, start/end timestamps and pid
  - no bytes are written anywhere except the named target/buffer and the activation log (asserted by the artifact tests)

### t5 — webcam list verb: logical devices with stable id, nodes, ALSA card, paired mic, and access status (ok / absent / forbidden-with-fix) in text and --json

- depends on: t1, t2
- acceptance:
  - webcam list --json emits logical devices with stable id, device nodes, ALSA card, paired mic, and per-device access status including the named fix when forbidden
  - list exits 0 with a valid empty result when no devices are attached; stderr is silent on success

### t6 — webcam stream verb group (video / audio / av): dry-run by default resolving device + negotiation and printing the attachment plan; --apply exposes an unbounded GStreamer-served local attachment point with warm-up, honest negotiated-vs-requested reporting, and activation logging

- depends on: t1, t2, t3, t4
- covers: c3, h3, c24, h10
- acceptance:
  - dry-run (default) resolves the device, validates the requested (format, resolution, fps) against the enumerated set, and prints the attachment plan without opening the device
  - with --apply, a second process attaches using only the --json payload and receives decodable frames (integration test)
  - negotiated-vs-requested is reported; an unsupported combination is a typed user error, never a silent fallback
  - warm-up discard is applied at stream start, with a documented, overridable default

### t7 — webcam record verb: bounded-by-construction clip/audio recording — duration and/or size cap enforced with a default, unbounded not expressible; dry-run default, --apply writes one artifact at the named path with full --json metadata

- depends on: t1, t2, t3, t4
- acceptance:
  - record enforces a bound: default cap applied when none given, and an unbounded recording is not expressible via any flag combination
  - record --apply writes exactly one artifact at the named path; --json reports negotiated format, the enforced bound, output path, and timestamps
  - dry-run resolves and validates without energizing hardware

### t8 — Surface wiring + self-description rewrite: register list/stream/record in all four hand-maintained points, fix prog to webcam, purge template prose from learn/explain/overview, keep the rubric green

- depends on: t5, t6, t7
- covers: c9, h13, c10, h6, c11, h7
- acceptance:
  - all three verbs are registered in the command module, _build_parser, the explain catalog (test_every_catalog_path_resolves passes) and overview._VERBS plus the learn map
  - prog is webcam; no doc string, catalog entry or learn text references webcam-cli as a typable command, and no template prose remains in learn/explain/overview
  - teken cli doctor --strict passes and noun groups use parser_class so parse errors keep the structured exit-1 contract

### t9 — On-host acceptance: documented script proving the blind-consumer contract on the C270 — list to stream to attach and list to record from stable ids + JSON alone, live second-process decode, busy-device probe, before-state cited from git history

- depends on: t8
- covers: c1, h1, c19, h14, c20, h15, c21, h16, c22, h17, c23, h18, c27, h12
- acceptance:
  - a documented acceptance script, run headless with no TTY, attaches a consumer from the --json payload alone and decodes live video and audio from the C270 while the stream runs
  - the script exercises list-then-stream-then-attach and list-then-record using only stable ids and JSON output, with no manual host inspection between steps
  - a second open of the held C270 returns the typed busy error within a bounded time during the live run
  - the acceptance doc cites the pre-streaming main commit as the before-state, targets only the C270 stable id, and the run ends with pytest -n auto and teken cli doctor --strict green

## Risks

- [unknown_nonblocking] attachment-point mechanism inside GStreamer (shmsink shared memory vs local socket sink) is decided during the stream task against consumer ergonomics — both are GStreamer-native and satisfy c24 (task t6)
- [unknown_nonblocking] holder identification for busy devices scans /proc/*/fd, which may be permission-limited for other users' processes — the busy error must degrade gracefully to holder-unknown (task t2)
- [unknown_nonblocking] C270 auto-exposure settle time is unmeasured — the warm-up default needs empirical tuning on this host during the stream task (task t6)
- [follow_up] parked follow-ups ride behind this plan: WebSocket audio endpoint layered on the buffer, and events-cli activation announcements (frame parks v2/v3) — neither blocks iteration 1
