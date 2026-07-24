"""Device identity core — *what capture devices exist, and how they pair*.

This module answers exactly one question and stops: which logical capture
devices are attached, and for each one, which video nodes and which microphone
belong to it. It never opens a device, never checks permissions, and never
probes a pixel format — those are separate concerns owned elsewhere. Everything
here is filesystem parsing, so it is safe to call from any context, including a
dry run.

Four facts about Linux capture hardware shape this design. Each was verified on
the operator's host and each is encoded in ``tests/fixtures``:

1. **A UVC camera usually exposes more than one ``/dev/video*`` node.** Both
   cameras on the reference host publish two nodes; only the lower-indexed one
   yields frames. Listing ``/dev/video*`` and calling it four cameras is wrong,
   so nodes are collapsed into one :class:`LogicalDevice` per physical device.

2. **``/dev/videoN`` and ALSA card numbers are plug-order, not identity.** They
   have already swapped once on the reference host. Nothing here is keyed on an
   index: the identity is the ``/dev/v4l/by-id`` handle, which udev builds from
   the USB vendor/product/serial descriptors. :attr:`AudioCard.index` is
   reported because callers see it in ``arecord -l``, but it is explicitly
   ephemeral — :attr:`AudioCard.alsa_address` is the handle to persist.

3. **Video and audio identifiers are unrelated.** A camera is a ``/dev/video*``
   node, its microphone is an ALSA card, and no numbering connects them. The
   only link is USB topology: both hang off the same sysfs USB device
   directory (``3-1``), on different interfaces (``3-1:1.0`` for video,
   ``3-1:1.2`` for audio). Pairing walks up from each side to that shared
   parent and matches there.

4. **A/V sets are not 1:1.** ``Reachy Mini Audio`` is a capture device with no
   camera; a webcam may have no microphone. Both enumerate correctly — a
   mic-only device is a :class:`LogicalDevice` with empty ``video_nodes``, a
   camera with no mic has ``audio is None``.

Scope limits worth knowing before you build on this:

* **USB only.** A capture card with no USB parent in sysfs (an analog HDA
  line-in, a PCIe capture card) is skipped: it has no USB descriptors to derive
  a stable identity from, and it cannot be paired by topology. That matches
  this tool's lane, but it means "not listed" is not the same as "not present".
* **``capture_node`` is a heuristic.** This kernel exposes no ``device_caps``
  attribute in sysfs, so the node that actually yields frames cannot be known
  without opening the device. The lowest ``-video-indexN`` is used, which is
  correct for every UVC device tested. A caller that opens the device and finds
  otherwise knows better than this module does.

All reads are taken relative to ``root``, which exists so tests can point at a
synthetic tree instead of the host. Device paths in the returned values
(``/dev/video0``, ``/dev/v4l/by-id/...``) are always reported as the kernel
names them and are never prefixed with ``root``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from webcam_cli.cli._errors import EXIT_USER_ERROR, CliError

_BY_ID_DIR = "dev/v4l/by-id"
_SYS_DIR = "sys"
_V4L_CLASS_DIR = "sys/class/video4linux"
_SOUND_CLASS_DIR = "sys/class/sound"
_ASOUND_DIR = "proc/asound"

_LIST_HINT = "run `webcam list --json` to see the stable id of every attached device"

# "usb-046d_C270_HD_WEBCAM_200901010001-video-index0" -> stable id + node index.
_BY_ID_NAME_RE = re.compile(r"^(?P<stable>.+)-video-index(?P<index>\d+)$")
# A sysfs USB *device* directory: "3-1", "5-1.3". Interfaces ("3-1:1.0"),
# root hubs ("usb3") and platform nodes ("NVDA8000:01") deliberately do not match.
_USB_DEVICE_RE = re.compile(r"^\d+-\d+(?:\.\d+)*$")
# " 1 [WEBCAM         ]: USB-Audio - C270 HD WEBCAM"
_CARDS_LINE_RE = re.compile(r"^\s*(?P<index>\d+)\s*\[(?P<id>[^\]]*?)\s*\]\s*:\s*(?P<rest>.*)$")
# A capture PCM directory under /proc/asound/cardN: "pcm0c" (playback is "pcm0p").
_CAPTURE_PCM_RE = re.compile(r"^pcm(?P<device>\d+)c$")
# udev's device-name allowlist; everything else becomes "_".
_UDEV_UNSAFE_RE = re.compile(r"[^A-Za-z0-9#+\-.:=@_]")
# A raw device-node selector, which this module refuses on purpose.
_NODE_SELECTOR_RE = re.compile(r"^(?:/dev/)?(?P<node>video\d+)$")

_USB_ATTRS = ("idVendor", "idProduct", "serial", "manufacturer", "product")


@dataclass(frozen=True)
class VideoNode:
    """One ``/dev/video*`` node belonging to a logical device."""

    path: str
    by_id: str
    index: int

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "by_id": self.by_id, "index": self.index}


@dataclass(frozen=True)
class AudioCard:
    """An ALSA capture card.

    ``index`` is the current card number. It is **ephemeral** — it changes when
    devices are replugged and must never be persisted as identity. Persist
    ``alsa_address``, which is keyed on the card's name and survives
    renumbering.
    """

    index: int
    card_id: str
    name: str
    alsa_address: str

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "card_id": self.card_id,
            "name": self.name,
            "alsa_address": self.alsa_address,
        }


@dataclass(frozen=True)
class LogicalDevice:
    """One physical capture device, however many kernel nodes it publishes."""

    stable_id: str
    label: str
    usb_path: str
    video_nodes: tuple[VideoNode, ...] = ()
    capture_node: str | None = None
    audio: AudioCard | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "stable_id": self.stable_id,
            "label": self.label,
            "usb_path": self.usb_path,
            "video_nodes": [node.as_dict() for node in self.video_nodes],
            "capture_node": self.capture_node,
            "audio": self.audio.as_dict() if self.audio is not None else None,
        }


# ---------------------------------------------------------------------------
# filesystem helpers (all root-relative, all failure-tolerant)
# ---------------------------------------------------------------------------


def _under(root: str, *parts: str) -> str:
    return os.path.join(root or "/", *parts)


def _listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _read_attr(path: str) -> str | None:
    text = _read_text(path)
    return text.strip() if text is not None else None


def _follow(link: str) -> str | None:
    """Resolve one sysfs symlink *lexically*, relative to its own directory.

    Lexical rather than ``realpath`` so a fixture tree resolves inside itself
    instead of escaping to the host's real ``/sys``.
    """
    try:
        target = os.readlink(link)
    except OSError:
        return None
    if os.path.isabs(target):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(os.path.dirname(link), target))


def _usb_device_dir(sysfs_path: str, sys_root: str) -> str | None:
    """Walk up a resolved sysfs path to the nearest USB *device* directory.

    ``.../usb3/3-1/3-1:1.0/video4linux/video0`` and
    ``.../usb3/3-1/3-1:1.2/sound/card1`` both land on ``.../usb3/3-1`` — which
    is precisely why a camera and its microphone can be matched to each other.
    Only components below ``sys_root`` are considered, so a fixture living at a
    path that happens to look like a USB address cannot confuse the walk.
    """
    relative = os.path.relpath(sysfs_path, sys_root)
    if relative.startswith(os.pardir):
        return None
    parts = relative.split(os.sep)
    for cut in range(len(parts), 0, -1):
        if _USB_DEVICE_RE.match(parts[cut - 1]):
            return os.path.join(sys_root, *parts[:cut])
    return None


def _usb_attrs(usb_dir: str | None) -> dict[str, str]:
    if usb_dir is None:
        return {}
    attrs: dict[str, str] = {}
    for key in _USB_ATTRS:
        value = _read_attr(os.path.join(usb_dir, key))
        if value:
            attrs[key] = value
    return attrs


def _udev_safe(text: str) -> str:
    return _UDEV_UNSAFE_RE.sub("_", text)


def _synthesise_stable_id(attrs: dict[str, str]) -> str | None:
    """Rebuild udev's ``usb-<vendor>_<model>_<serial>`` id from USB descriptors.

    Devices with a video node get their id straight from ``/dev/v4l/by-id``.
    A microphone with no camera has no such link, so the same id is rebuilt
    from sysfs using udev's own rules (vendor string, else vendor id; product
    string, else product id; serial; unsafe characters replaced by ``_``).
    Verified byte-identical to ``/dev/snd/by-id`` for all three USB audio
    devices on the reference host.
    """
    vendor = attrs.get("manufacturer") or attrs.get("idVendor")
    model = attrs.get("product") or attrs.get("idProduct")
    parts = [part for part in (vendor, model, attrs.get("serial")) if part]
    if not parts:
        return None
    return "usb-" + "_".join(_udev_safe(part) for part in parts)


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawNode:
    node: VideoNode
    stable_id: str
    usb_dir: str | None
    usb_path: str
    v4l_name: str


@dataclass(frozen=True)
class _RawCard:
    card: AudioCard
    usb_dir: str
    usb_path: str
    claimed: list[bool] = field(default_factory=lambda: [False])


def _scan_video_nodes(root: str) -> list[_RawNode]:
    by_id_dir = _under(root, _BY_ID_DIR)
    sys_root = _under(root, _SYS_DIR)
    class_dir = _under(root, _V4L_CLASS_DIR)

    raw_nodes: list[_RawNode] = []
    for name in _listdir(by_id_dir):
        matched = _BY_ID_NAME_RE.match(name)
        if matched is None:
            continue
        target = _follow(os.path.join(by_id_dir, name))
        if target is None:
            continue
        node_name = os.path.basename(target)

        device_dir = _follow(os.path.join(class_dir, node_name))
        usb_dir = _usb_device_dir(device_dir, sys_root) if device_dir else None
        v4l_name = _read_attr(os.path.join(device_dir, "name")) if device_dir else None

        raw_nodes.append(
            _RawNode(
                node=VideoNode(
                    path=f"/dev/{node_name}",
                    by_id=f"/{_BY_ID_DIR}/{name}",
                    index=int(matched.group("index")),
                ),
                stable_id=matched.group("stable"),
                usb_dir=usb_dir,
                usb_path=os.path.basename(usb_dir) if usb_dir else "",
                v4l_name=v4l_name or "",
            )
        )
    return raw_nodes


def _capture_pcm_device(root: str, index: int) -> int | None:
    """Lowest capture PCM device number of an ALSA card, or ``None`` if it has none.

    A USB card with only ``pcmNp`` entries is a speaker, not a microphone, and
    has no business in a capture-device listing.
    """
    devices = [
        int(matched.group("device"))
        for matched in (
            _CAPTURE_PCM_RE.match(entry)
            for entry in _listdir(_under(root, _ASOUND_DIR, f"card{index}"))
        )
        if matched is not None
    ]
    return min(devices) if devices else None


def _scan_audio_cards(root: str) -> list[_RawCard]:
    text = _read_text(_under(root, _ASOUND_DIR, "cards"))
    if text is None:
        return []
    sys_root = _under(root, _SYS_DIR)
    class_dir = _under(root, _SOUND_CLASS_DIR)

    cards: list[_RawCard] = []
    for line in text.splitlines():
        matched = _CARDS_LINE_RE.match(line)
        if matched is None:
            continue
        index = int(matched.group("index"))
        card_id = matched.group("id")

        pcm_device = _capture_pcm_device(root, index)
        if pcm_device is None:
            continue  # playback-only card

        card_dir = _follow(os.path.join(class_dir, f"card{index}"))
        usb_dir = _usb_device_dir(card_dir, sys_root) if card_dir else None
        if usb_dir is None:
            continue  # not a USB device: no stable identity, nothing to pair with

        rest = matched.group("rest")
        _, separator, tail = rest.partition(" - ")
        cards.append(
            _RawCard(
                card=AudioCard(
                    index=index,
                    card_id=card_id,
                    name=(tail if separator else rest).strip(),
                    alsa_address=f"hw:CARD={card_id},DEV={pcm_device}",
                ),
                usb_dir=usb_dir,
                usb_path=os.path.basename(usb_dir),
            )
        )
    cards.sort(key=lambda raw: raw.card.index)
    return cards


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def _claim_microphone(cards: list[_RawCard], usb_dir: str | None) -> AudioCard | None:
    """Take the first unclaimed ALSA capture card sharing this USB parent."""
    if not usb_dir:
        return None
    for raw in cards:
        if not raw.claimed[0] and raw.usb_dir == usb_dir:
            raw.claimed[0] = True
            return raw.card
    return None


def _camera(group: list[_RawNode], cards: list[_RawCard]) -> LogicalDevice:
    nodes = sorted(group, key=lambda raw: (raw.node.index, raw.node.path))
    usb_dir = next((raw.usb_dir for raw in nodes if raw.usb_dir), None)
    attrs = _usb_attrs(usb_dir)
    label = attrs.get("product") or nodes[0].v4l_name or nodes[0].stable_id
    return LogicalDevice(
        stable_id=nodes[0].stable_id,
        label=label,
        usb_path=nodes[0].usb_path,
        video_nodes=tuple(raw.node for raw in nodes),
        # No sysfs attribute reports which node yields frames; the lowest
        # -video-indexN is the convention and holds for every UVC device tested.
        capture_node=nodes[0].node.path,
        audio=_claim_microphone(cards, usb_dir),
    )


def _microphone_only(raw: _RawCard) -> LogicalDevice:
    attrs = _usb_attrs(raw.usb_dir)
    stable_id = _synthesise_stable_id(attrs) or f"usb-unknown-{raw.usb_path}"
    return LogicalDevice(
        stable_id=stable_id,
        label=attrs.get("product") or raw.card.name or stable_id,
        usb_path=raw.usb_path,
        video_nodes=(),
        capture_node=None,
        audio=raw.card,
    )


def enumerate_devices(root: str = "/") -> tuple[LogicalDevice, ...]:
    """Return every logical USB capture device attached under ``root``.

    Multi-node UVC cameras are collapsed into one entry, each camera is paired
    with the microphone sharing its sysfs USB parent, and microphones with no
    camera are returned as devices with no video nodes. The result is sorted by
    ``stable_id`` and is a pure function of the filesystem: no device is opened.
    """
    raw_nodes = _scan_video_nodes(root)
    cards = _scan_audio_cards(root)

    # Group on (stable id, USB address) rather than the stable id alone: two
    # devices whose descriptors are identical are still two devices.
    groups: dict[tuple[str, str], list[_RawNode]] = {}
    for raw in raw_nodes:
        groups.setdefault((raw.stable_id, raw.usb_path), []).append(raw)

    devices = [_camera(group, cards) for _, group in sorted(groups.items())]
    devices.extend(_microphone_only(raw) for raw in cards if not raw.claimed[0])
    devices.sort(key=lambda device: (device.stable_id, device.usb_path))
    return tuple(devices)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def _user_error(message: str, remediation: str) -> CliError:
    return CliError(code=EXIT_USER_ERROR, message=message, remediation=remediation)


def _node_selector_error(selector: str, node: str, devices: tuple[LogicalDevice, ...]) -> CliError:
    """Refuse ``/dev/videoN``, but name the stable id that should be used instead."""
    owner = next(
        (
            device
            for device in devices
            for video in device.video_nodes
            if video.path == f"/dev/{node}"
        ),
        None,
    )
    if owner is None:
        remediation = f"no attached device publishes /dev/{node}; {_LIST_HINT}"
    else:
        remediation = (
            f"/dev/{node} is plug-order, not identity, and moves between boots — "
            f"select {owner.stable_id!r} instead; {_LIST_HINT}"
        )
    return _user_error(
        f"device selector {selector!r} is a kernel node index, not a stable device id",
        remediation,
    )


def _normalise(selector: str) -> str:
    """Reduce a ``/dev/v4l/by-id`` path or link name to the bare stable id."""
    candidate = selector.rsplit("/", 1)[-1] if "/" in selector else selector
    matched = _BY_ID_NAME_RE.match(candidate)
    return matched.group("stable") if matched else candidate


def _select(selector: str, devices: tuple[LogicalDevice, ...]) -> LogicalDevice:
    raw = selector.strip()
    if not raw:
        raise _user_error("empty device selector", f"pass a stable device id; {_LIST_HINT}")

    node_match = _NODE_SELECTOR_RE.match(raw)
    if node_match is not None:
        raise _node_selector_error(raw, node_match.group("node"), devices)

    candidate = _normalise(raw).casefold()
    exact = [device for device in devices if device.stable_id.casefold() == candidate]
    if len(exact) == 1:
        return exact[0]

    matches = exact or [
        device
        for device in devices
        if candidate in device.stable_id.casefold() or candidate in device.label.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise _user_error(
            f"no attached capture device matches selector {selector!r}",
            f"{_LIST_HINT}, then pass one of them or a unique substring of one",
        )
    named = ", ".join(device.stable_id for device in matches)
    raise _user_error(
        f"device selector {selector!r} is ambiguous — it matches {len(matches)} devices: {named}",
        f"pass a longer, unique substring or the full stable id; {_LIST_HINT}",
    )


def resolve(selector: str, root: str = "/") -> LogicalDevice:
    """Resolve a selector to exactly one logical device.

    ``selector`` may be a full ``stable_id``, a unique case-insensitive
    substring of one (or of a device label), a ``/dev/v4l/by-id/...`` path, or a
    bare ``by-id`` link name. A raw ``/dev/videoN`` is refused on purpose: node
    numbering is plug-order and has already changed on the reference host, so
    accepting it would hand callers a selector that silently means a different
    camera after a replug. The refusal names the stable id to use instead.

    Raises :class:`~webcam_cli.cli._errors.CliError` with
    ``code=EXIT_USER_ERROR`` when nothing matches or more than one thing does.
    """
    return _select(selector, enumerate_devices(root=root))
