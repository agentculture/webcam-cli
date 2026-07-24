# webcam-cli

Agent and CLI for USB webcam and microphone access: enumerate attached devices, capture stills and video clips, and record audio, with agent-friendly verbs and safe-by-default writes.

## Status

**Scaffold.** The agent-first CLI skeleton, mesh identity, CI, and skill kit are in place and green. The capture surface — `list`, `describe`, `capture`, `record` — is **not implemented yet**. The design brief and the questions still to settle live in
[issue #1](https://github.com/agentculture/webcam-cli/issues/1).

## Scope

webcam-cli owns local USB capture devices — video *and* audio input — and the act of getting frames and samples off them. It produces an artifact plus honest metadata about it, and stops there.

It does **not** own interpreting what is in a frame (a vision model's job), sound *out* (`harmonics-cli`), browser or screen capture (`webglass-cli`), the general file-and-shell surface (`shell-cli`), or mesh event semantics (`events-cli`).

## Quickstart

```bash
uv sync
uv run pytest -n auto                 # run the test suite
uv run webcam whoami                  # identity from culture.yaml
uv run webcam learn                   # self-teaching prompt (add --json)
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

The console command is `webcam`. The import package is `webcam_cli` and the PyPI distribution is `webcam-cli` — deliberately decoupled, so the ergonomic thing you type stays short while the import cannot shadow a generic `webcam` module in a consumer's environment.

## CLI

Today's surface is the agent-first template baseline:

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Planned, per the build brief:

| Verb | What it will do |
|------|-----------------|
| `list` | Attached capture devices, video and audio, keyed by **stable id**, reporting the ephemeral node alongside it, collapsing multi-node devices into one logical entry. |
| `describe <device>` | The actually-enumerated capabilities: pixel formats, resolutions, frame rates. |
| `capture <device>` | A still. Emits the resolved device identity, the *negotiated* format/resolution/fps, the output path, and a timestamp. |
| `record <device>` | A clip and/or an audio recording, with an explicit duration. |

Every command supports `--json`. Results go to stdout, errors/diagnostics to stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment error, `3+` reserved.

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
