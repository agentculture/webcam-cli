# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] - 2026-07-31

### Added

- Capability gating for the new container elements: a route whose encoder or parser is missing raises a typed exit-2 naming the element and its Debian package, instead of silently degrading. An MJPG-only host is not refused for lacking a VP8 encoder.
- Opus constraint on `record --kind audio`: `--rate` must be one Opus carries (8000/12000/16000/24000/48000 Hz) and `--channels` at most 8 — the ceiling comes from matroskamux's `audio/x-opus` sink caps, not from opusenc. Raised as a typed exit-1 rather than resampling, which would record at a rate other than the one requested and reported.

### Changed

- BREAKING for consumers of 0.8.0 artifacts: `record --kind video`/`--kind audio` output is now a container, not a raw stream. Anything parsing 0.8.0's headerless output must be updated. Treat any artifact captured with 0.8.0 or earlier as a raw byte stream regardless of its extension.
- Codec is chosen from the negotiated source format: MJPG is framed with `jpegparse` and carried through as Motion JPEG (passthrough, no re-encode, no generation loss), raw pixel formats go through `videoconvert ! vp8enc deadline=1`, and audio through `audioconvert ! opusenc`. `--kind av` keeps no per-branch codec tail.
- `build_video_pipeline`/`build_audio_pipeline` mux by default; `mux=False` opts out for the two callers that supply their own tail (`stream`'s sink chain, and `record`'s warm-up pre-roll, which must not spend CPU encoding frames it discards).

### Fixed

- `record --kind video` and `record --kind audio` wrote headerless raw byte streams with no container at all (issue #5). A file named `.mkv` held concatenated JPEG, or — for raw pixel formats — undecodable YUY2 with nothing recording its geometry. Both now write real Matroska. `record --kind av` and all three `stream` sub-verbs were never affected and are byte-identical.
- `record` now stops its pipeline with `gst-launch-1.0 -e`, so the muxer finalizes its Segment header on SIGINT. Without it a VP8 recording misreported its own duration (measured: 63 frames declaring 0.731 s instead of 4.267 s). No samples were ever lost; the container was describing itself wrongly.

## [0.8.0] - 2026-07-24

### Added

- On-host acceptance for the a-v-streaming plan (task t9): `docs/acceptance-a-v-streaming.md` records what was run against the operator's Logitech C270, what was observed, and six findings. `scripts/acceptance/run.sh` re-runs it headless and non-interactively; `scripts/acceptance/blind-consumer.sh` is structurally blind — its only input is the payload file, it never runs `webcam`, never reads `/dev`, and is never told the device id — and exercises all four announced attachment forms plus a live instrumented decode; `scripts/acceptance/warmup-measure.py` reproduces the auto-exposure settle measurement. Every media artifact is written under `$TMPDIR` and deleted on exit, including on failure.
- `engine.DEFAULT_WARMUP_FRAMES` — one measured constant backing both verbs' warm-up defaults, with the five cold-open runs recorded in its docstring — plus `engine.warmup_seconds()` and `engine.output_reports_device_busy()`.
- `access.busy_error()` — builds the typed BUSY error, holder lookup included, for callers that learned a device was busy some way other than `open(2)`.
- `record --json` reports `warmup_frames` and `warmup_basis` alongside `warmup_s`; `stream`'s `warmup.basis` replaces the former `warmup.provisional` field.

### Fixed

- **`--probe` and `--apply` were non-functional on any host running PipeWire** — that is, both `stream` and `record`, since every hardware path negotiates through `engine.probe_formats` first. PipeWire's device provider calls `gst_device_provider_hide_provider("v4l2deviceprovider")`, so its spelling is the only one `gst-device-monitor-1.0` emits, and it diverges from GStreamer's own in three independently fatal ways: no `device.path` property (the node path arrives as `api.v4l2.path` and `object.path = v4l2:/dev/videoN`), caps serialized without type annotations (`width=640`, not `width=(int)640`), and list-valued framerates (`framerate={ (fraction)30/1, ... }`). The parser now accepts every device-path key and both caps spellings, and expands list-valued fields into their discrete alternatives; ranges are still skipped rather than guessed at. The reference C270 now enumerates 198 formats where it previously enumerated none.
- **No consumer could decode a `webcam stream av` attachment point at all.** The announced consumer command gave the demuxer's two branches no `queue`, so both ran from one streaming thread and the pipeline stalled: 0 video and 1 audio buffer reached a consumer over 8 seconds, with no error raised. Bisected on hardware against a known-good recorded file, which failed identically with the announced shape and yielded 40 video / 268 audio buffers with a `queue` per branch. Single-medium consumers have one branch and were never affected.
- **A busy V4L2 camera did not produce the typed busy error.** uvcvideo permits several `open()`s of the same node and only refuses at `S_FMT`, so `access.check_access` reported a camera another process was actively streaming from as `ok`, while the same physical device's ALSA node correctly returned `EBUSY`. The failure surfaced as a generic "pipeline exited during warm-up" with the cause buried in an embedded gst-launch transcript. `stream` and `record` now recognise the engine's own wording and raise the typed busy error naming the holding process; measured at 1.36 s (video) and 0.31 s (audio) against a genuinely held C270.
- **The activation log dropped the negotiated format, the applied warm-up and the pipeline pid from every `stream --apply` record.** `activation_scope` copies the detail mapping it is handed, and `stream` mutated its own local dict after entering the scope. For a consent record, "the camera was opened" without "in what format, for how long, by which process" is most of the value gone.

- **A no-flag dry-run opened hardware, on two independent paths.** The rule stated in `learn`, the `explain` catalog and every sub-verb epilog is that no flag opens nothing and logs nothing; both halves were false. `record`'s dry-run called `access.check_access()`, which does a real `os.open()` on the camera and mic node — an *unlogged* hardware touch, the one thing the consent posture exists to prevent. Separately and more broadly, `engine.detect()` probed elements with plain `gst-inspect-1.0 <element>`, and that binary opens every `/dev/video*` node as a side effect of its own introspection, so a dry-run naming one device opened **all four** on the reference host, including two belonging to hardware explicitly out of scope for this iteration. Neither was visible to the test suite: the first was asserted *as correct* by a test that had encoded the bug, and the second is structurally invisible because `engine.detect` is monkeypatched by design. Dry-run now derives access from non-opening `stat`/`os.access` checks and reports `unknown` — never a false `ok` — where `EBUSY` genuinely cannot be determined without opening; element probing uses `gst-inspect-1.0 --exists`, verified to agree with the plain form on all thirteen probed elements and to open zero device nodes, behind a once-per-`detect()` capability check so a host lacking the flag falls back rather than silently reporting the engine missing. Confirmed by `strace` on the real CLI: zero opens of `/dev/video*` and `/dev/snd/*` for both `record` and `stream av` dry-runs, and no activation-log line written.
- **The capture target was a plug-order device index.** `LogicalDevice.capture_node` was a numeric `/dev/videoN` path and was then used as the v4l2 capture target, despite enumeration already being driven by `/dev/v4l/by-id`. `/dev/videoN` numbering is not identity and has already moved on the reference host between two enumerations of the same two cameras — the suite even asserted the capture node moved on renumbering, which was the exposure stated as an expectation. It is now the by-id link, which carries vendor, product and serial; the numeric path stays visible under `video_nodes[].path`, so no fact gained a second source of truth. Verified end to end: a real capture negotiates and writes through the by-id target.
- **Every `socket.bind()` failure was reported as "port already in use".** `stream`'s attachment-point check mapped invalid-port, permission and address-unavailable failures onto the one remediation that cannot fix them, sending an agent into an unbounded retry on the wrong axis. Bind failures now branch on `errno`, and `--port` gained a `0..65535` validator that routes through the structured error contract as a user error rather than an argparse crash.
- **`overview`'s verb list had drifted from the registered surface** — ten commands registered, eight listed, with the `cli` noun missing entirely. The mechanism was an escape hatch in the guard test that skipped precisely the noun that broke; it is replaced with a recursive walk of the real parser, mirroring the existing catalog-coverage test, and `cli_sections()` no longer re-declares two of the lines a second time.

### Changed

- **Exit code `3` now means "device busy", and is retryable.** `BUSY` and `FORBIDDEN` both exited `2`, so an agent could not tell "another process holds the camera, wait and retry" from "this container has no seat ACL, retrying will never work" without string-matching the message — the exact anti-pattern the structured error contract exists to remove. `3` was already reserved for this; `4+` is now the reserved range. The JSON error shape is unchanged at `{code, message, remediation}`.
- `devices._CARDS_LINE_RE` is written with possessive quantifiers, making it backtrack-free by construction rather than by measurement. The greedy form measured linear on every adversarial input we could build, but the property now holds regardless of input. Behaviour is unchanged, verified against the host's `/proc/asound/cards` and 500,000 fuzzed lines with zero divergence.
- Warm-up defaults reconciled on a measured number, closing plan risk `r3`. `stream` used 30 frames and `record` a flat 2.0 s, and neither had been measured. Across five cold opens of the reference C270, auto-exposure settled in 12-15 *frames* whether the camera ran at 30 fps or at 5 fps, while the wall-clock interval ranged 0.40-3.00 s — settle tracks frame count, not time, so a fixed-seconds default is right at one frame rate and under-warms at every lower one. Both verbs now derive their default from `engine.DEFAULT_WARMUP_FRAMES` converted through the negotiated fps: `record`'s default becomes 1.0 s at 30 fps (was 2.0 s) and 6.0 s at 5 fps (was 2.0 s). The 2x margin over measured settle is deliberate and is *not* itself measured — all runs were in one room over one evening — and `README.md`, `learn`, both catalog entries and the `--json` payload say so rather than implying the number is authoritative.

## [0.7.0] - 2026-07-24

### Added

- CLAUDE.md expanded from the bootstrap seed into a full runtime prompt for the webcam-cli domain, grounded in the issue #1 build brief and a re-survey of the operator's host: the capture lane and its out-of-lane siblings, the six-verb scaffold's architecture (the four places a new verb must touch, the parser_class threading that keeps nested nouns on the structured error contract, the double --json sniff, the CWD-independent identity walk), the device-identity and permission constraints, the six open design questions that most constrain code shape, and the repo conventions recovered from the pre-scaffold template (version-bump-every-PR, the cicd lane, worktree layout, memory discipline, ask-colleague reflex).

### Changed

- README.md rewritten from template-clone instructions to an agent description: adds a Status section stating plainly that the capture surface is not implemented yet, a Scope section naming what belongs to sibling agents, a planned-verb table alongside today's baseline, and a section on why device identity is the hard part. Drops the 'Make it your own' clone checklist, which described renaming a template this repo already is.

### Fixed

- README quickstart invoked `uv run webcam-cli whoami` / `learn`, which fails with 'Failed to spawn' — `[project.scripts]` installs the console command as `webcam`, not `webcam-cli`. Corrected to `webcam`, and the deliberate command/package/dist name split is now stated explicitly. The same mismatch inside the CLI's own self-description (argparse `prog="webcam-cli"`, so `--help`, every error hint, `learn`, and the whole `explain` catalog point agents at a command that is not installed) is documented in CLAUDE.md as an open defect rather than fixed here, since it changes agent-visible output under the rubric gate. The README now also carries a Known-defect note so a reader who follows a `hint:` into the nonexistent `webcam-cli` binary can reconcile it; the fix is tracked in [#3](https://github.com/agentculture/webcam-cli/issues/3), which pairs it with the self-description rewrite because both touch the same strings.

## [0.6.1] - 2026-07-20

### Added

- **Worktree location convention** in `CLAUDE.md` — every worktree you create
  by hand (workforce fan-out lanes, scratch checkouts) lives in
  `../.worktrees.webcam-cli/<name>/`, one
  repo-named directory beside the checkout, replacing a shared `../worktrees/`
  folder. This workspace holds many sibling projects, so a generic shared
  folder accumulates orphaned trees from several repos at once with nothing
  indicating ownership — a stale-tree sweep can't tell a live lane from junk.
  Matches the convention already documented in sibling repo `reachy-mini-cli`.
  Adds branch-prefix guidance (scope the prefix to the work; plain `agent/*`
  collides with leftovers from earlier fan-outs and fails `git worktree add
  -b`), and notes that the vendored `assign-to-workforce` skill uses both the
  shared path *and* `agent/<task-id>` branches in its fan-out example — it is
  cited verbatim and must not be edited, so both are overridden when following
  it. Teardown guidance names `git worktree remove <path>` as the verb that
  actually deletes a worktree; `git worktree prune` only clears metadata for
  directories that are already gone. Tool-managed throwaways are explicitly
  out of scope: `ask-colleague`'s read-only verbs create a detached worktree
  under `${TMPDIR:-/tmp}` and reap it on an EXIT trap, so they never persist
  to need an owner.

## [0.6.0] - 2026-07-18

### Added

- **Four devague-origin skills re-vendored into `.claude/skills/`**
  (cite-don't-import), synced to the fixed devague source
  (devague#74/#75/#76):
  - `challenge` — a risk-scaled blind-spot discovery pass that runs between
    `/think` and `/spec-to-plan`, routing findings back through the existing
    deterministic moves as human-adjudicated proposals.
  - `scope` — the idea→scope leg that surveys the surfaces an idea touches
    before framing, seeding the Announcement Frame with provenance-backed
    boundary/non-goal/assumption claims.
  - `deviate` — stops an in-flight `assign-to-workforce` run when execution
    must diverge from the confirmed plan and records the divergence as a
    first-class, append-only deviation record.
  - `summarize-delivery` — closes the loop after an `assign-to-workforce`
    run with a planned-vs-actual accountability artifact.

  These four originate in `devague` and are re-broadcast via guildmaster; see
  `docs/skill-sources.md` for provenance.

## [0.5.0] - 2026-06-24

### Added

- **Memory-discipline "Conventions and workflow" section in `CLAUDE.md`** — a
  per-task *recall-before / remember-after* convention (scope localized to this
  repo's nick) so the vendored `remember` / `recall` skills are actually used,
  not just present: `/recall` before non-trivial work to build on prior
  decisions instead of re-deriving them, and `/remember` when a non-obvious
  decision, constraint, fix-and-why, or hard-won gotcha surfaces. The section
  documents this repo's memory as **in-repo and public** — records resolve to
  `<repo-root>/.eidetic/memory` (committed, team- and mesh-shared). Inserted
  idempotently (skipped if already present), slotted under an existing
  "Conventions and workflow" heading when one exists, else appended.

### Changed

- **Refreshed the `remember` + `recall` wrappers from eidetic-cli 0.10.0**
  (cite-don't-import) — picks up eidetic's **project-local store default**: the
  files backend now resolves per record by visibility — PUBLIC records inside a
  git repo go to `<repo-root>/.eidetic/memory` (committed, team-shared), PRIVATE
  records (or any record outside a repo) go to `$HOME/.eidetic/memory` (never
  committed), an explicit `EIDETIC_DATA_DIR` still wins, and recall reads both
  stores and merges. Also carries the 0.9.3 hardening (interactive-stdin guard,
  `help` as a search term, SIGPIPE-safe suffix parsing). **Recipe policy
  override (the wrappers here are NOT byte-verbatim):** the injected default
  visibility is flipped from eidetic's `private` to **`public`**, so a plain
  `/remember` lands the note in `./.eidetic/memory` in this repo, kept as part
  of the repo — pass `--visibility private` to route a record to `$HOME`
  instead. `remember` drives `eidetic remember` (idempotent upsert of one JSON
  record or an NDJSON batch on stdin); `recall` drives `eidetic recall` with
  four search modes (exact / approximate / keyword / hybrid). Each `SKILL.md` is
  localized only in the illustrative `--scope <nick>` examples (Provenance keeps
  "First-party to eidetic-cli"). Runtime dep: the `eidetic` CLI on PATH (else a
  local eidetic-cli checkout with `uv`) — **`eidetic >= 0.10.0`** for the
  in-repo routing; on an older CLI the public records still work but are stored
  in `$HOME/.eidetic/memory` instead of in-repo. Propagated by rollout-cli's
  `eidetic-memory` recipe.

## [0.4.0] - 2026-06-23

### Added

- **Vendored the `remember` + `recall` memory skills from eidetic-cli**
  (cite-don't-import) — the write/read halves of eidetic's shared
  `$HOME/.eidetic/memory` surface, so this agent (Claude and its colleague
  backend) can persist facts across sessions and recall them later, sharing
  one store.
  `remember` drives `eidetic remember` (idempotent upsert of one JSON record or
  an NDJSON batch on stdin, dedup by id + content hash); `recall` drives
  `eidetic recall` with four search modes — exact / approximate / keyword /
  hybrid — each hit carrying text, full provenance metadata, a relevance score,
  and a freshness signal. The `.sh` wrappers are byte-verbatim from eidetic-cli
  (their first-party origin); each `SKILL.md` is localized only in the
  illustrative `--scope <nick>` examples (Provenance keeps "First-party to
  eidetic-cli"). Both default to this agent's PRIVATE scope, reading the suffix
  from `culture.yaml`. Runtime dep: the `eidetic` CLI on PATH (else a local
  eidetic-cli checkout with `uv`). Propagated by rollout-cli's `eidetic-memory`
  recipe.

## [0.3.4] - 2026-06-20

### Fixed

- Identity docs and self-description strings still claimed `backend: claude`
  (prompt file `CLAUDE.md`), but this template was promoted to a colleague
  resident in #14/#15: `culture.yaml` declares `backend: colleague` (Qwen) with
  `AGENTS.colleague.md` as the resident prompt. Corrected the stale claim in
  `CLAUDE.md` (Identity section), `README.md`, `docs/skill-sources.md`, and the
  two CLI description strings (`overview` artifacts and `explain doctor`). The
  `doctor` backend→prompt-file mapping and the tests were already on
  `colleague`; this aligns the prose and self-description with them.

## [0.3.3] - 2026-06-20

### Fixed

- pyproject.toml: correct the `license` field and PyPI classifier from MIT to
  Apache-2.0 to match the `LICENSE` file. The README License section was already
  corrected in 0.3.2, but the package metadata was missed; the built wheel now
  reports `License-Expression: Apache-2.0`.

## [0.3.2] - 2026-06-18

### Added

- ask-colleague skill: `monitor`/`guide`/`stop` pilot verbs plus a `--watch`
  flag to dispatch, watch the live feed of, send mid-flight guidance to, and
  cooperatively stop a running colleague flight (re-vendored from colleague).

### Changed

- README: correct the License section from MIT to Apache 2.0 to match the
  `LICENSE` file.

## [0.3.1] - 2026-06-13

### Changed

- CLAUDE.md: add a convention to reach for the `ask-colleague` skill reflexively
  for explore/review/write/grade — read-only `review`/`explore` are always safe;
  side-effecting `write` needs the user's go-ahead.

## [0.3.0] - 2026-06-13

### Added

- AGENTS.colleague.md resident prompt file (backend colleague <-> AGENTS.colleague.md)

### Changed

- Promote agent identity to a colleague resident: culture.yaml backend
  claude -> colleague with a pinned model. The `doctor` backend-consistency
  map gains `colleague` -> AGENTS.colleague.md.

## [0.2.1] - 2026-06-12

### Changed

- **Re-vendored the `ask-colleague` skill from colleague (now 1.7.0, up from the
  0.39.2 sync)** — the wrapper had drifted multiple releases behind origin. Picks
  up the `clean` verb (reap stale/corrupt `colleague/*` branches + orphaned
  `.colleague/` artifacts a crashed run left behind), the `--json` flag on every
  verb (result JSON on stdout, diagnostics/digest on stderr), the
  `_colleague_via_uv` local-dev resolution that honors `--repo`, and the
  tri-state (0/1/2) exit-code contract. `scripts/ask-colleague.sh` + `prompts/`
  are byte-identical to the origin; `SKILL.md` diverges only in the one
  consumer-identifying Provenance clause (`webcam-cli vendors from
  guildmaster`). `docs/skill-sources.md` sync row updated to
  `2026-06-12 (colleague 1.7.0, direct)`. Refs: colleague#183, #186.

## [0.2.0] - 2026-06-06

### Added

- **`ask-colleague` skill** (`.claude/skills/ask-colleague/`) — the first-party front door to the `colleague` CLI (the renamed `convertible`). On top of `explore` / `review` / `write` it adds a `feedback` verb (grade a finished work item — the ROI loop), and `write` now **previews by default** in a throwaway worktree (no side effects) unless `--apply` / `--pr` is given. Reach for it reflexively — `review` for a diverse second opinion on a committed diff before opening a PR, `explore` for a fresh read of an unfamiliar area.

### Changed

- **Replaced the `outsource` skill with `ask-colleague`.** `outsource` was renamed to `ask-colleague` upstream ([colleague#148](https://github.com/agentculture/colleague/pull/148)). Because guildmaster has not re-broadcast the rename yet (its kit still ships the old `outsource`), `ask-colleague` is vendored **directly from the sibling `colleague` checkout** rather than from guildmaster — a tracked local divergence recorded in `docs/skill-sources.md`, parallel to the `agex` → `devex` one. Vendored verbatim except one consumer-identifying clause in the Provenance paragraph.
- **Ledger + CLAUDE.md + `.gitignore`:** point `docs/skill-sources.md` and the CLAUDE.md Skills section at `colleague` / `ask-colleague`, swap the *optional* runtime prerequisite `convertible` → `colleague` (env prefix `CONVERTIBLE_*` → `COLLEAGUE_*`, with the legacy names kept as a deprecated fallback), and gitignore the `.colleague/` run-artifact dir the skill writes (plus the stale `.agex/`).

## [0.1.4] - 2026-05-31

### Added

- **Vendor the `outsource` skill** (`.claude/skills/outsource/`) from
  guildmaster's canonical copy (origin
  [`agentculture/convertible`](https://github.com/agentculture/convertible),
  re-broadcast via guildmaster — guildmaster
  [#51](https://github.com/agentculture/guildmaster/pull/51)). Every agent
  cloned from this template now inherits the ability to hand a scoped task to a
  *different* engine/mind: `explore` (read-only investigation), `review` (a
  diverse second opinion on the committed diff), and `write` (delegate a small
  implementation). `explore`/`review` run isolated in a throwaway `git worktree`;
  `write` refuses a dirty tree. Fulfils
  [#8](https://github.com/agentculture/webcam-cli/issues/8).
- **Ledger + CLAUDE.md:** record `outsource` in `docs/skill-sources.md`
  (origin = convertible, re-broadcast via guildmaster; vendored verbatim — it
  already carries `type: command`) and document its *optional* runtime
  dependency on the `convertible` CLI (the skill exits with an install hint if
  absent, so a clone that never uses it is unaffected).

### Changed

### Fixed

## [0.1.3] - 2026-05-31

### Changed

- Expanded the clone-and-rename instructions in `CLAUDE.md`: added `README.md` to
  the rename targets and a portable `git grep` discovery command so a cloner can
  find every occurrence of the template name (hard-coded in ~100 places across the
  package, including the CLI command files and `_ISSUES_URL` in
  `webcam_cli/cli/__init__.py`) rather than renaming by hand.
- Synced `README.md`'s "Make it your own" checklist with `CLAUDE.md`: it now lists
  `README.md` itself as a rename target and points to `CLAUDE.md`'s discovery
  command as the authoritative procedure, so the two onboarding checklists no
  longer drift.

## [0.1.2] - 2026-05-30

### Changed

- Renamed the PR-lifecycle CLI references `agex` / `agex-cli` to `devex` (same
  tool, new name) across `CLAUDE.md`, `docs/skill-sources.md`, `.gitignore`, and
  the vendored `cicd`, `assign-to-workforce`, and `communicate` skills — the
  `cicd` scripts now invoke `devex pr`.
- Logged the vendored-skill in-place patch as a local divergence in
  `docs/skill-sources.md`; the matching canonical rename is tracked upstream for
  guildmaster in
  [agentculture/guildmaster#48](https://github.com/agentculture/guildmaster/issues/48)
  so a future re-sync reconciles cleanly.
- Aligned the documented `devex` version floor to `>=0.21` across the vendored
  `cicd` `SKILL.md` and `workflow.sh` install hint (were `>=0.1`), matching
  `docs/skill-sources.md` and the `await`-era feature set; flagged upstream on
  guildmaster#48.

### Fixed

- SonarCloud now reports code coverage — added `relative_files = true` to
  `[tool.coverage.run]` so `coverage.xml` emits repo-relative paths that map to
  `sonar.sources=webcam_cli` (absolute / `.venv` paths were dropped
  as unmappable). Mirrors the sibling `convertible` setup.

## [0.1.1] - 2026-05-26

### Changed

- **CI gates on the SonarCloud quality gate**
  ([issue #3](https://github.com/agentculture/webcam-cli/issues/3)) —
  added `sonar.qualitygate.wait=true` to `sonar-project.properties` so a failing
  gate fails the `test` job when `SONAR_TOKEN` is set. Token-less repos and fork
  PRs remain green (the scan step is guarded by `if: env.SONAR_TOKEN != ''`).

## [0.1.0] - 2026-05-26

### Added

- **Onboarded into the AgentCulture mesh** ([issue #1](https://github.com/agentculture/webcam-cli/issues/1)).
- **Agent-first CLI** cited from teken's (`afi-cli`) `python-cli` reference
  (`teken cli cite`) — verbs `whoami`, `learn`, `explain`, `overview`, `doctor`,
  and the `cli` noun group. Runtime is self-contained (`dependencies = []`);
  `teken>=0.8` is a dev dependency only. Passes the seven-bundle agent-first
  rubric (`teken cli doctor . --strict`). `doctor` checks the agent-identity
  invariants (prompt-file-present, backend-consistency, skills-present).
- **Mesh identity**: `culture.yaml` (`suffix: webcam-cli`,
  `backend: claude`) and the matching `CLAUDE.md` prompt file.
- **Canonical guildmaster skill kit** (11 skills) vendored under
  `.claude/skills/` (cite-don't-import): `agent-config`, `assign-to-workforce`,
  `cicd`, `communicate`, `doc-test-alignment`, `pypi-maintainer`, `run-tests`,
  `sonarclaude`, `spec-to-plan`, `think`, `version-bump`. Every `SKILL.md`
  carries `type: command` (load-bearing for the culture/claude backend);
  `cicd` / `communicate` consumer-identifying prose adapted, all script bodies
  verbatim. Provenance in `docs/skill-sources.md`. Three skills (`think`,
  `spec-to-plan`, `assign-to-workforce`) originate in `devague`, re-broadcast
  via guildmaster.
- **Build + deploy baseline**: `pyproject.toml` (hatchling), `tests/` (pytest,
  xdist, coverage), `.github/workflows/{tests,publish}.yml` (CI rubric/lint gate,
  PyPI Trusted Publishing), `.flake8`, `.markdownlint-cli2.yaml`,
  `sonar-project.properties`, and `.claude/skills.local.yaml.example`.

### Changed

### Fixed
