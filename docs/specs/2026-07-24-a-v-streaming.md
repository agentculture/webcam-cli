# a/v streaming

> webcam-cli streams live video, live microphone audio, and combined muxed A/V from attached USB capture devices, addressed by stable id, under the same agent-first JSON/error contract as the planned capture verbs
> instruction: verify end-to-end on this host: start a combined A/V stream of the C270 by stable id with --apply, then attach a second process using only the --json payload and confirm it decodes live video and audio

## Audience

- mesh agents and follow-on tools are the primary consumers — a vision model attaching to a video buffer, a voice pipeline consuming mic audio, events-cli announcing activations — with the human operator second, for debugging and consent oversight

## Before → After

- Before: webcam_cli contains no capture code at all — six template verbs and prose that still calls the repo a clonable template; there is no way to get frames or samples off the attached devices through this tool
- After: an agent can enumerate devices by stable id, attach to a live video / audio / combined-A-V stream buffer, or request a bounded recording — and everything needed to consume the stream (attachment endpoint, negotiated format, pairing, timestamps) is in the --json payload, no host inspection required

## Why it matters

- streaming turns the cameras and mics from one-shot artifact producers into live sensors the mesh can actually use — realtime vision and voice — while keeping the honest-metadata, consent-logged posture the brief demands

## Requirements

- stream verbs address devices by stable identity (/dev/v4l/by-id path, ALSA card longname/serial), never bare index — on this host the C270/Arducam video nodes and ALSA cards have already swapped since the brief was written
  - honesty: a stream request keyed by stable id still resolves correctly after an unplug/replug renumbers /dev/videoN and the ALSA card
- stream --json is complete enough that a consumer never re-derives: resolved stable identity, negotiated (format, resolution, fps) actually granted vs requested, delivery target, start timestamp — Q6 validation against the enumerated set, no silent fallback
  - honesty: a test consumer reconstructs device, negotiated format, and attachment target from the --json payload alone, with no host inspection
- stream verbs fail fast with a typed device-busy error naming the holder when determinable, never hang — and a running stream itself holds exclusive access for its whole lifetime, which its metadata must state
  - honesty: opening the C270 while another process holds it returns the typed busy error within a bounded time — never hangs — naming the holder when determinable
- device enumeration and stream-open errors distinguish absent from present-but-forbidden and name the per-subsystem fix: video nodes ride the logind seat ACL (headless/container/systemd loses them), audio nodes ride audio group membership (survives headless here)
  - honesty: with the seat ACL absent (headless simulation) the error names the ACL/group fix; with the node absent it reports absent; the two are never conflated
- each stream verb lands in all four hand-maintained surface points (command module with register(), the _build_parser call, an explain catalog entry, overview._VERBS plus the learn command map), nests with parser_class=type(p), and passes teken cli doctor --strict
  - honesty: teken cli doctor --strict passes with the new verbs registered in all four hand-maintained surface points
- the first streaming PR rewrites the agent-facing self-description in the same change: the learn text, explain catalog and overview artifact list still call this repo a clonable template, and prog plus every doc string say webcam-cli although the installed command is webcam
  - honesty: after the first streaming PR, learn/explain/overview contain no template prose and every documented invocation uses the installed command name webcam
- combined A/V streaming pairs camera and mic through USB topology (shared sysfs USB parent), never index correlation; on this host the C270 video nodes and its ALSA card share USB device 3-1 and the by-id/serial handles agree
  - honesty: pairing is derived from sysfs USB topology and matches the C270 camera to its own microphone on this host
- every stream activation is loggable and writes only to the named target with no hidden buffer; the tool states plainly that it cannot promise a hardware activity light (device firmware) and never implies it prevents covert use
  - honesty: every stream and record activation appends a log entry, and no bytes land anywhere except the named target or announced buffer
- video and combined-A-V stream buffers are exposed through a GStreamer-served local attachment point (shared-memory or local-socket sink) that a separate process attaches to using only what --json announced
  - honesty: a second process, given only the announced attachment point, attaches and receives decodable frames

## Honesty conditions

- on this host, a separate process attaches to a C270 video-plus-mic stream and decodes live media end-to-end
- the built wheel declares zero runtime dependencies after streaming lands (dependencies = [] unchanged in the PR diff)
- the rubric gate still passes with streaming verbs present: success stdout carries only the result, every failure exits 1 or 2 with error:+hint: (or the JSON triple), and no traceback appears in any new-verb test
- the --json payloads are exercised in tests by a headless script with no TTY and no human — proving the primary agent audience can operate the surface blind
- on this host, list -> stream -> attach and list -> record each complete using only stable ids and --json output, with no manual host inspection between steps
- main today contains no capture or stream code — only the six template verbs — so the before-state is verifiable in git history at the moment the spec lands
- a live consumer demo exists: frames and samples reach a second process while the stream is running, not merely a file inspected after the fact
- the success signal is executable: a documented acceptance script (or CI job) performs the blind consumer attach and a bounded record, and teken cli doctor --strict plus pytest stay green
- acceptance tests exercise only the C270 by its stable id; Reachy Mini Audio and Arducam appear only in enumeration or negative tests

## Success signals

- a consumer process that has never inspected this host starts a stream and decodes live media using only the --json payload; record produces a bounded, well-formed artifact; teken cli doctor --strict and the full CI matrix stay green

## Scope / boundaries

- streaming adds no runtime Python dependency — dependencies = [] in pyproject.toml stays empty (Q2 posture, shell-cli precedent); bandit config already skips B404/B603, so a subprocess engine is lint-accommodated
  - instruction: check the PR diff leaves dependencies = [] in pyproject.toml untouched; the engine is a subprocess shell-out to gst-launch-1.0 with capability detection
- the template contracts survive streaming unchanged: results to stdout and diagnostics to stderr never mixed, every verb takes --json, failures are CliError with hint: and exit codes 0/1/2, no traceback ever reaches stderr
- this feature targets only the Logitech webcam with its onboard mic (C270); Reachy Mini Audio is out of scope, the Arducam is not a target
  - instruction: key all stream/record acceptance paths on the C270 stable id (usb-046d_C270_HD_WEBCAM_200901010001); do not build Reachy or Arducam targeting into this iteration

## Non-goals

- streaming never interprets content (vision models do), never does sound out (harmonics-cli), never captures browser or screen (webglass-cli), and does not reimplement mesh event semantics — announcing that a stream started is a consumer relationship with events-cli (Q10)

## Assumptions

- the streaming engine is a shell-out to gst-launch-1.0: the only video-capable capture stack installed here (v4l2src/pulsesrc/alsasrc + jpegenc/vp8enc/theoraenc/opusenc + matroskamux/splitmuxsink/hlssink2/tcpserversink); x264enc is absent so H.264/MP4 is off this host's menu; capability detection stays mandatory because ffmpeg, v4l2-ctl and fswebcam are all absent and the brief forbids assuming a backend
- the Logitech mic streams via direct ALSA (hw:CARD address) rather than PulseAudio: the C270 PipeWire card profile is off so no PulseAudio source exists, and flipping the profile would be a system-state write; a 1-second arecord from hw:1,0 was verified working
- streams follow the template write contract: dry-run by default resolves the device, validates the requested negotiation and prints the delivery plan without energizing hardware; --apply starts the stream and applies warm-up (discard N frames / settle T ms, documented and overridable) before delivering

## Scope exploration

- `s1` — `/dev/v4l/by-id + /proc/asound/cards (re-surveyed live 2026-07-24)`: C270 = video0+video1 + ALSA card 1; Arducam = video2+video3 + card 3; Reachy Mini Audio = card 2, mic with no camera. Video-node and ALSA-card numbering have both swapped since the brief survey — plug-order identifiers went stale within weeks on the same host
  - seeds: `c2`
- `s2` — `issue #1 (output contract + Q6)`: the brief requires --json complete enough that a follow-on tool never re-derives what was captured or from where, and Q6 requires negotiated-vs-requested honesty; both extend verbatim to a stream's metadata
  - seeds: `c3`
- `s3` — `PATH + gst-inspect-1.0 survey (live)`: gst-launch-1.0 IS present — the brief's no-capture-backend evidence is out of date on this host; ffmpeg/v4l2-ctl/fswebcam remain absent; arecord/pactl/parec present for audio-only paths
  - seeds: `c4`
- `s4` — `pyproject.toml (0.7.0)`: dependencies = [] is the load-bearing zero-deps posture; bandit skips B404/B603 already accommodate subprocess shell-out; the published description promises stills, clips and audio recording but does not yet claim streaming
  - seeds: `c5`
- `s5` — `pactl list cards/sources + arecord hw:1,0 probe (live)`: C270 PipeWire active profile = off, so pactl lists no C270 source; direct ALSA capture verified OK; the Arducam and Reachy mics DO have PipeWire sources — audio routing differs per device on the same host
  - seeds: `c6`
- `s6` — `arecord -l (live) + issue #1 Q4`: Reachy Mini Audio shows Subdevices 0/1 right now — some process holds it open; V4L2 streaming is single-open; exclusive access is not hypothetical on this host, it is observable today
  - seeds: `c7`
- `s7` — `getfacl /dev/video0 + id (live)`: video0 grants user:spark:rw- via seat ACL and the operator is NOT in the video group, but IS in the audio group — a headless agent loses the camera yet keeps the mic; the two subsystems fail differently and need differently-named fixes
  - seeds: `c8`
- `s8` — `webcam_cli/cli/_output.py + _errors.py`: the stdout/stderr split means a raw media stream cannot share stdout with the JSON result payload — the stream delivery target is a real design decision, recorded as a pending question
  - seeds: `c9`
- `s9` — `cli/__init__.py + _commands/* + tests/ + .github/workflows/tests.yml`: test_every_catalog_path_resolves enforces catalog completeness; the missing-parser_class exit-2 leak is guarded by test_cli_overview_unknown_flag_structured_error; teken cli doctor --strict (26 checks) is a hard CI gate run against the installed CLI
  - seeds: `c10`
- `s10` — `learn.py + explain/catalog.py + overview.py + prog (cli/__init__.py:73)`: self-docs are template prose instructing an uninstalled binary name; CLAUDE.md requires rewriting them as the domain surface lands, not after
  - seeds: `c11`
- `s11` — `issue #1 lane + sibling table`: the lane is getting frames and samples off local USB devices with honest metadata, and stopping; whether serving a network endpoint still counts as producing an artifact is the in-lane boundary question streaming newly raises
  - seeds: `c12`
- `s12` — `PipeWire sysfs.path + /dev/v4l/by-id (live) + issue #1 Q7`: the C270 mic sysfs path usb3/3-1/3-1:1.2/sound/card1 shares USB parent 3-1 with the by-id video links (serial 046d_C270_HD_WEBCAM_200901010001) — topology pairing is implementable today; matroskamux plus vp8enc/opusenc installed gives live in-tool muxing a candidate answer to Q7's who-muxes
  - seeds: `c13`
- `s13` — `issue #1 Q1 + Q5 + template write contract`: capture-as-write is recommended-not-confirmed in the brief; a stream energizes hardware indefinitely, so the dry-run/--apply split and warm-up defaults apply at least as strongly as for stills
  - seeds: `c14`
- `s14` — `issue #1 Q8 consent posture`: continuous streaming sharpens the surveillance posture beyond one-shot capture; what stays promisable is activation logging and named-output-only delivery — whether streams are duration-bounded by default is a user decision, recorded as a question
  - seeds: `c15`

## Decisions

- stream verbs deliver into a live stream buffer, never files on disk; GStreamer is the standard transport unless something better is identified
- record writes a bounded artifact: a file or buffered bytes, with an enforced size limit
- both record and stream ship as first-class verb surfaces
- streams are unbounded (no default duration cap); consent oversight rides activation logging and possibly events-cli announcements, not a timer
- record is bounded by default and must always be bounded — duration/size cap enforced, unbounded recording not expressible

## Open / follow-up

- announcing stream start/stop through events-cli — operator said maybe; cross-repo consumer relationship (brief Q10), settle with the sibling
- WebSocket audio-only stream endpoint (realtime-API style PCM/Opus chunks) layered on top of the GStreamer buffer — follow-up after iteration 1
