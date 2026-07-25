"""Tests for :mod:`webcam_cli.devices` — the device identity core.

Every test runs against a synthetic filesystem tree under ``tests/fixtures``
rather than the host, so the suite is hermetic and never opens a device.

Four trees, all derived from the operator's host on 2026-07-24:

``host-baseline``
    The host exactly as it is: four ``/dev/video*`` nodes that are only *two*
    physical cameras (C270 on USB ``3-1``, Arducam on USB ``5-1.3``), three USB
    ALSA capture cards, one of which (``Reachy Mini Audio``) has no camera at
    all, plus a non-USB playback-only NVIDIA HDA card that must not enumerate.
    ``proc/asound/cards`` is byte-identical to the host's.

``host-renumbered``
    The same three physical devices replugged: the C270 and the Arducam swap
    ``/dev/videoN`` numbers, ALSA card numbers *and* USB ports. Nothing about
    their identity changed, so every stable id must be unchanged.

``camera-only``
    One UVC camera with two nodes and no sound subsystem at all — no
    ``/proc/asound`` directory exists. Guards the "A/V sets are not 1:1" rule
    from the other side.

``degraded``
    A partially-readable tree: a camera whose ``/sys/class/video4linux`` link
    never appeared, a by-id entry that is not a symlink, a udev leftover that is
    not a video node, a USB microphone whose descriptor files are all missing,
    and an analog capture card with no USB parent. Nothing here may raise, and
    — the point of the fixture — a device that is plainly present must still be
    listed rather than silently disappear.
"""

from __future__ import annotations

import re
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from webcam_cli.cli._errors import EXIT_USER_ERROR, CliError
from webcam_cli.devices import (
    _CARDS_LINE_RE,
    AudioCard,
    LogicalDevice,
    VideoNode,
    _capture_target,
    enumerate_devices,
    resolve,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BASELINE = str(FIXTURES / "host-baseline")
RENUMBERED = str(FIXTURES / "host-renumbered")
CAMERA_ONLY = str(FIXTURES / "camera-only")
DEGRADED = str(FIXTURES / "degraded")

_NUMERIC_NODE_RE = re.compile(r"^/dev/video\d+$")

C270_ID = "usb-046d_C270_HD_WEBCAM_200901010001"
ARDUCAM_ID = "usb-Arducam_Technology_Co.__Ltd._Arducam_12MP_SN0001"
REACHY_ID = "usb-Pollen_Robotics_Reachy_Mini_Audio_202000386253800193"


def by_id(devices: tuple[LogicalDevice, ...]) -> dict[str, LogicalDevice]:
    return {device.stable_id: device for device in devices}


# --------------------------------------------------------------------------
# Criterion 1: multi-node UVC devices collapse into one logical entry
# --------------------------------------------------------------------------


def test_four_video_nodes_enumerate_as_two_cameras_plus_one_mic() -> None:
    devices = enumerate_devices(root=BASELINE)
    assert [device.stable_id for device in devices] == sorted([C270_ID, ARDUCAM_ID, REACHY_ID])
    cameras = [device for device in devices if device.video_nodes]
    assert len(cameras) == 2


def test_c270_two_video_nodes_collapse_into_one_logical_device() -> None:
    c270 = by_id(enumerate_devices(root=BASELINE))[C270_ID]
    assert len(c270.video_nodes) == 2
    assert [node.path for node in c270.video_nodes] == ["/dev/video0", "/dev/video1"]
    assert [node.index for node in c270.video_nodes] == [0, 1]


def test_stable_id_is_the_by_id_handle_carrying_the_serial() -> None:
    c270 = by_id(enumerate_devices(root=BASELINE))[C270_ID]
    assert c270.stable_id.endswith("200901010001")
    assert c270.video_nodes[0].by_id == f"/dev/v4l/by-id/{C270_ID}-video-index0"
    assert c270.video_nodes[1].by_id == f"/dev/v4l/by-id/{C270_ID}-video-index1"


def test_capture_node_is_the_lowest_indexed_node_of_the_pair() -> None:
    devices = by_id(enumerate_devices(root=BASELINE))
    assert devices[C270_ID].capture_node == f"/dev/v4l/by-id/{C270_ID}-video-index0"
    assert devices[ARDUCAM_ID].capture_node == f"/dev/v4l/by-id/{ARDUCAM_ID}-video-index0"


def test_capture_node_is_the_stable_by_id_handle_not_a_plug_order_index() -> None:
    """The capture target must not be a /dev/videoN path.

    /dev/videoN numbering is plug-order, and it has already moved on this
    host between two enumerations of the same hardware. Anything that opens
    a device must go through the by-id link, which carries vendor, product
    and serial. The numeric node stays visible under `video_nodes[].path`.
    """
    for device in enumerate_devices(root=BASELINE):
        if device.capture_node is None:
            continue
        assert device.capture_node.startswith("/dev/v4l/by-id/"), device.capture_node
        assert not _NUMERIC_NODE_RE.match(device.capture_node), device.capture_node


def test_capture_node_does_not_move_when_the_kernel_renumbers() -> None:
    """The whole point: same hardware, different plug order, same target."""
    baseline = by_id(enumerate_devices(root=BASELINE))
    renumbered = by_id(enumerate_devices(root=RENUMBERED))
    # The numeric node genuinely moved between these two fixture trees...
    assert baseline[C270_ID].video_nodes[0].path != renumbered[C270_ID].video_nodes[0].path
    # ...and the capture target did not.
    assert baseline[C270_ID].capture_node == renumbered[C270_ID].capture_node
    assert baseline[ARDUCAM_ID].capture_node == renumbered[ARDUCAM_ID].capture_node


def test_capture_node_falls_back_to_the_numeric_path_without_a_by_id_link() -> None:
    node = VideoNode(path="/dev/video9", by_id="", index=0)
    device = LogicalDevice(
        stable_id="x", label="x", usb_path="1-1", video_nodes=(node,), capture_node=None
    )
    assert device.video_nodes[0].by_id == ""
    # _capture_target is the single place the preference is expressed.
    assert _capture_target(node) == "/dev/video9"


def test_label_and_usb_path_come_from_usb_descriptors() -> None:
    devices = by_id(enumerate_devices(root=BASELINE))
    assert devices[C270_ID].label == "C270 HD WEBCAM"
    assert devices[C270_ID].usb_path == "3-1"
    assert devices[ARDUCAM_ID].label == "Arducam_12MP"
    assert devices[ARDUCAM_ID].usb_path == "5-1.3"


# --------------------------------------------------------------------------
# Criterion 2: pairing derives from the shared sysfs USB parent
# --------------------------------------------------------------------------


def test_c270_camera_pairs_with_its_own_microphone() -> None:
    c270 = by_id(enumerate_devices(root=BASELINE))[C270_ID]
    assert c270.audio is not None
    assert c270.audio.card_id == "WEBCAM"
    assert c270.audio.name == "C270 HD WEBCAM"
    assert c270.audio.index == 1
    assert c270.audio.alsa_address == "hw:CARD=WEBCAM,DEV=0"


def test_arducam_camera_pairs_with_its_own_microphone() -> None:
    arducam = by_id(enumerate_devices(root=BASELINE))[ARDUCAM_ID]
    assert arducam.audio is not None
    assert arducam.audio.card_id == "Arducam12MP"
    assert arducam.audio.index == 3
    assert arducam.audio.alsa_address == "hw:CARD=Arducam12MP,DEV=0"


def test_each_microphone_is_claimed_by_exactly_one_camera() -> None:
    devices = enumerate_devices(root=BASELINE)
    addresses = [d.audio.alsa_address for d in devices if d.audio is not None]
    assert len(addresses) == len(set(addresses))


def test_pairing_matches_the_usb_parent_of_both_sides() -> None:
    """The mic's sysfs USB parent must be the camera's own, not a neighbour's."""
    devices = by_id(enumerate_devices(root=BASELINE))
    # C270 video lives on 3-1; its ALSA card 1 lives on 3-1:1.2 -> parent 3-1.
    assert devices[C270_ID].usb_path == "3-1"
    assert devices[C270_ID].audio is not None and devices[C270_ID].audio.index == 1
    # The Arducam sits on a different port entirely and keeps its own card.
    assert devices[ARDUCAM_ID].usb_path == "5-1.3"
    assert devices[ARDUCAM_ID].audio is not None and devices[ARDUCAM_ID].audio.index == 3


def test_microphone_without_a_camera_still_enumerates() -> None:
    reachy = by_id(enumerate_devices(root=BASELINE))[REACHY_ID]
    assert reachy.video_nodes == ()
    assert reachy.capture_node is None
    assert reachy.label == "Reachy Mini Audio"
    assert reachy.usb_path == "5-1.1"
    assert reachy.audio is not None
    assert reachy.audio.alsa_address == "hw:CARD=Audio,DEV=0"


def test_non_usb_playback_only_card_is_not_a_capture_device() -> None:
    """The NVIDIA HDA card has no capture PCM and no USB parent."""
    devices = enumerate_devices(root=BASELINE)
    assert all("NVIDIA" not in device.stable_id for device in devices)
    assert all(device.audio is None or device.audio.index != 0 for device in devices)


def test_camera_with_no_sound_subsystem_pairs_to_nothing() -> None:
    devices = enumerate_devices(root=CAMERA_ONLY)
    assert len(devices) == 1
    assert devices[0].stable_id == C270_ID
    assert devices[0].audio is None
    assert devices[0].capture_node == f"/dev/v4l/by-id/{C270_ID}-video-index0"
    assert len(devices[0].video_nodes) == 2


# --------------------------------------------------------------------------
# Degraded trees: never raise, and never make a present device disappear
# --------------------------------------------------------------------------


def test_camera_with_no_sysfs_link_is_still_listed() -> None:
    """Reporting no camera when one is plainly attached is the failure to avoid."""
    device = by_id(enumerate_devices(root=DEGRADED))[C270_ID]
    assert device.capture_node == f"/dev/v4l/by-id/{C270_ID}-video-index0"
    assert device.usb_path == ""  # topology unknown, so honestly reported as unknown
    assert device.audio is None  # ...and with no topology there is nothing to pair
    assert device.label == C270_ID  # no descriptors to build a nicer label from


def test_unreadable_by_id_entries_are_skipped_without_raising() -> None:
    devices = enumerate_devices(root=DEGRADED)
    listed = {device.stable_id for device in devices}
    assert "usb-Broken_Entry_0001" not in listed  # not a symlink
    assert all(not device.stable_id.endswith("-index0") for device in devices)  # not a node


def test_usb_microphone_with_no_descriptors_falls_back_to_a_topology_id() -> None:
    devices = by_id(enumerate_devices(root=DEGRADED))
    assert "usb-unknown-9-1" in devices
    mystery = devices["usb-unknown-9-1"]
    assert mystery.label == "Mystery Mic"
    assert mystery.audio is not None
    assert mystery.audio.alsa_address == "hw:CARD=Mystery,DEV=0"


def test_non_usb_capture_card_is_out_of_lane_and_not_listed() -> None:
    """card0 has a capture PCM but no USB parent: no stable id, nothing to pair."""
    devices = enumerate_devices(root=DEGRADED)
    assert all(device.audio is None or device.audio.card_id != "NVIDIA" for device in devices)
    assert len(devices) == 2


# --------------------------------------------------------------------------
# Criterion 3: resolution by stable id is index-independent
# --------------------------------------------------------------------------


def test_renumbering_moves_every_index_but_no_identity() -> None:
    baseline = by_id(enumerate_devices(root=BASELINE))
    renumbered = by_id(enumerate_devices(root=RENUMBERED))
    assert set(baseline) == set(renumbered)
    # ...and the indices really did move, or this test would prove nothing.
    # Read off video_nodes[].path, not capture_node: capture_node is the by-id
    # handle now and is stable across renumbering *by design*, so asserting on
    # it here would silently stop proving anything.
    assert baseline[C270_ID].video_nodes[0].path == "/dev/video0"
    assert renumbered[C270_ID].video_nodes[0].path == "/dev/video2"
    assert baseline[ARDUCAM_ID].video_nodes[0].path == "/dev/video2"
    assert renumbered[ARDUCAM_ID].video_nodes[0].path == "/dev/video0"


def test_resolution_by_stable_id_survives_renumbering() -> None:
    for selector in (C270_ID, "C270", ARDUCAM_ID, "Arducam_12MP", REACHY_ID, "Reachy"):
        before = resolve(selector, root=BASELINE)
        after = resolve(selector, root=RENUMBERED)
        assert before.stable_id == after.stable_id, selector


def test_alsa_card_number_moves_but_the_alsa_address_does_not() -> None:
    before = resolve("C270", root=BASELINE).audio
    after = resolve("C270", root=RENUMBERED).audio
    assert before is not None and after is not None
    assert before.index == 1 and after.index == 3
    assert before.alsa_address == after.alsa_address == "hw:CARD=WEBCAM,DEV=0"


def test_pairing_is_recomputed_from_topology_not_remembered_by_index() -> None:
    """After the swap ALSA card 1 belongs to the Arducam, not the C270."""
    renumbered = by_id(enumerate_devices(root=RENUMBERED))
    assert renumbered[ARDUCAM_ID].audio is not None
    assert renumbered[ARDUCAM_ID].audio.index == 1
    assert renumbered[ARDUCAM_ID].audio.card_id == "Arducam12MP"
    assert renumbered[C270_ID].audio is not None
    assert renumbered[C270_ID].audio.index == 3
    assert renumbered[C270_ID].audio.card_id == "WEBCAM"


def test_usb_port_change_does_not_change_the_stable_id() -> None:
    before = resolve("C270", root=BASELINE)
    after = resolve("C270", root=RENUMBERED)
    assert before.usb_path == "3-1"
    assert after.usb_path == "5-1.3"
    assert before.stable_id == after.stable_id


# --------------------------------------------------------------------------
# resolve() selector forms and the error contract
# --------------------------------------------------------------------------


def test_resolve_accepts_a_full_stable_id() -> None:
    assert resolve(C270_ID, root=BASELINE).stable_id == C270_ID


def test_resolve_accepts_a_unique_substring() -> None:
    assert resolve("C270", root=BASELINE).stable_id == C270_ID
    assert resolve("12MP", root=BASELINE).stable_id == ARDUCAM_ID


def test_resolve_substring_is_case_insensitive() -> None:
    assert resolve("c270", root=BASELINE).stable_id == C270_ID
    assert resolve("REACHY", root=BASELINE).stable_id == REACHY_ID


def test_resolve_matches_the_human_label_too() -> None:
    assert resolve("HD WEBCAM", root=BASELINE).stable_id == C270_ID


def test_resolve_accepts_a_by_id_path_for_either_node() -> None:
    for index in (0, 1):
        selector = f"/dev/v4l/by-id/{C270_ID}-video-index{index}"
        assert resolve(selector, root=BASELINE).stable_id == C270_ID


def test_resolve_accepts_a_bare_by_id_name() -> None:
    assert resolve(f"{C270_ID}-video-index1", root=BASELINE).stable_id == C270_ID


def test_resolve_rejects_an_unknown_selector_with_a_user_error() -> None:
    with pytest.raises(CliError) as excinfo:
        resolve("no-such-camera", root=BASELINE)
    error = excinfo.value
    assert error.code == EXIT_USER_ERROR
    assert "no-such-camera" in error.message
    assert "list" in error.remediation


def test_resolve_rejects_an_ambiguous_selector_and_names_the_candidates() -> None:
    with pytest.raises(CliError) as excinfo:
        resolve("usb-", root=BASELINE)
    error = excinfo.value
    assert error.code == EXIT_USER_ERROR
    assert C270_ID in error.message
    assert ARDUCAM_ID in error.message
    assert "list" in error.remediation


def test_resolve_rejects_an_empty_selector() -> None:
    with pytest.raises(CliError) as excinfo:
        resolve("   ", root=BASELINE)
    assert excinfo.value.code == EXIT_USER_ERROR
    assert "list" in excinfo.value.remediation


def test_resolve_rejects_a_dev_video_index_and_names_the_stable_id() -> None:
    """``/dev/video0`` is plug-order, not identity — refuse it, but helpfully."""
    with pytest.raises(CliError) as excinfo:
        resolve("/dev/video0", root=BASELINE)
    error = excinfo.value
    assert error.code == EXIT_USER_ERROR
    assert C270_ID in error.remediation
    assert "list" in error.remediation


def test_resolve_rejects_an_unknown_dev_video_index() -> None:
    with pytest.raises(CliError) as excinfo:
        resolve("/dev/video9", root=BASELINE)
    assert excinfo.value.code == EXIT_USER_ERROR
    assert "list" in excinfo.value.remediation


def test_resolve_on_an_empty_tree_still_fails_cleanly(tmp_path: Path) -> None:
    root = str(tmp_path)
    assert enumerate_devices(root=root) == ()
    with pytest.raises(CliError) as excinfo:
        resolve("C270", root=root)
    assert excinfo.value.code == EXIT_USER_ERROR


# --------------------------------------------------------------------------
# Shape of the value objects
# --------------------------------------------------------------------------


def test_value_objects_are_frozen() -> None:
    device = resolve("C270", root=BASELINE)
    with pytest.raises(FrozenInstanceError):
        device.stable_id = "nope"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        device.video_nodes[0].path = "nope"  # type: ignore[misc]


def test_video_nodes_is_a_tuple_and_devices_are_hashable() -> None:
    device = resolve("C270", root=BASELINE)
    assert isinstance(device, LogicalDevice)
    assert isinstance(device.video_nodes, tuple)
    assert isinstance(device.video_nodes[0], VideoNode)
    assert isinstance(device.audio, AudioCard)
    assert len({device, device}) == 1


def test_as_dict_round_trips_everything_a_json_verb_needs() -> None:
    payload = resolve("C270", root=BASELINE).as_dict()
    assert payload["stable_id"] == C270_ID
    assert payload["label"] == "C270 HD WEBCAM"
    assert payload["usb_path"] == "3-1"
    assert payload["capture_node"] == f"/dev/v4l/by-id/{C270_ID}-video-index0"
    assert [node["path"] for node in payload["video_nodes"]] == ["/dev/video0", "/dev/video1"]
    assert payload["audio"]["alsa_address"] == "hw:CARD=WEBCAM,DEV=0"


def test_as_dict_reports_a_missing_microphone_as_null() -> None:
    payload = enumerate_devices(root=CAMERA_ONLY)[0].as_dict()
    assert payload["audio"] is None


def test_enumerate_is_deterministic_and_sorted_by_stable_id() -> None:
    first = enumerate_devices(root=BASELINE)
    second = enumerate_devices(root=BASELINE)
    assert first == second
    assert [device.stable_id for device in first] == sorted(d.stable_id for d in first)


def test_enumerating_the_real_host_is_read_only_and_never_raises() -> None:
    """Smoke test against the live ``/`` — pure filesystem reads, opens nothing."""
    devices = enumerate_devices()
    assert isinstance(devices, tuple)
    for device in devices:
        assert device.stable_id
        assert device.video_nodes or device.audio is not None


# --------------------------------------------------------------------------
# The /proc/asound/cards line regex
#
# SonarCloud S8786 flags this pattern as super-linear. Measured, it is linear
# on every adversarial shape we could construct -- the analyzer objects to the
# overlap between adjacent quantifiers (`\s*` beside `.*`), which in practice
# cannot backtrack because `.*$` always succeeds. Rather than argue with the
# analyzer, the pattern is written with possessive quantifiers so it is
# backtrack-free *by construction* and the objection cannot recur. These tests
# pin both halves of that: the property, and the parse behaviour it must not
# change.
# --------------------------------------------------------------------------


def test_cards_line_regex_is_backtrack_free_by_construction() -> None:
    """Every quantifier in the cards-line pattern is possessive.

    A possessive quantifier never gives characters back, so the match is
    single-pass regardless of input -- there is no backtracking budget to
    exhaust and no super-linear path to find.
    """
    pattern = _CARDS_LINE_RE.pattern
    greedy = [
        f"{pattern[max(0, i - 6):i + 1]!r} (offset {i})"
        for i, char in enumerate(pattern)
        if char in "*+"
        # a quantifier is possessive when the next character is '+'; skip the
        # '+' that is itself the possessive marker, and '+' inside a class.
        and not pattern.startswith("+", i + 1)
        and not (char == "+" and pattern.startswith(("*", "+"), i - 1))
    ]
    assert greedy == [], f"non-possessive quantifier(s) in _CARDS_LINE_RE: {greedy}"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (" 0 [NVIDIA         ]: HDA-Intel - HDA NVidia", ("0", "NVIDIA", "HDA-Intel - HDA NVidia")),
        (
            " 1 [WEBCAM         ]: USB-Audio - C270 HD WEBCAM",
            ("1", "WEBCAM", "USB-Audio - C270 HD WEBCAM"),
        ),
        (
            "3 [Arducam12MP    ]: USB-Audio - Arducam_12MP",
            ("3", "Arducam12MP", "USB-Audio - Arducam_12MP"),
        ),
        ("10 [x]:y", ("10", "x", "y")),
        (
            " 2 [Audio]  :  USB-Audio - Reachy Mini Audio",
            ("2", "Audio", "USB-Audio - Reachy Mini Audio"),
        ),
        ("0 []: no id at all", ("0", "", "no id at all")),
    ],
)
def test_cards_line_regex_parses_the_shapes_alsa_emits(
    line: str, expected: tuple[str, str, str]
) -> None:
    matched = _CARDS_LINE_RE.match(line)
    assert matched is not None, line
    assert (matched.group("index"), matched.group("id"), matched.group("rest")) == expected


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "no leading index [X]: y",
        "1 no bracket at all",
        "1 [unterminated",
        "    0    :    missing brackets",
        "  Playback devices are not card lines",
    ],
)
def test_cards_line_regex_rejects_non_card_lines(line: str) -> None:
    assert _CARDS_LINE_RE.match(line) is None


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda n: " " * n + "x", id="spaces-then-nondigit"),
        pytest.param(lambda n: "1" * n, id="digits-only"),
        pytest.param(lambda n: "1 [" + "a" * n, id="open-bracket-never-closed"),
        pytest.param(lambda n: "1 [" + "a " * n, id="bracket-alternating-space"),
        pytest.param(lambda n: "1 [X" + " " * n + "]" + " " * n, id="padded-but-no-colon"),
        pytest.param(lambda n: "1 [X]:" + " " * n, id="trailing-space-run"),
    ],
)
def test_cards_line_regex_stays_linear_on_adversarial_input(make) -> None:
    """Catastrophic backtracking would blow up between these two sizes."""
    small, large = 2_000, 64_000
    for size in (small, large):
        started = time.perf_counter()
        _CARDS_LINE_RE.match(make(size))
        elapsed = time.perf_counter() - started
        # Generous by ~3 orders of magnitude against measured (<0.001s at 64k);
        # a quadratic path at 64k takes minutes, so this cannot pass by luck.
        assert elapsed < 0.5, f"{size} chars took {elapsed:.3f}s"
