"""``webcam list`` — logical capture devices, their nodes, and access status.

Composes the two wave-1 modules that already own this domain rather than
re-deriving anything: :mod:`webcam_cli.devices` for *what is attached and how
it pairs* (identity is keyed on ``/dev/v4l/by-id``, never on the plug-order
``/dev/videoN`` index — see that module's docstring for why), and
:mod:`webcam_cli.access` for *can this node be opened right now*. This module
adds nothing to either: it enumerates, probes access on the one video node and
the one audio node each logical device actually offers, and renders.

Access is reported **per subsystem, not per device** — video and audio fail
independently on this project's reference host (a headless agent keeps its
microphones' ``audio``-group access while losing every camera's seat ACL), so
folding them into one status would hide exactly the failure this tool exists
to surface. See ``CLAUDE.md``, "Domain constraints".

``list`` never fails because one device is unhappy. :func:`~webcam_cli.access.
check_access` *reports* rather than raises, and this module never calls
:func:`webcam_cli.access.access_error` — a forbidden, absent, or busy device
is shown as such, with its remediation carried through, while ``list`` itself
still exits 0. Capture verbs that need to fail loudly on a bad device are a
different, later concern (open questions 3 and 4 in issue #1).

No hardware is activated beyond the single non-blocking permission probe
:func:`~webcam_cli.access.check_access` already performs per node (an
``O_NONBLOCK`` ``open()``/``close()`` pair, not a capture). This module never
streams, records, calls the capture engine, or probes formats.

``capture_node`` is reported by :mod:`webcam_cli.devices` as a documented
*heuristic* — this kernel exposes no ``device_caps`` attribute in sysfs, so
"the node that actually yields frames" cannot be known without opening it and
reading a frame, which is out of lane here. The JSON payload flags this
explicitly (``capture_node_is_heuristic``) and the text rendering says so in
words, so neither mode presents a guess as a fact.
"""

from __future__ import annotations

import argparse
import re

from webcam_cli.access import AccessReport, check_access
from webcam_cli.cli._output import emit_result
from webcam_cli.devices import AudioCard, LogicalDevice, enumerate_devices

# "hw:CARD=WEBCAM,DEV=0" -> capture PCM device number, for the /dev/snd node path.
_ALSA_DEV_RE = re.compile(r"DEV=(?P<dev>\d+)")


def _audio_node_path(card: AudioCard) -> str:
    """The ALSA capture PCM device node for ``card``, e.g. ``/dev/snd/pcmC1D0c``.

    :class:`~webcam_cli.devices.AudioCard` carries ``alsa_address`` (the
    ``hw:CARD=...,DEV=n`` handle that is the one worth persisting — see that
    module's docstring) but not the raw device-node path
    :func:`~webcam_cli.access.check_access` needs to actually probe
    openability. That path is reconstructed here, from the card's current
    ``index`` and the capture device number embedded in ``alsa_address``.
    """
    matched = _ALSA_DEV_RE.search(card.alsa_address)
    device = matched.group("dev") if matched else "0"
    return f"/dev/snd/pcmC{card.index}D{device}c"


def _access_payload(report: AccessReport) -> dict[str, object]:
    return {
        "state": report.state.value,
        "path": report.path,
        "remediation": report.remediation,
    }


def _device_payload(device: LogicalDevice) -> dict[str, object]:
    """``device.as_dict()`` plus one access probe per subsystem it actually has."""
    payload = device.as_dict()

    if device.capture_node is not None:
        payload["video_access"] = _access_payload(check_access(device.capture_node, "video"))
        payload["capture_node_is_heuristic"] = True
    else:
        payload["video_access"] = None
        payload["capture_node_is_heuristic"] = None

    if device.audio is not None:
        payload["audio_access"] = _access_payload(
            check_access(_audio_node_path(device.audio), "audio")
        )
    else:
        payload["audio_access"] = None

    return payload


def build_report(root: str) -> dict[str, object]:
    """Enumerate every logical capture device under ``root`` with access status.

    Pure composition of :func:`webcam_cli.devices.enumerate_devices` (identity)
    and :func:`webcam_cli.access.check_access` (openability — one probe per
    subsystem a device actually has). Never raises: a device whose node is
    absent, forbidden, or busy is still listed, with that state and its
    remediation attached, so this always returns a complete report — including
    the empty one when no device is attached.
    """
    devices = enumerate_devices(root=root)
    return {
        "devices": [_device_payload(device) for device in devices],
        "count": len(devices),
    }


def _render_video_block(payload: dict[str, object]) -> list[str]:
    nodes = payload["video_nodes"]
    if not nodes:
        return ["  video: none"]
    node_paths = ", ".join(str(node["path"]) for node in nodes)
    access = payload["video_access"]
    lines = [
        f"  video nodes: {node_paths}",
        f"  capture node (heuristic, not a guarantee): {payload['capture_node']}",
        f"  video access: {access['state']}",
    ]
    if access["remediation"]:
        lines.append(f"    hint: {access['remediation']}")
    return lines


def _render_audio_block(payload: dict[str, object]) -> list[str]:
    audio = payload["audio"]
    if audio is None:
        return ["  audio: none"]
    access = payload["audio_access"]
    lines = [
        f"  audio: {audio['alsa_address']} ({audio['name']})",
        f"  audio access: {access['state']}",
    ]
    if access["remediation"]:
        lines.append(f"    hint: {access['remediation']}")
    return lines


def _render_text(report: dict[str, object]) -> str:
    devices = report["devices"]
    if not devices:
        return "no capture devices found"

    blocks: list[str] = [f"{report['count']} capture device(s)"]
    for payload in devices:
        blocks.append("")
        blocks.append(f"{payload['stable_id']} ({payload['label']})")
        blocks.append(f"  usb: {payload['usb_path']}")
        blocks.extend(_render_video_block(payload))
        blocks.extend(_render_audio_block(payload))
    return "\n".join(blocks)


def cmd_list(args: argparse.Namespace) -> int:
    root = getattr(args, "root", None) or "/"
    report = build_report(root)
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(report, json_mode=True)
    else:
        emit_result(_render_text(report), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "list",
        help="List logical capture devices: id, nodes, ALSA card, paired mic, access status.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.add_argument(
        "--root",
        default="/",
        metavar="PATH",
        help=(
            "Filesystem root to enumerate devices under (default: /); "
            "mainly for pointing at a synthetic device tree in tests."
        ),
    )
    p.set_defaults(func=cmd_list)
