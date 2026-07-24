"""Smoke tests for the CLI entry point and its verbs.

Two contracts beyond the per-verb smoke tests are enforced here, both of them
about the CLI's *self-description* rather than its behaviour:

* every path registered in the live parser tree has an ``explain`` entry, so the
  catalog cannot silently fall behind the surface (the converse of
  :func:`test_every_catalog_path_resolves`);
* no user-facing string presents ``webcam-cli`` as something to type. The
  console command is ``webcam``; ``webcam-cli`` names the project, the PyPI
  distribution, and the mesh nick, and stays correct in those roles.
"""

from __future__ import annotations

import argparse
import json
import re

import pytest

from webcam_cli import __version__
from webcam_cli.cli import _build_parser, main
from webcam_cli.explain import known_paths
from webcam_cli.explain.catalog import ENTRIES


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    # The installed console command is `webcam`, so that is what usage must say.
    assert "usage: webcam " in out or out.startswith("usage: webcam\n")
    assert "usage: webcam-cli" not in out


def test_unknown_command_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- whoami ---------------------------------------------------------------


def test_whoami_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nick: webcam-cli" in out
    assert "backend: colleague" in out
    assert "model:" in out


def test_whoami_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["nick"] == "webcam-cli"
    assert payload["version"] == __version__
    assert payload["backend"] == "colleague"


# --- learn ----------------------------------------------------------------


def test_learn_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn"])
    assert rc == 0
    out = capsys.readouterr().out
    assert len(out) >= 200
    assert "Exit-code policy" in out
    assert "--json" in out
    assert "explain" in out
    # The command map must name the capture surface, not a template's verbs.
    for verb in ("webcam list", "webcam stream video", "webcam record"):
        assert verb in out


def test_learn_states_the_hardware_activation_rule(capsys: pytest.CaptureFixture[str]) -> None:
    """The fact an agent most needs before invoking anything: what opens a camera.

    A CLI that can silently open a camera and microphone is a surveillance
    surface, so the dry-run / ``--probe`` / ``--apply`` split has to be legible
    from ``learn`` alone rather than only from a verb's ``--help``.
    """
    rc = main(["learn"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--probe" in out and "--apply" in out
    assert "OPENS the camera" in out
    assert "activation log" in out
    # The consent limit is stated, never overstated.
    assert "activity light CANNOT be promised" in out
    assert "does not prevent covert use" in out


def test_learn_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # `tool` is the distribution name; `command` is what an agent types.
    assert payload["tool"] == "webcam-cli"
    assert payload["command"] == "webcam"
    assert payload["version"] == __version__
    assert payload["json_support"] is True
    assert payload["explain_pointer"] == "webcam explain <path>"

    paths = [tuple(entry["path"]) for entry in payload["commands"]]
    for path in (("list",), ("stream", "video"), ("stream", "audio"), ("stream", "av")):
        assert path in paths
    assert ("record",) in paths

    assert set(payload["hardware_activation"]) >= {"default", "--probe", "--apply"}
    assert set(payload["bounds"]) == {"record", "stream"}


def test_learn_json_command_map_matches_the_registered_surface() -> None:
    """The hand-maintained command map in `learn` must not drift from the parser."""
    from webcam_cli.cli._commands.learn import _as_json_payload

    documented = {tuple(entry["path"]) for entry in _as_json_payload()["commands"]}
    missing = set(_registered_paths()) - documented
    assert not missing, f"registered but absent from `learn`: {sorted(missing)}"


# --- explain --------------------------------------------------------------


def test_explain_root(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("# webcam\n")


def test_explain_self(capsys: pytest.CaptureFixture[str]) -> None:
    # `explain <tool-name>` is what the agent-first rubric probes, and the tool
    # name is the installed console command.
    rc = main(["explain", "webcam"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("#")


def test_explain_resolves_the_distribution_name_as_a_legacy_alias(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`webcam-cli` is never advertised, but a guess at the dist name still works."""
    rc = main(["explain", "webcam-cli"])
    assert rc == 0
    assert capsys.readouterr().out == ENTRIES[("webcam",)]


def test_explain_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == ["whoami"]
    assert "webcam whoami" in payload["markdown"]


def test_explain_unknown_path_errors(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "nonexistent"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "hint:" in captured.err


def test_every_catalog_path_resolves(capsys: pytest.CaptureFixture[str]) -> None:
    for path in known_paths():
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()


# --- surface wiring -------------------------------------------------------


def _registered_paths(
    parser: argparse.ArgumentParser | None = None,
    prefix: tuple[str, ...] = (),
) -> list[tuple[str, ...]]:
    """Every command path the *live* parser tree exposes, depth-first."""
    parser = parser if parser is not None else _build_parser()
    paths: list[tuple[str, ...]] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                path = (*prefix, name)
                paths.append(path)
                paths.extend(_registered_paths(subparser, path))
    return paths


def test_the_capture_surface_is_registered() -> None:
    paths = set(_registered_paths())
    assert {("list",), ("record",), ("stream",)} <= paths
    assert {
        ("stream", "overview"),
        ("stream", "video"),
        ("stream", "audio"),
        ("stream", "av"),
    } <= paths


def test_every_registered_path_has_a_catalog_entry() -> None:
    """The converse of `test_every_catalog_path_resolves`.

    That test proves no catalog entry is dead; this one proves no registered
    verb is undocumented, which is the direction that actually breaks an agent.
    """
    undocumented = sorted(set(_registered_paths()) - set(known_paths()))
    assert not undocumented, f"registered but not in the explain catalog: {undocumented}"


def test_every_registered_verb_appears_in_overview() -> None:
    """`overview._VERBS` is a hand-maintained duplicate of the surface."""
    from webcam_cli.cli._commands.overview import _VERBS

    listed = " ".join(_VERBS)
    for (top, *_rest) in {p[:1] for p in _registered_paths()}:
        if top == "cli":
            continue  # described by its own `cli overview`, not the agent rollup
        assert top in listed, f"`{top}` is registered but missing from overview._VERBS"


# --- the command an agent is told to type ---------------------------------

# `webcam-cli` presented as something typable: the string followed by a flag, a
# placeholder, or one of the registered top-level verbs. Bare mentions of the
# project, the PyPI distribution, the mesh nick, or the `webcam-cli/` state
# directory are correct and deliberately not matched.
_DEAD_COMMAND_RE = re.compile(
    r"webcam-cli\s+(?:--?\w|<|list\b|stream\b|record\b|whoami\b|learn\b"
    r"|explain\b|overview\b|doctor\b|cli\b)"
)


def _help_texts(parser: argparse.ArgumentParser | None = None) -> list[str]:
    parser = parser if parser is not None else _build_parser()
    texts = [parser.format_help()]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for subparser in action.choices.values():
                texts.extend(_help_texts(subparser))
    return texts


def _agent_facing_texts(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    """Everything the CLI tells an agent about itself, keyed by where it came from."""
    texts = {f"--help #{i}": text for i, text in enumerate(_help_texts())}
    texts.update({f"explain {' '.join(path) or '<root>'}": body for path, body in ENTRIES.items()})

    for argv in (
        ["learn"],
        ["learn", "--json"],
        ["overview"],
        ["overview", "--json"],
        ["cli", "overview"],
        ["stream", "overview"],
        ["whoami"],
        ["doctor"],
    ):
        main(argv)
        captured = capsys.readouterr()
        texts[" ".join(argv)] = captured.out + captured.err
    return texts


def test_no_user_facing_string_presents_webcam_cli_as_a_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`webcam-cli explain …` is not an installed binary; instructing it is a dead end.

    The three-way split is deliberate — command ``webcam``, import package
    ``webcam_cli``, distribution ``webcam-cli`` — so this asserts only that the
    dist name is never presented as something to *type*, not that it is absent.
    """
    offenders = {
        source: _DEAD_COMMAND_RE.findall(text)
        for source, text in _agent_facing_texts(capsys).items()
        if _DEAD_COMMAND_RE.search(text)
    }
    assert not offenders, f"`webcam-cli` presented as a typable command in: {offenders}"


def test_no_template_prose_survives(capsys: pytest.CaptureFixture[str]) -> None:
    """The scaffold described this repo as a clonable template. It is a capture agent."""
    banned = ("clonable", "template", "scaffold", "rename the package", "mint a new agent")
    offenders = {
        source: [phrase for phrase in banned if phrase in text.lower()]
        for source, text in _agent_facing_texts(capsys).items()
    }
    offenders = {source: hits for source, hits in offenders.items() if hits}
    assert not offenders, f"template prose still in the self-description: {offenders}"


# --- the capture verbs, reached through main() ----------------------------
#
# Hardware posture: every test below is a dry run against an empty synthetic
# device tree or a parse-level failure. Nothing here opens a camera or a
# microphone — `--probe`/`--apply` belong to the on-host acceptance run.


def test_list_runs_through_main(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["list", "--json", "--root", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {"devices": [], "count": 0}
    assert captured.err == ""


def test_bare_stream_prints_the_noun_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["stream"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("# webcam stream")
    assert "What each invocation touches" in out


def test_stream_overview_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["stream", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "webcam stream"
    assert payload["sections"]


def test_stream_noun_parse_errors_keep_the_structured_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`parser_class` must propagate into the noun, or this exits 2 with no hint."""
    with pytest.raises(SystemExit) as exc:
        main(["stream", "video", "--bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "webcam stream video --help" in err


def test_record_runs_through_main_and_fails_typed(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An empty device tree means the selector resolves to nothing: a user error
    # carrying a remediation, routed through the JSON error contract.
    rc = main(
        ["record", "no-such-device", str(tmp_path / "clip.mkv"), "--root", str(tmp_path), "--json"]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["code"] == 1
    assert payload["remediation"]
