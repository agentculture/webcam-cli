# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`webcam-cli` is an AgentCulture mesh agent that **owns local USB capture devices — video *and* audio input — and the act of getting frames and samples off them**. Enumerate what is attached, describe what each device can actually do, and capture stills, clips, and audio on demand, with an output contract another tool can consume without guessing.

The build brief is [issue #1](https://github.com/agentculture/webcam-cli/issues/1) — read it before designing anything. It defines the lane, the evidence it was derived from, and ten open questions that are deliberately unanswered.

**Out of lane** (these belong to siblings — consume them, don't reimplement them):

| Not ours | Owner |
|----------|-------|
| Interpreting what is *in* a frame | a vision model |
| Non-TTS sound *out* | `harmonics-cli` |
| Browser/page/screen capture | `webglass-cli` |
| General file + shell execution surface | `shell-cli` |
| Event semantics for the mesh | `events-cli` |
| Reachy Mini onboard camera (unsettled) | `reachy-mini-cli` — see open question 9 |

We produce an artifact and honest metadata about it, and stop there.

## Current state: the capture surface is built

The domain surface landed on the `spec/a-v-streaming` branch, built from a converged devague spec (`docs/specs/2026-07-24-a-v-streaming.md`) and plan (`docs/plans/2026-07-24-a-v-streaming.md`). Four domain modules sit under `webcam_cli/`, and three capture verbs sit alongside the six template verbs.

| Module | Owns |
|--------|------|
| `devices.py` | logical device identity: `/dev/v4l/by-id` enumeration, multi-node UVC collapse, camera↔mic pairing through the shared sysfs USB parent, `resolve()` by stable id |
| `access.py` | can this node be opened, and if not why: `ok` / `absent` / `forbidden` / `busy`, each with a per-subsystem remediation; `find_holder()` names a busy holder from `/proc/*/fd` |
| `engine.py` | the GStreamer boundary: capability detection, format probing, negotiation validation, pipeline construction (argv, never a shell string) |
| `activation.py` | the consent record: append-only JSONL, one line per activation, written on scope exit even when the body raises |

Verbs: **`list`** (logical devices with per-subsystem access status), **`stream video|audio|av`** (unbounded, serves a `tcpserversink` attachment point on loopback, Matroska-contained), **`record`** (bounded by construction — a duration cap always applies and unbounded is not expressible; writes exactly one Matroska file).

**Every capture path writes Matroska.** `record` picks the codec from the *negotiated source format* rather than a fixed house choice: an MJPG camera format is framed with `jpegparse` and carried through as Motion JPEG (passthrough, no re-encode, no generation loss), a raw pixel format (YUYV and friends) goes through `videoconvert ! vp8enc`, and audio through `audioconvert ! opusenc`. Opus then constrains the audio request — `--rate` must be one it carries (8000/12000/16000/24000/48000 Hz) and `--channels` at most 8, as a typed user error rather than a silent resample. `--kind av` is the exception with no per-branch codec tail: `matroskamux` takes `image/jpeg`, raw `video/x-raw` and `audio/x-raw` on its request pads directly, so both branches are stored as the device delivers them. `stream` serves Matroska on every sub-verb too, but builds its own tail (`matroskamux streamable=true ! tcpserversink`) and so passes `mux=False` to the engine builders — as does `record`'s warm-up phase, which sinks to `fakesink` and must not spend CPU encoding frames it discards.

That is a correction, not a restatement: up to and including the published `webcam_cli` 0.8.0, `record --kind video` and `record --kind audio` linked the source caps straight to `filesink`, so a file named `.mkv` held concatenated JPEG or headerless YUY2 with nothing recording its geometry ([issue #5](https://github.com/agentculture/webcam-cli/issues/5)). Treat any artifact captured with 0.8.0 or earlier as a raw byte stream, not a container. `stream` was never affected — it has always added `matroskamux` in its own sink chain, and the on-hardware acceptance run confirms it.

**The three-level hardware rule is the single most important fact about this surface**, and it is stated in `learn`, in the `explain` catalog, and in every sub-verb's epilog: no flag = dry-run that opens nothing; `--probe` = enumerates real formats, which opens the camera, and is logged; `--apply` = captures or streams, and is logged. An agent must be able to tell from the invocation alone whether hardware switches on.

Both earlier defects are fixed: `prog` is now `webcam` (no user-facing string presents `webcam-cli` as typable — a test enforces it), and the template prose is gone from `learn`, the catalog, and `overview`. `webcam-cli` correctly survives as the *project*, *dist*, and *mesh nick* — do not blanket-replace it.

**Not built yet**: a `describe` verb (its enumeration is currently reachable via `stream video <device> --probe --json` → `negotiation.available`), the WebSocket audio endpoint, and `events-cli` activation announcements. The last two are parked follow-ups on the frame (`v2`, `v3`), not oversights.

## Identity

`culture.yaml` declares `suffix: webcam-cli`, `backend: colleague`, model `sakamakismile/Qwen3.6-27B-Text-NVFP4-MTP`.

`backend: colleague` fixes the **resident mesh prompt** to `AGENTS.colleague.md`. This file (`CLAUDE.md`) is Claude Code guidance only — the mesh runtime does not read it. The pair satisfies the two invariants `steward doctor` verifies: prompt-file-present and backend-consistency; `webcam doctor` checks the same locally.

**Open genesis follow-up:** the brief asks you to *confirm* `colleague` is the intended backend. It is coherent on disk today (yaml, `AGENTS.colleague.md`, the `doctor` mapping, and `tests/test_cli_introspection.py::test_doctor_recognizes_declared_backend` all agree), so changing it means changing all four together.

## Commands

```bash
uv sync                                    # install (dev group included)

uv run pytest -n auto                      # full suite, parallel
uv run pytest tests/test_cli.py::test_whoami_json -v   # a single test (drop -n)
uv run pytest -n auto --cov=webcam_cli --cov-report=term   # coverage; fail_under = 60

uv run webcam whoami                       # NOTE: `webcam`, not `webcam-cli`
uv run python -m webcam_cli learn --json   # equivalent module entry point
```

Lint — CI runs all five, and `markdownlint` is the one most often forgotten:

```bash
uv run black --check webcam_cli tests      # line-length 100
uv run isort --check-only webcam_cli tests
uv run flake8 webcam_cli tests
uv run bandit -c pyproject.toml -r webcam_cli
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
uv run teken cli doctor . --strict         # the agent-first rubric gate (26 checks)
```

The rubric gate is a hard CI gate, not advice. It runs the *installed* CLI and asserts learnability, JSON parseability, the `hint:`/exit-code error contract, and that every noun with action-verbs exposes `overview`. Any new noun you add must satisfy it.

## Architecture

The CLI is **cited, not imported**, from teken's `python-cli` reference (`teken cli cite`). That is why `dependencies = []` in `pyproject.toml` and `teken` is dev-only. This repo owns its copy outright — edit it freely; do not try to upgrade it from upstream.

**The zero-runtime-dependency posture is load-bearing for the capture-engine decision** (open question 2). Adding a Python capture library ends it. `shell-cli`'s zero-deps guard is the precedent cited in the brief.

### Adding a verb or noun

Each command module under `webcam_cli/cli/_commands/` exposes `register(sub)`; `_build_parser()` in `webcam_cli/cli/__init__.py` calls them in order. To add a surface you touch four places:

1. a module in `_commands/` with `register(sub)`, whose handler returns `int | None` and raises `CliError` on failure;
2. the `register()` call in `_build_parser()`;
3. an entry in `webcam_cli/explain/catalog.py` — **every registered path needs one**, and `tests/test_cli.py::test_every_catalog_path_resolves` enforces it;
4. `_VERBS` in `_commands/overview.py` and the command map in `_commands/learn.py`, which are hand-maintained duplicates of the surface.

Noun groups nest via `p.add_subparsers(..., parser_class=type(p))` — passing `parser_class` is required, or that noun's parse errors bypass the structured error contract and exit 2 instead of 1 (see `_commands/cli.py:40` and the test guarding it).

### The two stable contracts

**Errors** (`cli/_errors.py`, `cli/__init__.py`): every failure raises `CliError(code, message, remediation)`. `_dispatch` catches it, and wraps any other exception, so **no traceback ever reaches stderr**. Exit codes: `0` success, `1` user error, `2` environment error (not retryable without a config fix), `3` device busy/EBUSY (retryable), `4+` reserved. Text mode renders `error:` + `hint:`; JSON mode emits `{code, message, remediation}`.

`_CliArgumentParser` routes argparse's own errors through the same path. Because parse-time failures happen before `args.json` exists, `main()` pre-scans raw argv for `--json` into a class-level `_json_hint` — that is why the flag has to be sniffed twice.

**Output** (`cli/_output.py`): results to stdout, errors and diagnostics to stderr, **never mixed**. Every command takes `--json`. The rubric asserts stderr is empty on success.

The two failure classes the domain work added — `EBUSY` on an already-open camera, and present-but-forbidden device nodes — now have their own typed treatment inside this contract rather than sharing a generic `code=2`: `access.access_error()` maps `AccessState.BUSY` to `EXIT_BUSY_ERROR` (3) and `AccessState.FORBIDDEN` still to `EXIT_ENV_ERROR` (2), because busy is retryable and forbidden is not. See open questions 3 and 4.

### Identity plumbing

`_commands/whoami.py` hand-parses `culture.yaml` (no YAML dependency — the zero-deps rule) and walks up from `__file__`, deliberately *not* the CWD, so the identity is the agent's own and not whatever `culture.yaml` a caller happens to be standing in. In a wheel install no `culture.yaml` ships, and `doctor` degrades to one info check and exit 0. `doctor` and `overview` both build on `whoami.report()`.

## Domain constraints

These are design constraints, not trivia. Full derivation is in issue #1; the essentials, **re-verified on the operator's host on 2026-07-24**:

- **Four `/dev/video*` nodes, two physical cameras.** A UVC device commonly exposes a second node that yields no usable frames. Listing `/dev/video*` and calling it four cameras is wrong.
- **`/dev/videoN` numbering is plug-order, not identity — and it has already moved.** Between the brief and today, on the same machine, the C270 went from `video2` to `video0` and the Arducam from `video0` to `video2`; the ALSA cards swapped the same way (C270 `card 3` → `card 1`, Arducam `card 1` → `card 3`). This is not a hypothetical: `"capture from device 0"` is not a reproducible instruction, and any state you persist keyed on the index is already stale. `/dev/v4l/by-id/` carries vendor, product, and serial and is the stable handle.
- **Video and audio identifiers are unrelated.** The C270's camera is a `/dev/video*` node; its microphone is an ALSA card. Nothing in the numbering connects them — pairing means matching through USB topology.
- **A/V sets are not 1:1.** `Reachy Mini Audio` is a capture device with no camera. Any model assuming each webcam has a mic is already false on the first host tested.
- **Access comes from a seat ACL, not group membership.** The operator is *not* in the `video` group; `getfacl /dev/video0` shows `user:spark:rw-`, granted by logind to the active seat. **An agent running headless, in a container, or as a systemd unit will not get that ACL** and will fail to open a device that works fine from a desktop session. Since the users here are agents, this is the failure mode most likely to actually bite.
- **Never assume a capture backend.** Re-surveyed 2026-07-24: `gst-launch-1.0`/`gst-inspect-1.0` **are** present (the brief's "no backend" evidence is out of date), but `ffmpeg`, `v4l2-ctl`, and `fswebcam` are all still absent — do not shell out to them. Among GStreamer elements, `v4l2src`/`alsasrc`/`matroskamux`/`jpegenc`/`vp8enc`/`opusenc`/`tcpserversink` are present and **`x264enc` is absent**, so H.264/MP4 is off this host's menu and VP8 (video) plus Opus (audio) in Matroska is the *encoded* path. MJPG off the camera needs no encoder at all and is muxed through untouched, which is why it is the preferred video path rather than a transcode. `engine.detect()` reports the plugin map; route on what it says, never on what you expect.
- **Sensor warm-up is mandatory.** The first frames off a UVC camera are dark or badly exposed while auto-exposure settles. Grab-one-frame ships a black image — the single most common way a webcam tool "works" while producing useless output.

**Consent posture.** A CLI that lets an agent silently open a camera and microphone is a surveillance surface. Be precise about what can and cannot be promised: every capture can write to a named path with no hidden buffer, and every activation can be logged. A hardware activity light **cannot** be promised — that is device firmware, outside this tool's control. State the limit; never imply the tool prevents covert use.

## Design questions — how the brief's ten were settled

Issue #1 parked ten questions. The A/V streaming spec settled most of them **with the operator**, and the frame (`.devague/frames/a-v-streaming.json`) records who decided what. Do not silently re-open a settled one; the decisions are load-bearing for code already written.

| Q | Settled as |
|---|-----------|
| Q1 capture-as-write | **Yes**, and refined into three levels: default dry-run opens nothing, `--probe` enumerates for real (opens the camera, logged), `--apply` captures (logged). Recorded as deviation `d2` after the original "validate against the enumerated set *without* opening the device" proved self-contradictory. |
| Q2 engine + deps | **Shell out to `gst-launch-1.0`**; `dependencies = []` stays empty. Capability detection is mandatory and a missing engine is a typed exit-2 with an install hint. |
| Q3 headless permissions | `access.py` separates *absent* from *forbidden* and names the fix per subsystem — seat ACL (and its headless/container loss) for video, `audio`-group for ALSA. |
| Q4 exclusive access | Typed busy error, bounded time, never hangs; `find_holder()` names the holder when `/proc` permits and degrades to holder-unknown otherwise. |
| Q5 warm-up | Mandatory and overridable. **Defaults were provisional pending on-host measurement** — see the acceptance record for the measured figure. |
| Q6 negotiation | `validate_negotiation()` grants or raises; **never a silent fallback**. Partial requests act as a constraint set, and the payload always reports requested-vs-negotiated. |
| Q7 A/V pairing | Pairing is derived from the **shared sysfs USB parent**, never index correlation. `stream av` and `record --kind av` mux in-tool via `matroskamux`, as does every single-medium path. |
| Q8 consent | Every activation is logged (`~/.local/state/webcam-cli/activations.jsonl`, override `$WEBCAM_ACTIVATION_LOG`); capture writes only to the named target. The tool states plainly that it **cannot** promise a hardware activity light. |
| Q9 Reachy Mini | **Out of scope** for this iteration — only the Logitech C270 and its onboard mic are targeted (boundary claim `c27`). |
| Q10 `events-cli` | **Still open**, parked as follow-up `v2`: announcing stream start/stop is a consumer relationship to settle with the sibling agent. |

Still genuinely open, parked on the frame rather than guessed: **`v3`** — a WebSocket audio-only endpoint (realtime-API style PCM/Opus chunks) layered on the buffer, deferred so iteration 1 ships one uniform transport.

`--json` must stay complete enough that a follow-on tool never has to re-derive what was captured or from where. That is the property the on-host acceptance run exists to prove.

## Conventions and workflow

- **Every PR bumps the version** — even docs, config, or CI. Use the `version-bump` skill; the `version-check` CI job blocks merge otherwise.
- **PRs go through the `cicd` skill** (`devex pr` + SonarCloud gating). Online posts sign as `- webcam-cli (Claude)`; the `cicd` / `communicate` scripts resolve the nick from `culture.yaml` automatically, so don't sign bodies by hand.
- **Deploy**: pushing to `main` publishes to PyPI via Trusted Publishing; PRs do a TestPyPI dry-run. Both need the `pypi` / `testpypi` GitHub environments configured. `webcam_cli-0.6.1` is already published.
- **The vendored `.claude/skills/` are cited verbatim** — never reformat or edit their scripts. Re-sync from guildmaster per `docs/skill-sources.md`, which also records four tracked local divergences (`devex` rename, `ask-colleague` from colleague, and four skills vendored straight from devague). Prerequisites: `devex` (>=0.21) and `agtag` (>=0.1) on PATH; `colleague` optional.
- **Reach for `ask-colleague` reflexively**, not as a last resort — its value is a *second, independent mind* (a different backend/model), not a stronger one. Run `review` on a non-trivial committed diff before opening a PR, and `explore` for a fresh read of an unfamiliar area. Both are read-only in a throwaway worktree, so the reflex is always safe; side-effecting `write --apply` / `write --pr` needs the user's go-ahead. Its output is a second opinion to verify and own, never authority.
- **Memory discipline — recall before, remember after.** This repo's eidetic memory is **in-repo and public**: records resolve to `<repo-root>/.eidetic/memory`, committed and shared with the team and mesh peers (the `claude` and `colleague` backends share the `webcam-cli` scope). `/recall` before non-trivial work so you build on prior decisions instead of re-deriving them; `/remember` when a non-obvious decision, constraint, fix-and-why, or costly gotcha surfaces — as it happens, not at the end. A plain `/remember` lands in-repo; `--visibility private` routes to `$HOME/.eidetic/memory` instead. Don't store what the repo already records.
- **Worktrees you create by hand live in `../.worktrees.webcam-cli/<name>/`** — one repo-named directory beside the checkout:

  ```bash
  git worktree add ../.worktrees.webcam-cli/<name> -b <branch>
  ```

  Never a shared `../worktrees/`: this workspace holds many sibling projects, and a generic folder accumulates orphaned trees from several repos with nothing indicating ownership, so a stale-tree sweep cannot tell a live lane from junk. Scope the branch prefix to the work (`capture/t2`, not `agent/t2`) — plain `agent/*` collides with leftovers from earlier fan-outs and `git worktree add -b` fails on an existing branch. Remove with `git worktree remove <path>`; `git worktree prune` only clears metadata for directories already gone. Never `rm -rf` a worktree you did not create.

  The vendored `assign-to-workforce` skill's fan-out example uses both the shared path *and* `agent/<task-id>` branches. It is cited verbatim and must not be edited — override both when following it. `ask-colleague`'s read-only verbs create their own detached worktree under `${TMPDIR:-/tmp}` and reap it on an EXIT trap; those are outside this rule, not a violation of it.

## Layout

```text
webcam_cli/
  devices.py              logical device identity; enumerate_devices(root), resolve(selector)
  access.py               ok/absent/forbidden/busy + per-subsystem remediation; find_holder()
  engine.py               GStreamer boundary: detect, probe_formats, validate_negotiation, build_*_pipeline
  activation.py           append-only consent log; activation_scope() writes one line on exit
  cli/__init__.py         parser assembly, _dispatch, exception→exit-code translation
  cli/_errors.py          CliError + exit-code policy (stable contract)
  cli/_output.py          stdout/stderr split (stable contract)
  cli/_commands/          one module per verb, each exposing register(sub)
                          list_devices.py · stream.py (noun group) · record.py · the six template verbs
  explain/catalog.py      markdown keyed by command-path tuple — every path needs an entry
tests/                    smoke, introspection, and per-module suites
  conftest.py             resets the sticky _CliArgumentParser._json_hint between tests
  fixtures/               synthetic sysfs/proc trees: host-baseline, host-renumbered, camera-only, degraded
docs/specs/               exported devague specs (the converged frame)
docs/plans/               exported devague plans (tasks, waves, risks)
docs/skill-sources.md     skill provenance ledger + re-sync procedure
.devague/                 frame + plan state; questions/ and reviews/ are gitignored working state
.claude/skills/           vendored guildmaster skill kit (cite-don't-import; never edit)
culture.yaml              mesh identity (suffix + backend)
AGENTS.colleague.md       resident mesh prompt (backend: colleague)
```

Two testing notes that will bite otherwise. **`_json_hint` is class-level state** that `main()` sets from raw argv and never clears — correct inside one CLI process, sticky across a test session, so `tests/conftest.py` resets it around every test. A module ending on a `--json` call otherwise makes a later test that builds its own parser render errors as JSON. And **the fixture trees contain colons** in path names (`NVDA8000:01`, `3-1:1.0`) because real sysfs does and the interface colon is load-bearing for the USB-parent regex — that makes this repo un-checkout-able on Windows, which is acceptable for a V4L2/ALSA tool but is a constraint it did not previously have.

This file describes the repository **as it exists on disk today**. Keep claims grounded in checked-in reality; if a section drifts ahead of reality, mark it `(planned)` or move it under a `## Roadmap` heading.
