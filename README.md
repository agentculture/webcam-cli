# webcam-cli

Agent and CLI for USB webcam and microphone access: enumerate attached devices, capture stills and video clips, and record audio, with agent-friendly verbs and safe-by-default writes.

## Status

**The capture surface has landed.** `list`, `stream`, and `record` are implemented and wired into the CLI, alongside the agent-first baseline (`whoami`, `learn`, `explain`, `overview`, `doctor`, `cli overview`).

Not built: `describe` (the enumerated-capability verb from the brief — today `webcam stream video <device> --probe --json` reports the same enumeration, at the cost of briefly opening the camera) and `capture` (a single still — `record --duration` is the nearest thing). The design brief and the questions still open live in
[issue #1](https://github.com/agentculture/webcam-cli/issues/1).

## Scope

webcam-cli owns local USB capture devices — video *and* audio input — and the act of getting frames and samples off them. It produces an artifact plus honest metadata about it, and stops there.

It does **not** own interpreting what is in a frame (a vision model's job), sound *out* (`harmonics-cli`), browser or screen capture (`webglass-cli`), the general file-and-shell surface (`shell-cli`), or mesh event semantics (`events-cli`).

## Quickstart

```bash
uv sync
uv run pytest -n auto                 # run the test suite
uv run webcam learn                   # start here: the self-teaching prompt (add --json)
uv run webcam list                    # what capture hardware is attached, and can it be opened
uv run webcam whoami                  # identity from culture.yaml
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

The console command is `webcam`. The import package is `webcam_cli` and the PyPI distribution is `webcam-cli` — deliberately decoupled, so the ergonomic thing you type stays short while the import cannot shadow a generic `webcam` module in a consumer's environment. Only `webcam` is ever typed; `webcam-cli` names the project, the distribution, and the mesh nick.

## CLI

| Verb | What it does |
|------|--------------|
| `list` | Attached capture devices, video and audio, keyed by **stable id**, reporting the ephemeral node alongside it, collapsing multi-node devices into one logical entry, with per-subsystem access status. |
| `stream video\|audio\|av <device>` | Serve a live attachment point another process can consume (GStreamer `tcpserversink` on loopback, Matroska-contained). **Unbounded by construction** — no `--duration`. |
| `stream overview` | Describe the `stream` verb group. |
| `record <device> <path>` | Record a clip, an audio file, or both muxed, to exactly one file. **Bounded by construction** — a duration cap always applies and no flag means "forever". |
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors/diagnostics to stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment error (not retryable), `3` device busy (retryable), `4+` reserved.

## What switches the camera on

This tool opens cameras and microphones, so which invocations energize hardware has to be readable from the surface alone. `stream` and `record` share one three-level split:

| Invocation | What it touches |
|------------|-----------------|
| default (no flag) | Nothing. Resolves the device, validates the request, prints the plan it would run. Not logged. |
| `--probe` | **Opens the camera** to enumerate its real formats. Written to the activation log. |
| `--apply` | Opens the device and streams or records. Written to the activation log. |

`list` opens nothing beyond one non-blocking permission probe per node.

Sensor warm-up is applied before frames reach a consumer, since the first frames off a UVC camera are unsettled while auto-exposure converges. Both verbs discard **30 frames** by default, converted through the negotiated frame rate — 1.0s at 30fps, 6.0s at 5fps — plus 200ms of audio ring-fill for `stream audio`; audio-only recording warms up for 0.0s, since a microphone has no exposure to settle. Override with `--warmup-frames N` / `--warmup-ms MS` (`stream`) or `--warmup SECONDS` (`record`); `0` disables.

The unit is frames rather than seconds because that is what was measured. On the reference C270, auto-exposure settled after 13–15 frames whether the camera ran at 30fps or at 5fps — the frame count held while the wall-clock interval grew five-fold — so a fixed-seconds default is right at exactly one frame rate and under-warms at every lower one. 30 frames is about 2x the slowest settle observed; that margin is deliberate and is *not* itself measured, since every run was under one indoor lighting condition. The runs are recorded in [docs/acceptance-a-v-streaming.md](docs/acceptance-a-v-streaming.md) and reproducible with `scripts/acceptance/warmup-measure.py`.

Every activation is appended to an activation log (`~/.local/state/webcam-cli/activations.jsonl` by default; override with `$WEBCAM_ACTIVATION_LOG`), and a capture writes only to the path you name — no hidden buffer, never to stdout.

A hardware activity light **cannot** be promised: that is device firmware, outside this tool's control. This tool records activations; it does not prevent covert use, and nothing here should be read as claiming otherwise.

## Why device identity is the hard part

`/dev/videoN` numbering is plug order, not identity. On the operator's host the two attached cameras swapped indices between the brief being written and being re-checked — the C270 moved from `video2` to `video0` and the Arducam the other way, with their ALSA cards swapping to match. A UVC camera also commonly exposes two `/dev/video*` nodes, only one of which yields frames, so counting nodes overcounts cameras. Stable handles come from `/dev/v4l/by-id/`, which carries vendor, product, and serial.

Access is the other trap: on that host the operator is not in the `video` group at all — read/write comes from a seat ACL granted by logind. An agent running headless, in a container, or as a systemd unit will not receive it and will fail to open a camera that works fine from a desktop session.

See [issue #1](https://github.com/agentculture/webcam-cli/issues/1) for the full evidence and the open questions it raises.

## What this repo carries

- **An agent-first CLI** cited from [teken](https://github.com/agentculture/teken)
  (`afi-cli`) — the runtime package has no third-party dependencies.
- **A mesh identity** — `culture.yaml` (`suffix` + `backend`) and the matching
  resident prompt file (`AGENTS.colleague.md`, since this agent runs
  `backend: colleague`).
- **The canonical guildmaster skill kit** under `.claude/skills/`, vendored
  cite-don't-import. See [`docs/skill-sources.md`](docs/skill-sources.md).
- **A build + deploy baseline** — pytest, lint, the agent-first rubric gate, and
  PyPI Trusted Publishing wired into GitHub Actions.

See [`CLAUDE.md`](CLAUDE.md) for the architecture, the domain constraints, and the conventions (version-bump-every-PR, the `cicd` PR lane, worktree layout, deploy setup).

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
