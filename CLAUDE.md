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

## Current state: scaffold, not the product

Nothing in the domain is implemented yet. What exists is the agent-first CLI skeleton scaffolded from `culture-agent-template`: the six template verbs (`whoami`, `learn`, `explain`, `overview`, `doctor`, `cli overview`), the error/output contract, CI, and the vendored skill kit. `webcam_cli/` contains **no capture code at all**.

Two consequences worth knowing before you touch anything:

1. **The self-description strings are still template prose.** `learn`, the `explain` catalog (`webcam_cli/explain/catalog.py`), and `overview`'s artifact list all describe this repo as "a clonable template for AgentCulture mesh agents". That is false — it is now a webcam agent. These are the agent-facing docs, so they must be rewritten as the domain surface lands, not after.
2. **The console command is `webcam`, but the CLI calls itself `webcam-cli`.** `[project.scripts]` installs `webcam`; argparse is built with `prog="webcam-cli"` (`webcam_cli/cli/__init__.py:73`), so `--help`, every error hint, `learn`, and the whole `explain` catalog instruct an agent to type `webcam-cli explain …` — which is not an installed binary. The three-way split is deliberate per the brief (command `webcam`, import package `webcam_cli`, dist `webcam-cli`), but the self-docs pointing at a nonexistent command is not. Fix `prog` and the doc strings together, or the rubric's learnability guarantee is a lie to its only readers.

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

**Errors** (`cli/_errors.py`, `cli/__init__.py`): every failure raises `CliError(code, message, remediation)`. `_dispatch` catches it, and wraps any other exception, so **no traceback ever reaches stderr**. Exit codes: `0` success, `1` user error, `2` environment error, `3+` reserved. Text mode renders `error:` + `hint:`; JSON mode emits `{code, message, remediation}`.

`_CliArgumentParser` routes argparse's own errors through the same path. Because parse-time failures happen before `args.json` exists, `main()` pre-scans raw argv for `--json` into a class-level `_json_hint` — that is why the flag has to be sniffed twice.

**Output** (`cli/_output.py`): results to stdout, errors and diagnostics to stderr, **never mixed**. Every command takes `--json`. The rubric asserts stderr is empty on success.

Two failure classes the domain work will add — `EBUSY` on an already-open camera, and present-but-forbidden device nodes — need their own typed treatment inside this contract rather than a generic `code=2`. See open questions 3 and 4.

### Identity plumbing

`_commands/whoami.py` hand-parses `culture.yaml` (no YAML dependency — the zero-deps rule) and walks up from `__file__`, deliberately *not* the CWD, so the identity is the agent's own and not whatever `culture.yaml` a caller happens to be standing in. In a wheel install no `culture.yaml` ships, and `doctor` degrades to one info check and exit 0. `doctor` and `overview` both build on `whoami.report()`.

## Domain constraints

These are design constraints, not trivia. Full derivation is in issue #1; the essentials, **re-verified on the operator's host on 2026-07-24**:

- **Four `/dev/video*` nodes, two physical cameras.** A UVC device commonly exposes a second node that yields no usable frames. Listing `/dev/video*` and calling it four cameras is wrong.
- **`/dev/videoN` numbering is plug-order, not identity — and it has already moved.** Between the brief and today, on the same machine, the C270 went from `video2` to `video0` and the Arducam from `video0` to `video2`; the ALSA cards swapped the same way (C270 `card 3` → `card 1`, Arducam `card 1` → `card 3`). This is not a hypothetical: `"capture from device 0"` is not a reproducible instruction, and any state you persist keyed on the index is already stale. `/dev/v4l/by-id/` carries vendor, product, and serial and is the stable handle.
- **Video and audio identifiers are unrelated.** The C270's camera is a `/dev/video*` node; its microphone is an ALSA card. Nothing in the numbering connects them — pairing means matching through USB topology.
- **A/V sets are not 1:1.** `Reachy Mini Audio` is a capture device with no camera. Any model assuming each webcam has a mic is already false on the first host tested.
- **Access comes from a seat ACL, not group membership.** The operator is *not* in the `video` group; `getfacl /dev/video0` shows `user:spark:rw-`, granted by logind to the active seat. **An agent running headless, in a container, or as a systemd unit will not get that ACL** and will fail to open a device that works fine from a desktop session. Since the users here are agents, this is the failure mode most likely to actually bite.
- **Assume no capture backend is installed.** On this host `ffmpeg`, `v4l2-ctl`, and `fswebcam` are all absent; only `arecord` and `pactl` are present. Detect capability and fail with a clear install hint — never assume.
- **Sensor warm-up is mandatory.** The first frames off a UVC camera are dark or badly exposed while auto-exposure settles. Grab-one-frame ships a black image — the single most common way a webcam tool "works" while producing useless output.

**Consent posture.** A CLI that lets an agent silently open a camera and microphone is a surveillance surface. Be precise about what can and cannot be promised: every capture can write to a named path with no hidden buffer, and every activation can be logged. A hardware activity light **cannot** be promised — that is device firmware, outside this tool's control. State the limit; never imply the tool prevents covert use.

## Open design questions

Ten questions in issue #1 are parked deliberately rather than guessed at. Settle them with the operator — do not quietly assume an answer while implementing. The ones that most constrain code shape (numbered as in the brief):

- **Q1 — Is capture a "write"?** The template contract is *dry-run by default, `--apply` commits*. Capture writes a file and energises hardware. The brief recommends treating it as a write (dry-run resolves the device, validates the format, and prints the path it would write) so agents can probe cheaply — recommended, not yet confirmed.
- **Q2 — Capture engine and dependency posture** — shell out versus Python libraries. See the zero-deps note above.
- **Q3 — Headless permission model** — `list` must distinguish *absent* from *present-but-forbidden* and name the fix. Silence turns into "the agent says there is no camera" when there plainly is one.
- **Q4 — Exclusive access** — V4L2 streaming is single-open. Fail fast with a typed error naming the holder if determinable; never hang.
- **Q6 — Format negotiation** — validate a requested (format, resolution, fps) against the enumerated set and report what you actually got, rather than silently falling back.
- **Q7 — A/V pairing** — is "record the C270's camera *and* its mic" one verb, and who muxes?

The rest (Q5 warm-up, Q8 consent posture) are folded into the constraints above; Q9 (Reachy Mini camera) and Q10 (`events-cli` relationship) are cross-repo and need the sibling agents.

Whatever is decided, `--json` must be complete enough that a follow-on tool never has to re-derive what was captured or from where.

The suggested starting surface, on top of the template verbs, is `webcam list`, `webcam describe <device>`, `webcam capture <device>`, `webcam record <device>`.

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
  cli/__init__.py         parser assembly, _dispatch, exception→exit-code translation
  cli/_errors.py          CliError + exit-code policy (stable contract)
  cli/_output.py          stdout/stderr split (stable contract)
  cli/_commands/          one module per verb, each exposing register(sub)
  explain/catalog.py      markdown keyed by command-path tuple — every path needs an entry
tests/                    smoke + introspection tests
.claude/skills/           vendored guildmaster skill kit (cite-don't-import; never edit)
docs/skill-sources.md     skill provenance ledger + re-sync procedure
culture.yaml              mesh identity (suffix + backend)
AGENTS.colleague.md       resident mesh prompt (backend: colleague)
```

This file describes the repository **as it exists on disk today**. Keep claims grounded in checked-in reality; if a section drifts ahead of reality, mark it `(planned)` or move it under a `## Roadmap` heading. The domain sections above are the one place that intentionally describes work not yet built — they are marked as such, and they cite issue #1 rather than asserting a design.
