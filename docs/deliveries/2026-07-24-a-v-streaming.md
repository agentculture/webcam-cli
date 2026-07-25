# Delivery Summary — a/v streaming

plan: `a-v-streaming` · run: `complete` · date: `2026-07-24`
baseline: `devague summary skeleton`

## Intent

Take the converged `a-v-streaming` frame — *webcam-cli streams live video, live
microphone audio, and combined muxed A/V from attached USB capture devices,
addressed by stable id, under the same agent-first JSON/error contract as the
planned capture verbs* — from a repository with **no capture code at all** to a
working capture surface, executed as nine tasks in four dependency waves fanned
out by `/assign-to-workforce`, each merge TDD-gated by the main agent.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Device identity core: enumerate logical devices from /dev/v4l/by-id + /sys USB topology + /proc/asound, collapse multi-node UVC devices, pair camera to mic via shared USB parent, resolve by stable id
- `t2` — Typed device-access errors: busy (naming the holder via /proc scan when determinable), absent vs present-but-forbidden with per-subsystem fix naming, within the CliError contract
- `t3` — GStreamer engine adapter: capability detection (gst-launch-1.0 + plugin probe via gst-inspect-1.0), pipeline construction for v4l2src / alsasrc / muxed A-V, typed exit-2 install hint when absent — zero runtime deps
- `t4` — Activation log: append-only JSONL of every stream/record activation (device stable id, verb, target, timestamps, pid), location documented and echoed in --json
- `t5` — webcam list verb: logical devices with stable id, nodes, ALSA card, paired mic, and access status (ok / absent / forbidden-with-fix) in text and --json
- `t6` — webcam stream verb group (video / audio / av): dry-run by default resolving device + negotiation and printing the attachment plan; --apply exposes an unbounded GStreamer-served local attachment point with warm-up, honest negotiated-vs-requested reporting, and activation logging
- `t7` — webcam record verb: bounded-by-construction clip/audio recording — duration and/or size cap enforced with a default, unbounded not expressible; dry-run default, --apply writes one artifact at the named path with full --json metadata
- `t8` — Surface wiring + self-description rewrite: register list/stream/record in all four hand-maintained points, fix prog to webcam, purge template prose from learn/explain/overview, keep the rubric green
- `t9` — On-host acceptance: documented script proving the blind-consumer contract on the C270 — list to stream to attach and list to record from stable ids + JSON alone, live second-process decode, busy-device probe, before-state cited from git history

## Actual Delivery

All nine tasks merged. Every merge passed the TDD gate (suite green before and
after); no task was reverted.

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `webcam_cli/devices.py` — by-id enumeration, multi-node collapse, USB-parent pairing, `resolve()`. 4 synthetic sysfs fixture trees. Merged `843a597`. |
| `t2` | delivered | `webcam_cli/access.py` — `ok`/`absent`/`forbidden`/`busy` with per-subsystem remediation, `/proc` holder scan. Merged `b6602d8`. Amended twice post-merge (see Drift). |
| `t3` | delivered | `webcam_cli/engine.py` — capability detection, format probing, negotiation validation, argv pipeline builders. Merged `f6bb67b`. Shipped defective; repaired by `t9` (see Drift). |
| `t4` | delivered | `webcam_cli/activation.py` — append-only JSONL, one line on scope exit including on exception, `O_APPEND` concurrent-safe. Merged `4d1b8cd`. |
| `t5` | delivered | `_commands/list_devices.py` — logical devices with `video_access`/`audio_access` reported independently. Merged `44200a3`. |
| `t6` | delivered | `_commands/stream.py` — `stream video\|audio\|av`, unbounded, `tcpserversink` on loopback, Matroska-contained, warm-up, negotiated-vs-requested. Merged `a80448f`. Announced `av` consumer command was defective; repaired by `t9` (see Drift). |
| `t7` | delivered | `_commands/record.py` — bound enforced at `Bound` construction; unbounded inexpressible. Merged `c5988e9`. |
| `t8` | delivered | `prog` → `webcam`; all three verbs registered in the four hand-maintained points; template prose purged; 17 catalog entries. Merged `0f2b550`. |
| `t9` | delivered | `docs/acceptance-a-v-streaming.md` + `scripts/acceptance/` (3 scripts); 14/14 acceptance checks passing twice; four defects found and fixed; warm-up measured. Merged `99388e4`. |

## Mid-work Decisions

- `d1` — wave 2 (t6, t7) is built and tested hardware-free; the live `--apply`
  integration proof moves to t9 — *t6's acceptance criterion 2 and t7's
  `--apply` criterion both imply live capture during wave 2. The split plan
  presented to the operator stated hardware activates only in wave 4; that was
  inaccurate. Operator chose to keep wave 2 hardware-free so camera/mic
  activation happens in exactly one place, t9, under their eye.*
- `d2` — dry-run does not touch hardware by default; real format enumeration is
  opt-in behind `--probe` and is itself logged as an activation — *t6 criterion 1
  required dry-run to validate "against the enumerated set" AND run "without
  opening the device", but `engine.probe_formats` shells out to
  `gst-device-monitor-1.0`, which briefly opens every camera on the host.*
- **Wave-1 modules were made flat (`devices.py`, not `devices/`)** — no
  deviation record covers this. Four parallel agents each owning a package
  `__init__.py` would have collided at merge; the dependency graph guarantees
  logical independence within a wave, not file disjointness.
- **Verb modules were forbidden from touching the four shared wiring files**,
  which is why `t8` exists as its own wave. Wave-2 agents wrote `register(sub)`
  but nothing called it, and their tests built local parsers instead of going
  through `main()`.
- **Warm-up was rebased from wall-clock onto frame count** during `t9`, after
  measurement showed settle tracks frames (12–15) and not seconds (0.40 s–3.00 s
  across fps). Both verbs now derive from one `engine.DEFAULT_WARMUP_FRAMES`.
- **Captured media was confined to `$TMPDIR` and gitignored** before any
  hardware ran. This repository is public and its `.eidetic/memory` is
  committed; frames of the operator's room must not become distributable.
- **`t9` was authorized to repair merged modules.** It was alone in its wave, so
  file-disjointness no longer applied, and four of its findings were defects in
  already-merged code rather than gaps in its own.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t6` (`d1`) | t6's criterion 2 and t7's `--apply` criterion both imply live capture during wave 2; operator chose to keep wave 2 hardware-free so activation happens in exactly one place, under their eye | `acceptable` |
| `t6` (`d2`) | dry-run could not both validate against the enumerated set and avoid opening the device; resolved into a three-level split (default / `--probe` / `--apply`) | `acceptable` |
| `t3` | `probe_formats` could not parse this host's `gst-device-monitor` transcript at all — PipeWire's provider displaces GStreamer's and reports no `device.path`, untyped caps, and list-valued framerates. `--probe`/`--apply` were non-functional for **both** verbs from merge until `t9`. Not detectable hardware-free. | `acceptable` |
| `t6` | the announced `av` consumer command gave the demuxer's two branches no `queue`, so **no consumer could decode an `av` attachment point** (0 video buffers over 8 s, no error raised). Single-medium streams have one branch and were unaffected — which is why the hardware-free suite passed. | `acceptable` |
| `t2` | V4L2 busy was not detected: uvcvideo permits several `open()`s and refuses only at `S_FMT`, so a genuinely held camera reported `ok` while its ALSA sibling correctly reported `EBUSY`. `t9` added `access.busy_error()` and mapped it from engine output. Criterion 3 of `t9` passes **because of** this fix, not despite it. | `acceptable` |
| `t6` | every `stream --apply` activation record lost its negotiated format, warm-up and pipeline pid — `activation_scope` copies the detail mapping and `stream` mutated a local dict. For a consent log this was most of the value. | `acceptable` |
| `t2` | `ENODEV`/`ENXIO` (hardware vanished between enumeration and open) were bucketed as `forbidden`, returning seat-ACL/group remediation for a problem that is not a permission problem. Fixed on the integration branch (`588b10a`) before wave 2. | `acceptable` |
| `t6`/`t7` | warm-up defaults disagreed (stream ~1 s, record 2.0 s) and were expressed in the wrong unit. `record`'s flat 2.0 s was wrong *in kind*: 60 frames at 30 fps but only 10 at 5 fps, under the measured settle. | `acceptable` |
| — (cross-cutting) | `tests/conftest.py` added on the integration branch: `_CliArgumentParser._json_hint` is class-level state `main()` sets and never clears, so a module ending on a `--json` call made a later test that builds its own parser render errors as JSON. Invisible per-file; surfaced only when t5/t6/t7 were merged together. | `acceptable` |

No task was dropped, blocked, or delivered partially. The recurring pattern —
six of the nine drift entries are defects that a green hardware-free suite could
not see — is the run's most transferable finding and is recorded in
`docs/acceptance-a-v-streaming.md`.

## Evidence

- tests: full suite — **359 passed** (`uv run pytest -n auto`, and again serially
  via `uv run pytest`; the `_json_hint` bug was ordering-dependent, so both
  orderings are checked)
- tests: `tests/test_devices.py::test_resolution_by_stable_id_survives_renumbering` — pass
- tests: `tests/test_devices.py::test_c270_camera_pairs_with_its_own_microphone` — pass
- tests: `tests/test_access.py::test_check_access_busy_is_bounded_and_never_hangs` — pass
- tests: `tests/test_access.py::test_vanished_hardware_reports_absent_not_forbidden` — pass (4 params)
- tests: `tests/test_record.py::TestArgparseEscapeHatches::test_no_unbounded_flag_exists_on_the_surface` — pass (5 params)
- tests: `tests/test_record.py::TestApply::test_apply_writes_exactly_one_artifact` — pass
- tests: `tests/test_stream.py::test_streams_are_unbounded_no_duration_flag_exists` — pass
- tests: `tests/test_cli.py::test_no_template_prose_survives` — pass
- tests: `tests/test_cli.py::test_every_registered_path_has_a_catalog_entry` — pass
- lint: `black --check` · `isort --check-only` · `flake8` · `bandit -c pyproject.toml -r webcam_cli` — all clean, bandit 0 issues at every severity
- lint: `markdownlint-cli2` over tracked markdown — 0 errors
- rubric: `uv run teken cli doctor . --strict` — 26/26 PASS, exit 0
- live: `scripts/acceptance/run.sh` — **14 passed, 0 failed**, run twice on the final tree
- live: blind consumer decoded **60 video buffers (I420 1280x960) + 399 audio buffers (S16LE 48 kHz mono) over 4 s**; `raw_socket` form read the announced EBML magic `1a 45 df a3` as its first four bytes
- live: busy probe — video exit 2 in **1.36 s**, audio exit 2 in **0.31 s**, both naming the holder
- live: warm-up — settle at **12–15 frames** across five cold opens, wall-clock 0.40 s–3.00 s
- commits: `52aa9fd..99388e4` (26 commits, 169 files, +13 198/−159)
- version: `0.7.0` → `0.8.0` with CHANGELOG entry
- issues: `agentculture/lobes-cli#155` (filed — blocks the model-consumer demo, not this delivery)

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| A blind consumer attaches to a live stream using only the `--json` payload and decodes real video and audio | high | `scripts/acceptance/blind-consumer.sh` (structurally blind: only input is the payload file) · 60 video + 399 audio buffers over 4 s · `docs/acceptance-a-v-streaming.md` |
| Device identity survives replug renumbering; cameras pair to their own microphone through USB topology | high | test `tests/test_devices.py::test_resolution_by_stable_id_survives_renumbering` · test `::test_c270_camera_pairs_with_its_own_microphone` |
| A busy device fails fast with a typed error naming the holder, and never hangs | high | live: video 1.36 s / audio 0.31 s, exit 2, holder named · test `tests/test_access.py::test_check_access_busy_is_bounded_and_never_hangs` |
| `absent` and `forbidden` are never conflated, and each names its per-subsystem fix | high | test `tests/test_access.py::test_absent_and_forbidden_are_never_conflated` · `::test_forbidden_remediation_names_seat_acl_for_video` |
| An unbounded recording is not expressible via any flag combination | high | test `tests/test_record.py::TestArgparseEscapeHatches::*` (5 params) · `Bound.__post_init__` refuses construction |
| `record --apply` writes exactly one artifact | high | test `tests/test_record.py::TestApply::test_apply_writes_exactly_one_artifact` (asserts the whole `tmp_path` file set) |
| Every activation is logged, with negotiated format, warm-up and pid | high | `webcam_cli/activation.py` · t9 fix + acceptance step 4 asserting record completeness |
| Streaming adds no runtime dependency | high | `pyproject.toml` `dependencies = []` unchanged across `52aa9fd..99388e4` |
| The self-description no longer presents `webcam-cli` as a typable command, and carries no template prose | high | test `tests/test_cli.py::test_no_template_prose_survives` · rubric `project_scripts: webcam` |
| Warm-up settle is a frame count (12–15), not a wall-clock duration | high | `scripts/acceptance/warmup-measure.py` · five cold opens at 30 fps and 5 fps |
| The chosen 2x warm-up margin (30 frames) is correct for arbitrary conditions | unverified | measured in one room on one evening; every surface states it as provisional |
| MJPEG muxes into `matroskamux` without `jpegparse` | medium | live decode succeeded on this host; not tested on another GStreamer build |
| `negotiated.fps` reflects deliverable frame rate | unverified | **contradicted** — the C270 delivers ~11–15 fps at the `1280x960@30` mode it advertises; see Remaining Work |
| The surface behaves correctly for a headless agent with no seat ACL | unverified | not reproducible from a desktop session; the `forbidden` path is unit-tested but never exercised live |

## Remaining Work / Follow-up

- **`negotiated.fps` overstates deliverable frame rate.** The C270 advertises
  `1280x960@30` and default negotiation picks it, but delivers ~11–15 fps.
  Nothing silently falls back, yet a consumer sizing a timeline will be misled.
  Fixing it means deciding whether `_pick_default_format` should prefer the
  largest *sustainable* mode over the largest advertised one — a design call for
  the operator. Recommended: count frames during the existing warm-up window
  (they are already being discarded) and report a measured fps alongside the
  advertised one.
- **`attach.consumer.generic` silently drops one stream of an `av` pair** and
  reports no error. The `gst_launch_str` form is correct; only the `generic`
  convenience form is affected.
- **The 2x warm-up margin is unmeasured.** One room, one evening. A darker room
  will settle slower and is the case a default must survive.
- **The headless/`forbidden` path has never run live.** It cannot be reproduced
  from a desktop session with a seat ACL; a container or systemd-unit run would
  close it.
- **No `describe` verb.** The brief's suggested surface included one; this plan
  did not. Its enumeration is currently reachable via
  `stream video <device> --probe --json` → `negotiation.available`.
- **Parked frame follow-ups, unchanged**: `v2` — announcing stream start/stop
  through `events-cli` (cross-repo, needs the sibling agent); `v3` — a WebSocket
  audio-only endpoint (realtime-API style PCM/Opus chunks) layered on the buffer.
- **Model-consumer demo blocked upstream.** Frames to `senses`
  (`gemma-4-12B`) verified working; microphone audio to `stt` cannot be
  demonstrated through the gateway while `agentculture/lobes-cli#155` is open.
