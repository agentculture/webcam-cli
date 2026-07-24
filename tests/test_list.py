"""Tests for ``webcam list`` (build-plan task t5).

Covers both acceptance criteria:

1. ``webcam list --json`` emits logical devices with stable id, device nodes,
   ALSA card, paired mic, and per-device access status — including the named
   fix when a subsystem is forbidden.
2. ``list`` exits 0 with a valid empty result when no devices are attached,
   and stderr is silent on success.

The verb is not wired into ``webcam_cli.cli.main`` yet (that lands in a later
task), so every test here builds a local ``argparse`` parser, calls
``list_devices.register(sub)`` on it, and invokes the handler directly —
never through ``webcam_cli.cli.main``.

Hardware posture: per the operator-approved deviation for this wave, no test
here opens, streams, or probes format capability on a device. The one thing
this module (via ``webcam_cli.access.check_access``) *does* do is a single
non-blocking permission probe per subsystem per device — exactly what the
acceptance criteria require in order to report access status. Every test that
needs a deterministic access outcome monkeypatches
``list_devices.check_access`` rather than relying on real hardware state, so
the suite is hermetic and passes identically on a host with no camera
attached. The one exception is a lenient real-``/`` smoke test at the bottom,
which asserts only shape and the never-raises guarantee — never a specific
access state.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from webcam_cli.access import AccessReport, AccessState, Holder
from webcam_cli.cli._commands import list_devices

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BASELINE = str(FIXTURES / "host-baseline")
RENUMBERED = str(FIXTURES / "host-renumbered")
CAMERA_ONLY = str(FIXTURES / "camera-only")
DEGRADED = str(FIXTURES / "degraded")

C270_ID = "usb-046d_C270_HD_WEBCAM_200901010001"
ARDUCAM_ID = "usb-Arducam_Technology_Co.__Ltd._Arducam_12MP_SN0001"
REACHY_ID = "usb-Pollen_Robotics_Reachy_Mini_Audio_202000386253800193"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="webcam")
    sub = parser.add_subparsers(dest="command", parser_class=type(parser))
    list_devices.register(sub)
    return parser


def _invoke(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    return 0 if result is None else result


def _by_id(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return {device["stable_id"]: device for device in report["devices"]}  # type: ignore[index]


def _always_ok(path: str, kind: str) -> AccessReport:
    return AccessReport(path=path, kind=kind, state=AccessState.OK, remediation="")


# ---------------------------------------------------------------------------
# register() wiring
# ---------------------------------------------------------------------------


def test_register_adds_list_with_json_and_root_flags() -> None:
    parser = _build_parser()
    args = parser.parse_args(["list"])
    assert args.command == "list"
    assert args.json is False
    assert args.root == "/"
    assert args.func is list_devices.cmd_list


def test_register_accepts_json_and_custom_root(tmp_path: Path) -> None:
    parser = _build_parser()
    args = parser.parse_args(["list", "--json", "--root", str(tmp_path)])
    assert args.json is True
    assert args.root == str(tmp_path)


# ---------------------------------------------------------------------------
# Criterion 2: exit 0 with a valid empty result, silent stderr on success
# ---------------------------------------------------------------------------


def test_empty_tree_json_is_zero_devices_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _invoke(["list", "--json", "--root", str(tmp_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload == {"devices": [], "count": 0}


def test_empty_tree_text_mode_exits_zero_with_silent_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _invoke(["list", "--root", str(tmp_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "no capture devices found" in captured.out


def test_nonexistent_root_is_still_a_clean_empty_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad --root must not raise — enumerate_devices degrades to empty."""
    missing = tmp_path / "does" / "not" / "exist"

    exit_code = _invoke(["list", "--json", "--root", str(missing)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"devices": [], "count": 0}


def test_success_never_writes_to_stderr_even_with_devices(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(list_devices, "check_access", _always_ok)

    exit_code = _invoke(["list", "--json", "--root", BASELINE])

    assert exit_code == 0
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Criterion 1: full schema — stable id, nodes, ALSA card, paired mic, access
# ---------------------------------------------------------------------------


def test_json_schema_carries_stable_id_nodes_card_and_mic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(list_devices, "check_access", _always_ok)

    exit_code = _invoke(["list", "--json", "--root", BASELINE])
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)

    assert report["count"] == 3
    assert {d["stable_id"] for d in report["devices"]} == {C270_ID, ARDUCAM_ID, REACHY_ID}

    c270 = _by_id(report)[C270_ID]
    assert c270["label"] == "C270 HD WEBCAM"
    assert c270["usb_path"] == "3-1"
    assert [n["path"] for n in c270["video_nodes"]] == ["/dev/video0", "/dev/video1"]
    assert c270["video_nodes"][0]["by_id"] == f"/dev/v4l/by-id/{C270_ID}-video-index0"
    assert c270["video_nodes"][0]["index"] == 0
    assert c270["capture_node"] == "/dev/video0"
    assert c270["capture_node_is_heuristic"] is True
    assert c270["audio"]["alsa_address"] == "hw:CARD=WEBCAM,DEV=0"
    assert c270["audio"]["card_id"] == "WEBCAM"
    assert c270["audio"]["index"] == 1


def test_video_and_audio_access_are_reported_separately(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On the reference host video and audio fail independently — prove the
    payload keeps two distinct statuses, not one folded device-level status."""

    def fake(path: str, kind: str) -> AccessReport:
        if kind == "video":
            return AccessReport(path=path, kind="video", state=AccessState.OK, remediation="")
        return AccessReport(
            path=path,
            kind="audio",
            state=AccessState.FORBIDDEN,
            remediation="join the 'audio' group and re-login",
        )

    monkeypatch.setattr(list_devices, "check_access", fake)

    exit_code = _invoke(["list", "--json", "--root", BASELINE])
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)

    c270 = _by_id(report)[C270_ID]
    assert c270["video_access"]["state"] == "ok"
    assert c270["video_access"]["remediation"] == ""
    assert c270["audio_access"]["state"] == "forbidden"
    assert "audio" in c270["audio_access"]["remediation"]


def test_forbidden_video_carries_the_named_fix_in_the_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The heart of criterion 1: a forbidden device names its own fix."""
    remediation = (
        "permission denied opening /dev/video0 — on an active desktop session, "
        "logind grants a per-seat ACL; add the invoking user to the 'video' group "
        "as a fallback"
    )

    def fake(path: str, kind: str) -> AccessReport:
        if kind == "video":
            return AccessReport(
                path=path, kind="video", state=AccessState.FORBIDDEN, remediation=remediation
            )
        return _always_ok(path, kind)

    monkeypatch.setattr(list_devices, "check_access", fake)

    exit_code = _invoke(["list", "--json", "--root", BASELINE])
    assert exit_code == 0  # forbidden device must not fail the whole listing
    report = json.loads(capsys.readouterr().out)

    c270 = _by_id(report)[C270_ID]
    assert c270["video_access"]["state"] == "forbidden"
    assert c270["video_access"]["remediation"] == remediation
    assert "video" in c270["video_access"]["remediation"]

    # And the text rendering surfaces the same hint for a human skimming a terminal.
    exit_code = _invoke(["list", "--root", BASELINE])
    assert exit_code == 0
    text = capsys.readouterr().out
    assert "forbidden" in text
    assert remediation in text


def test_absent_and_forbidden_are_distinguishable_in_the_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Present-but-forbidden must never be reported the same way as absent."""
    calls: list[str] = []

    def fake(path: str, kind: str) -> AccessReport:
        if kind == "video":
            calls.append(path)
            return AccessReport(
                path=path,
                kind="video",
                state=AccessState.ABSENT,
                remediation=f"{path} does not exist — check the camera is plugged in",
            )
        return _always_ok(path, kind)

    monkeypatch.setattr(list_devices, "check_access", fake)

    exit_code = _invoke(["list", "--json", "--root", BASELINE])
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)

    c270 = _by_id(report)[C270_ID]
    assert c270["video_access"]["state"] == "absent"
    assert c270["video_access"]["state"] != "forbidden"
    assert "does not exist" in c270["video_access"]["remediation"]
    # check_access was probed on the capture node specifically, not a guess.
    assert "/dev/video0" in calls
    assert "/dev/video1" not in calls  # the non-capture node is never probed


def test_busy_state_passes_through_with_holder_named_in_remediation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(path: str, kind: str) -> AccessReport:
        if kind == "video":
            return AccessReport(
                path=path,
                kind="video",
                state=AccessState.BUSY,
                remediation="V4L2 device is already open by zoom (pid 4242)",
                holder=Holder(pid=4242, command="zoom"),
            )
        return _always_ok(path, kind)

    monkeypatch.setattr(list_devices, "check_access", fake)

    exit_code = _invoke(["list", "--json", "--root", BASELINE])
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)

    c270 = _by_id(report)[C270_ID]
    assert c270["video_access"]["state"] == "busy"
    assert "zoom" in c270["video_access"]["remediation"]
    assert "4242" in c270["video_access"]["remediation"]


# ---------------------------------------------------------------------------
# A/V sets are not 1:1 — mic-only and camera-only devices
# ---------------------------------------------------------------------------


def test_mic_only_device_has_null_video_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(list_devices, "check_access", _always_ok)

    exit_code = _invoke(["list", "--json", "--root", BASELINE])
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)

    reachy = _by_id(report)[REACHY_ID]
    assert reachy["video_nodes"] == []
    assert reachy["capture_node"] is None
    assert reachy["capture_node_is_heuristic"] is None
    assert reachy["video_access"] is None
    assert reachy["audio"]["alsa_address"] == "hw:CARD=Audio,DEV=0"
    assert reachy["audio_access"]["state"] == "ok"


def test_camera_only_device_has_null_audio_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(list_devices, "check_access", _always_ok)

    exit_code = _invoke(["list", "--json", "--root", CAMERA_ONLY])
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)

    assert report["count"] == 1
    device = report["devices"][0]
    assert device["stable_id"] == C270_ID
    assert device["audio"] is None
    assert device["audio_access"] is None
    assert device["video_access"]["state"] == "ok"


# ---------------------------------------------------------------------------
# Degraded and renumbered trees must never raise
# ---------------------------------------------------------------------------


def test_degraded_tree_lists_present_devices_without_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(list_devices, "check_access", _always_ok)

    exit_code = _invoke(["list", "--json", "--root", DEGRADED])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)
    assert report["count"] == len(report["devices"])
    assert report["count"] > 0  # devices plainly present must not vanish


def test_renumbered_tree_keeps_the_same_stable_ids(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(list_devices, "check_access", _always_ok)

    exit_code = _invoke(["list", "--json", "--root", RENUMBERED])
    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)

    assert {d["stable_id"] for d in report["devices"]} == {C270_ID, ARDUCAM_ID, REACHY_ID}
    c270 = _by_id(report)[C270_ID]
    assert c270["capture_node"] == "/dev/video2"  # moved, but the id did not


# ---------------------------------------------------------------------------
# Text-mode rendering is readable and carries the same facts as JSON
# ---------------------------------------------------------------------------


def test_text_mode_lists_stable_id_label_and_usb_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(list_devices, "check_access", _always_ok)

    exit_code = _invoke(["list", "--root", BASELINE])
    assert exit_code == 0
    text = capsys.readouterr().out

    assert C270_ID in text
    assert "C270 HD WEBCAM" in text
    assert "3-1" in text
    assert "3 capture device(s)" in text


def test_text_mode_flags_capture_node_as_a_heuristic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(list_devices, "check_access", _always_ok)

    exit_code = _invoke(["list", "--root", BASELINE])
    assert exit_code == 0
    text = capsys.readouterr().out

    assert "heuristic" in text.lower()


# ---------------------------------------------------------------------------
# _audio_node_path: the /dev/snd path reconstruction
# ---------------------------------------------------------------------------


def test_audio_node_path_is_derived_from_index_and_alsa_address() -> None:
    from webcam_cli.devices import AudioCard

    card = AudioCard(
        index=1, card_id="WEBCAM", name="C270 HD WEBCAM", alsa_address="hw:CARD=WEBCAM,DEV=0"
    )
    assert list_devices._audio_node_path(card) == "/dev/snd/pcmC1D0c"


def test_audio_node_path_falls_back_to_device_zero_when_unparseable() -> None:
    from webcam_cli.devices import AudioCard

    card = AudioCard(index=2, card_id="X", name="X", alsa_address="garbage")
    assert list_devices._audio_node_path(card) == "/dev/snd/pcmC2D0c"


# ---------------------------------------------------------------------------
# build_report(): the function underneath the handler, exercised directly
# ---------------------------------------------------------------------------


def test_build_report_never_raises_for_a_bad_root(tmp_path: Path) -> None:
    report = list_devices.build_report(str(tmp_path / "nope"))
    assert report == {"devices": [], "count": 0}


# ---------------------------------------------------------------------------
# Real host: lenient smoke test — shape and never-raises only, no hardware
# assumptions, so this is safe on a machine with no camera attached.
# ---------------------------------------------------------------------------


def test_real_host_smoke_never_raises_and_stays_well_formed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = _invoke(["list", "--json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)
    assert isinstance(report["devices"], list)
    assert report["count"] == len(report["devices"])
    for device in report["devices"]:
        assert device["stable_id"]
        assert (device["video_access"] is None) == (device["capture_node"] is None)
        assert (device["audio_access"] is None) == (device["audio"] is None)
