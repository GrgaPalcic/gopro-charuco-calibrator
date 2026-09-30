"""Shared fixtures: a synthetic 4:3 clip of the board, written with ffmpeg.

The clip holds the board still at a set of positions (sharp), blurs some of those holds
on purpose, moves fast between positions, and starts with no board in view. Every frame
is labelled, so a test can say which kind of frame each kept view came from.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytest

from gopro_charuco_calibrator.coverage import pose_distance
from gopro_charuco_calibrator.detection import detect_markers
from gopro_charuco_calibrator.models import BoardConfig
from gopro_charuco_calibrator.synthetic import recording_camera

CLIP_SIZE = (1600, 1200)
CLIP_FPS = 24
HOLD_FRAMES = 24  # 1 s held still
MOVE_FRAMES = 6  # 0.25 s fast move to the next position
# Blurred enough to halve the sharpness score, not so much the board is lost.
BLUR_SIGMA = 1.3

# (u, v as a fraction of the frame, distance m, tilt rotation vector, how the hold looks)
# "sharp": all sharp; "half": sharp then blurred; "blurred": blurred throughout.
POSES = [
    (0.50, 0.50, 0.45, (0.0, 0.0, 0.0), "sharp"),
    (0.25, 0.30, 0.60, (0.0, 0.0, 0.0), "half"),
    (0.50, 0.28, 0.60, (0.0, 0.0, 0.0), "sharp"),
    (0.75, 0.30, 0.60, (0.0, 0.0, 0.2), "sharp"),
    (0.76, 0.50, 0.60, (0.0, 0.0, 0.0), "sharp"),
    (0.75, 0.70, 0.60, (0.0, 0.0, 0.0), "sharp"),
    (0.50, 0.72, 0.60, (0.0, 0.0, -0.2), "half"),
    (0.25, 0.70, 0.60, (0.0, 0.0, 0.0), "sharp"),
    (0.24, 0.50, 0.60, (0.0, 0.0, 0.0), "sharp"),
    (0.40, 0.40, 0.30, (0.0, 0.0, 0.0), "half"),
    (0.60, 0.40, 0.30, (0.0, 0.0, 0.0), "blurred"),
    (0.60, 0.60, 0.30, (0.0, 0.0, 0.0), "half"),
    (0.35, 0.35, 0.40, (0.6, 0.0, 0.0), "sharp"),
    (0.65, 0.35, 0.40, (0.0, 0.6, 0.0), "sharp"),
    (0.65, 0.65, 0.40, (-0.6, 0.0, 0.0), "sharp"),
    (0.35, 0.65, 0.40, (0.0, -0.6, 0.0), "sharp"),
]


@dataclass
class SyntheticClip:
    path: Path
    size: tuple[int, int]
    fps: int
    labels: list[tuple[str, int]]  # per frame: (kind, pose index); kind sharp/blur/move/empty
    board: BoardConfig

    def label_at(self, time_s: float) -> tuple[str, int]:
        index = min(int(round(time_s * self.fps)), len(self.labels) - 1)
        return self.labels[index]


def _encoder_args() -> list[str]:
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    if "libx264" in encoders:
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-pix_fmt", "yuv420p"]
    return ["-c:v", "mpeg4", "-q:v", "2", "-pix_fmt", "yuv420p"]


def write_clip(path: Path, frames, size, fps) -> None:
    command = [
        "ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
        "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "-",
        *_encoder_args(), str(path),
    ]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE)
    for frame in frames:
        proc.stdin.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())
    proc.stdin.close()
    assert proc.wait(timeout=120) == 0


def build_clip(
    path: Path,
    poses=POSES,
    size=CLIP_SIZE,
    hold_frames: int = HOLD_FRAMES,
    move_frames: int = MOVE_FRAMES,
) -> SyntheticClip:
    board = BoardConfig()
    camera = recording_camera(board, size)
    width, height = size
    labels: list[tuple[str, int]] = []

    def pose(entry):
        u, v, distance, tilt, _kind = entry
        return camera.pose_towards(u * width, v * height, distance, tilt)

    def frames():
        empty = camera.room.copy()
        empty[~camera.inside] = 0
        for _ in range(CLIP_FPS // 2):
            labels.append(("empty", -1))
            yield empty
        for index, entry in enumerate(poses):
            kind = entry[4]
            sharp = camera.render_gray(*pose(entry))
            blurred = cv2.GaussianBlur(sharp, (0, 0), BLUR_SIGMA)
            for frame_no in range(hold_frames):
                is_blur = kind == "blurred" or (kind == "half" and frame_no >= hold_frames // 2)
                labels.append(("blur" if is_blur else "sharp", index))
                yield blurred if is_blur else sharp
            if index + 1 < len(poses):
                a, b = np.asarray(entry[:3], float), np.asarray(poses[index + 1][:3], float)
                ta, tb = np.asarray(entry[3], float), np.asarray(poses[index + 1][3], float)
                for step in range(1, move_frames + 1):
                    w = step / (move_frames + 1)
                    u, v, d = a + w * (b - a)
                    rotation, tvec = camera.pose_towards(
                        u * width, v * height, d, ta + w * (tb - ta)
                    )
                    labels.append(("move", index))
                    yield camera.render_gray(rotation, tvec)

    write_clip(path, frames(), size, CLIP_FPS)
    return SyntheticClip(path=path, size=size, fps=CLIP_FPS, labels=labels, board=board)


def varied_poses(count: int = 30, seed: int = 7, size=CLIP_SIZE):
    """Board positions spread over the frame, near and far, tilted every way: enough
    for OpenICC's view initialisation (15 flat-on views made it crash, 2026-09-30)."""
    board = BoardConfig()
    camera = recording_camera(board, size)
    width, height = size
    rng = np.random.default_rng(seed)
    poses, seen = [], []
    while len(poses) < count:
        u, v = rng.uniform(0.18, 0.82, 2)
        distance = rng.uniform(0.28, 0.6)
        tilt = tuple(rng.uniform([-0.6, -0.6, -0.4], [0.6, 0.6, 0.4]))
        gray = camera.render_gray(*camera.pose_towards(u * width, v * height, distance, tilt))
        full = detect_markers(gray, size, board)
        half = cv2.resize(gray, (width // 2, height // 2), interpolation=cv2.INTER_AREA)
        quick = detect_markers(half, (width // 2, height // 2), board)
        if full is None or full.marker_count < 16 or quick is None or quick.marker_count < 10:
            continue
        if any(pose_distance(full.pose, other) < 0.15 for other in seen):
            continue
        seen.append(full.pose)
        poses.append((float(u), float(v), float(distance), tilt, "sharp"))
    return poses


def ffmpeg_missing() -> bool:
    return shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None


needs_ffmpeg = pytest.mark.skipif(ffmpeg_missing(), reason="needs ffmpeg and ffprobe")


@pytest.fixture(scope="session")
def synthetic_clip(tmp_path_factory) -> SyntheticClip:
    if ffmpeg_missing():
        pytest.skip("needs ffmpeg and ffprobe")
    return build_clip(tmp_path_factory.mktemp("clip") / "GX010001.MP4")


@pytest.fixture(scope="session")
def varied_clip(tmp_path_factory) -> SyntheticClip:
    """30 tilted positions held for 0.5 s each (for the OpenICC solve)."""
    if ffmpeg_missing():
        pytest.skip("needs ffmpeg and ffprobe")
    path = tmp_path_factory.mktemp("varied") / "GX010002.MP4"
    return build_clip(path, poses=varied_poses(), hold_frames=12, move_frames=3)
