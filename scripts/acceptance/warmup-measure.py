#!/usr/bin/env python3
"""Measure a UVC camera's auto-exposure settle time, in frames and in seconds.

Method: open the camera with **no warm-up**, capture a burst of MJPEG frames,
decode each to GRAY8, and take the mean luma per frame. The settle point is
the first frame after which mean luma stays within
:data:`SETTLE_TOLERANCE` of its final value for the rest of the burst.

Two frame rates are captured because the answer changes shape between them:
if settle is a fixed *interval* the frame index scales with fps, and if it is
a fixed *frame count* the wall-clock time does. On the reference C270 it is
the frame count that holds (see ``docs/acceptance-a-v-streaming.md``), which
is why both ``webcam stream`` and ``webcam record`` express their warm-up
default in frames.

PRIVACY: the decoded frames are written to a temporary file under the run
directory, reduced to one number each, and **deleted before this script
returns** — including on failure. Nothing but the numbers survives. Keep the
bursts short; this switches on somebody's camera.

Standard library only.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - driving gst-launch-1.0 is the point of this script
import sys
import time

WIDTH = 640
HEIGHT = 480
FRAME_BYTES = WIDTH * HEIGHT  # GRAY8, and 640 is already stride-aligned

#: Mean luma must stay within this fraction of its final value to count as
#: settled. 2% is well outside frame-to-frame sensor noise on this camera and
#: well inside the 9-19% excursions the unsettled frames show.
SETTLE_TOLERANCE = 0.02

#: (fps, frames) bursts to capture. 30 fps is the common case; 5 fps is the
#: one that discriminates a frame-count settle from an interval settle.
BURSTS = ((30, 60), (5, 20))

#: Seconds of idle before each burst, so the sensor is genuinely cold. A
#: warm sensor keeps its converged exposure and shows almost no ramp, which
#: would understate the settle time the first consumer of a stream sees.
IDLE_S = 100


def capture(node: str, fps: int, frames: int, path: str) -> None:
    argv = [
        "gst-launch-1.0",
        "-q",
        "v4l2src",
        f"device={node}",
        f"num-buffers={frames}",
        "!",
        f"image/jpeg,width={WIDTH},height={HEIGHT},framerate={fps}/1",
        "!",
        "jpegdec",
        "!",
        "videoconvert",
        "!",
        "video/x-raw,format=GRAY8",
        "!",
        "filesink",
        f"location={path}",
    ]
    subprocess.run(argv, check=True, timeout=frames / fps + 30)  # nosec B603 - fixed argv


def luma_series(path: str) -> list[float]:
    series: list[float] = []
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(FRAME_BYTES)
            if len(chunk) < FRAME_BYTES:
                break
            series.append(sum(chunk) / FRAME_BYTES)
    return series


def settle_frame(series: list[float]) -> int | None:
    if len(series) < 5:
        return None
    final = sum(series[-5:]) / 5
    band = SETTLE_TOLERANCE * final
    for index in range(len(series)):
        if all(abs(value - final) <= band for value in series[index:]):
            return index
    return None


def run_burst(node: str, run_dir: str, fps: int, frames: int) -> None:
    path = os.path.join(run_dir, f"warmup-{fps}fps.gray")
    try:
        capture(node, fps, frames, path)
        series = luma_series(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if not series:
        print(f"  {fps:>2} fps: no frames captured")
        return
    index = settle_frame(series)
    final = sum(series[-5:]) / 5
    swing = (max(series[:5]) - final) / final * 100 if final else 0.0
    if index is None:
        print(f"  {fps:>2} fps: never settled within {SETTLE_TOLERANCE:.0%} over {len(series)} frames")
        return
    print(
        f"  {fps:>2} fps: settled at frame {index:>3} "
        f"({index / fps:.2f} s)  final luma {final:6.2f}  "
        f"opening excursion {swing:+.1f}%"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: warmup-measure.py <capture-node> <run-dir>", file=sys.stderr)
        return 2
    node, run_dir = argv
    cold = os.environ.get("WEBCAM_ACCEPTANCE_COLD", "1") != "0"
    print(f"auto-exposure settle, {WIDTH}x{HEIGHT} MJPEG, warm-up disabled, cold={cold}:")
    for fps, frames in BURSTS:
        if cold:
            # A warm sensor keeps its converged exposure and shows almost no
            # ramp, which understates what a stream's first consumer sees.
            time.sleep(IDLE_S)
        run_burst(node, run_dir, fps, frames)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
