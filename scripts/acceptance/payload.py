#!/usr/bin/env python3
"""Payload-only helpers for the blind-consumer acceptance check.

Every function here takes the ``webcam stream --json`` payload (or a value
read out of it) and nothing else. It never imports ``webcam_cli``, never
reads ``/dev``, ``/proc/asound`` or ``/sys``, and never learns the device's
stable id — the whole point of task t9's blind-consumer criterion is that a
process which has never inspected this host can consume the stream from the
announced JSON alone.

The one liberty taken is *instrumentation*: to count buffers, the announced
consumer command has ``identity silent=false`` spliced in front of each
``fakesink`` and ``-v`` added. That is a mechanical string substitution on
the announced command, not extra knowledge about the host — the unmodified
command is run separately, verbatim, by ``blind-consumer.sh``.

Standard library only, matching the repository's zero-runtime-dependency
posture (this is a dev script, but there is no reason for it to be looser).
"""

from __future__ import annotations

import json
import re
import shlex
import signal
import socket
import subprocess  # nosec B404 - driving gst-launch-1.0 is the point of this script
import sys
import time

# The Matroska/EBML magic every Matroska byte stream starts with. The payload
# states this literally in attach.consumer.raw_socket; this checks it.
EBML_MAGIC = bytes((0x1A, 0x45, 0xDF, 0xA3))

_FAKESINK_RE = re.compile(r"fakesink\b")
_TCPCLIENTSRC_RE = re.compile(r"tcpclientsrc\s+host=\S+\s+port=\d+")


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _dig(data: object, dotted: str) -> object:
    for key in dotted.split("."):
        if isinstance(data, list):
            data = data[int(key)]
        else:
            assert isinstance(data, dict), f"{dotted}: {key} is not addressable"
            data = data[key]
    return data


def cmd_get(argv: list[str]) -> int:
    value = _dig(_load(argv[0]), argv[1])
    print(value if not isinstance(value, (dict, list)) else json.dumps(value))
    return 0


def cmd_ebml(argv: list[str]) -> int:
    """Honour attach.consumer.raw_socket: connect, read, check the magic."""
    host, port = argv[0], int(argv[1])
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.settimeout(10)
        head = b""
        while len(head) < 4:
            chunk = sock.recv(4 - len(head))
            if not chunk:
                break
            head += chunk
    if head != EBML_MAGIC:
        print(f"first 4 bytes were {head.hex(' ')}, expected {EBML_MAGIC.hex(' ')}")
        return 1
    print(f"raw TCP socket to {host}:{port} yielded EBML magic {head.hex(' ')} as announced")
    return 0


def _instrument(command: str) -> tuple[list[str], list[str]]:
    """Splice a counting ``identity`` in front of every ``fakesink``.

    Returns the argv to run and the counter names in pipeline order. For an
    ``av`` consumer the announced chain is video-branch-then-audio-branch, so
    the names come back in that order.
    """
    names: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        name = f"c{len(names)}"
        names.append(name)
        return f"identity name={name} silent=false ! {match.group(0)}"

    instrumented = _FAKESINK_RE.sub(_replace, command)
    argv = shlex.split(instrumented)
    argv.insert(1, "-v")  # after gst-launch-1.0
    return argv, names


def _counts(output: str, names: list[str]) -> dict[str, int]:
    return {
        name: output.count(f"GstIdentity:{name}: last-message = chain") for name in names
    }


def _caps(output: str, name: str) -> str | None:
    marker = f"GstIdentity:{name}.GstPad:sink: caps = "
    for line in output.splitlines():
        index = line.find(marker)
        if index != -1:
            return line[index + len(marker) :].strip()
    return None


def _summarise(output: str, names: list[str], labels: list[str]) -> tuple[str, bool]:
    counts = _counts(output, names)
    parts = []
    good = True
    for name, label in zip(names, labels):
        caps = _caps(output, name) or "no caps observed"
        head = caps.split(",")[0].strip()
        detail = ""
        for field in ("format", "width", "height", "rate", "channels"):
            match = re.search(rf"\b{field}=\((?:int|string)\)([A-Za-z0-9_]+)", caps)
            if match:
                detail += f" {field}={match.group(1)}"
        parts.append(f"{label}: {counts[name]} buffers, {head}{detail}")
        if counts[name] <= 0:
            good = False
    return "; ".join(parts), good


def _labels(medium: str, count: int) -> list[str]:
    if medium == "av" and count == 2:
        return ["video", "audio"]
    return [medium] * count


def _run(argv: list[str], seconds: float, interrupt: bool) -> str:
    proc = subprocess.Popen(  # nosec B603 - argv built from the payload, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if interrupt:
        time.sleep(seconds)
        proc.send_signal(signal.SIGINT)
    try:
        output, _ = proc.communicate(timeout=seconds + 20)
    except subprocess.TimeoutExpired:
        proc.kill()
        output, _ = proc.communicate()
    return output or ""


def cmd_live(argv: list[str]) -> int:
    """Attach to the live stream and count decoded buffers per branch."""
    payload = _load(argv[0])
    seconds = float(argv[1])
    command = str(_dig(payload, "attach.consumer.gst_launch_str"))
    medium = str(_dig(payload, "medium"))

    gst_argv, names = _instrument(command)
    output = _run(gst_argv, seconds, interrupt=True)
    summary, good = _summarise(output, names, _labels(medium, len(names)))
    print(f"live decode over {seconds}s from {_dig(payload, 'attach.uri')}: {summary}")
    if not good:
        print("no buffers reached a branch; last 5 lines of pipeline output:")
        for line in output.strip().splitlines()[-5:]:
            print(f"  {line}")
        return 1
    return 0


def cmd_decode(argv: list[str]) -> int:
    """Decode a captured container using the announced chain, off the wire."""
    payload = _load(argv[0])
    path = argv[1]
    command = str(_dig(payload, "attach.consumer.gst_launch_str"))
    medium = str(_dig(payload, "medium"))

    offline = _TCPCLIENTSRC_RE.sub(f"filesrc location={shlex.quote(path)}", command)
    if offline == command:
        print("could not rewrite the announced source element for offline decode")
        return 1
    gst_argv, names = _instrument(offline)
    output = _run(gst_argv, 60.0, interrupt=False)
    summary, good = _summarise(output, names, _labels(medium, len(names)))
    print(summary)
    if not good:
        for line in output.strip().splitlines()[-5:]:
            print(f"  {line}")
        return 1
    return 0


_COMMANDS = {
    "get": cmd_get,
    "ebml": cmd_ebml,
    "live": cmd_live,
    "decode": cmd_decode,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in _COMMANDS:
        print(f"usage: payload.py {{{'|'.join(_COMMANDS)}}} ...", file=sys.stderr)
        return 2
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
