"""``webcam overview`` — read-only descriptive snapshot of the agent.

Describes the agent to an agent reader: identity (from culture.yaml), the verb
surface, which invocations energize hardware, the identity/access contracts the
capture verbs obey, and the consent posture — stated with its limits, never
overstated. The shared section/render helpers here are reused by the ``cli``
noun's ``overview`` (see :mod:`webcam_cli.cli._commands.cli`) and by the
``stream`` noun's (see :mod:`webcam_cli.cli._commands.stream`).

Descriptive verbs never hard-fail on a missing target path — an optional
positional ``target`` is accepted and ignored (overview describes this agent,
not an external target), so ``overview <bogus-path>`` still exits 0.
"""

from __future__ import annotations

import argparse

from webcam_cli import activation
from webcam_cli.cli._commands.whoami import report
from webcam_cli.cli._output import emit_result

_VERBS = [
    "list — attached capture devices: stable id, nodes, paired mic, access state",
    "stream video|audio|av <device> — serve a live attachment point (unbounded)",
    "stream overview — describe the stream verb group (bare 'stream' does the same)",
    "record <device> <path> — record a bounded clip to one file (a duration cap always applies)",
    "whoami — identity probe (nick, version, backend, model)",
    "learn — structured self-teaching prompt",
    "explain <path> — markdown docs for a topic",
    "overview — this descriptive snapshot",
    "doctor — check the agent-identity invariants",
    "cli overview — describe the CLI surface itself (bare 'cli' does the same)",
]

#: The single most important thing an agent needs before invoking anything here:
#: whether the invocation will switch a camera on. ``stream`` and ``record``
#: share this split, and it is readable from the flags alone.
_HARDWARE = [
    "default (no flag) — dry run: resolves the device, validates the request, prints "
    "the plan. Opens nothing, logs nothing.",
    "--probe — enumerates the device's real formats, which OPENS the camera; written "
    "to the activation log.",
    "--apply — opens the device and streams or records; written to the activation log.",
    "list — opens nothing beyond one non-blocking permission probe per node.",
]

_CONTRACTS = [
    "identity is the stable id from /dev/v4l/by-id, never /dev/videoN: node numbers are "
    "plug-order and the reference host's two cameras have already swapped",
    "access is reported per subsystem — absent, forbidden and busy are distinct states, "
    "each naming its own fix (seat ACL for video, 'audio' group for ALSA)",
    "record is bounded by construction (a duration cap always applies, no flag means "
    "'forever'); stream is unbounded by construction (there is no --duration)",
    "an unsupported (format, resolution, fps) is a typed user error naming the "
    "enumerated alternatives — never a silent fallback",
]


def _consent_items() -> list[str]:
    return [
        f"activation log: {activation.log_path()} (override ${activation.ENV_LOG_PATH})",
        "a capture writes only to the path you name — no hidden buffer, never to stdout",
        "a hardware activity light CANNOT be promised: that is device firmware, outside "
        "this tool's control. This tool records activations; it does not prevent covert use.",
    ]


def agent_sections() -> list[dict[str, object]]:
    """Sections describing the agent (used by the global verb)."""
    ident = report()
    return [
        {
            "title": "Identity",
            "items": [
                f"nick: {ident['nick']}",
                f"version: {ident['version']}",
                f"backend: {ident['backend']}",
                f"model: {ident['model']}",
            ],
        },
        {"title": "Verbs", "items": list(_VERBS)},
        {"title": "What touches the hardware", "items": list(_HARDWARE)},
        {"title": "Contracts", "items": list(_CONTRACTS)},
        {"title": "Consent", "items": _consent_items()},
    ]


def cli_sections() -> list[dict[str, object]]:
    """Sections describing the CLI surface itself (used by `cli overview`).

    ``_VERBS`` is the single source of truth for the registered surface (see
    :func:`test_every_registered_path_appears_in_overview_verbs` in
    ``tests/test_cli.py``), so this reuses it verbatim rather than
    re-declaring ``stream overview``/``cli overview`` here too.
    """
    return [
        {
            "title": "Verbs",
            "items": list(_VERBS),
        },
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "results to stdout, errors/diagnostics to stderr (never mixed)",
                "exit codes: 0 success, 1 user error, 2 environment error, "
                "3 device busy (retryable), 4+ reserved",
                "writes and hardware activation are opt-in: --apply commits, --probe opens "
                "the camera to enumerate formats, and the default is a dry run",
            ],
        },
    ]


def render_text(subject: str, sections: list[dict[str, object]]) -> str:
    lines = [f"# {subject}", ""]
    for section in sections:
        lines.append(f"## {section['title']}")
        for item in section["items"]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip()


def emit_overview(subject: str, sections: list[dict[str, object]], *, json_mode: bool) -> None:
    if json_mode:
        emit_result({"subject": subject, "sections": sections}, json_mode=True)
    else:
        emit_result(render_text(subject, sections), json_mode=False)


def cmd_overview(args: argparse.Namespace) -> int:
    # `target` is accepted for rubric compatibility (descriptive verbs must not
    # hard-fail on a missing path) but overview describes this agent itself.
    emit_overview(
        "webcam",
        agent_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "overview",
        help="Read-only descriptive snapshot of the agent (identity, verbs, contracts, consent).",
    )
    p.add_argument(
        "target",
        nargs="?",
        help="Ignored — overview always describes this agent itself. Accepted so a "
        "stray path argument never hard-fails.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview)
