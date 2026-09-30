"""The "From a recording" route: calibrate from a clip recorded on the camera.

The operator records a clip on the camera in the dataset's own mode, drops the mp4 into
the app, and a background job checks it, picks sharp, still, varied views, and solves.
It is independent of ``CaptureSession`` (the live USB route).

Frame selection, per sample of the clip (``RecordingConfig.sample_hz``, 12 Hz):
1. **Board gate** on a half-size copy: at least ``capture.min_markers`` markers, else
   ``no_board``. Candidates are then detected again at full resolution.
2. **Motion**: ``marker_motion`` against the previous sample, in full-resolution
   pixels. The live gate ``capture.max_motion_px`` is per live frame (~30 fps) at
   1920 px wide, so the limit here is that value x (width / 1920) x (30 / sample_hz).
   A sample with no board in the sample before cannot show it is still: ``moving``.
3. **Held positions**: consecutive still samples whose pose stays within
   ``min_param_dist / 2`` of the first one are one window (the board held in one
   place). The sharpest sample wins; the others are ``blurred`` when their sharpness
   is under ``blur_ratio`` x the winner's, else ``duplicate``.
4. **Novelty**: a winner closer than ``capture.min_param_dist`` (coverage.pose_distance)
   to a view already kept, from this clip or an earlier one, is a ``duplicate`` unless
   it is sharper than the one from this clip it matches, which it then replaces.
5. **Blur across the clip**: when the clip ends, a winner whose size-weighted sharpness
   is under ``blur_ratio`` x the reference's ``BLUR_REFERENCE_PERCENTILE`` (90th
   percentile) is ``blurred``. The reference is every held position of this clip (kept
   or not, so a retake that repeats positions still counts) plus every view the run
   already kept. A first clip needs 3 held positions before the rule applies (fewer is
   no distribution); a retake always has the run's views, so a short retake that is
   blurred throughout is still caught. Sharpness (``board_sharpness``) is the variance
   of the Laplacian inside the board's box on the half-size image, divided by the
   variance of the pixels in that box: both grow with the square of the contrast, so
   the ratio does not change when the board is darker or greyer (fisheye edge fall-off,
   shade, a retake in other light). A far board has denser edges and so scores higher,
   which multiplying by the board's apparent size (``PoseParams.size``) roughly offsets.
   A high percentile, not the median, so the rule still works when most held positions
   are blurred (a clip recorded before the fast-shutter QR code was scanned): it holds
   while about one held position in ten is sharp. Measured on 22 synthetic board
   positions, 0.28 to 0.65 m, flat and tilted (2026-09-30, relative to the 90th
   percentile of the sharp full-contrast ones): sharp holds scored 0.60 to 1.12x at full,
   70 % and 45 % contrast alike; blurred holds (Gaussian sigma 1.3) 0.23 to 0.36x. The
   tests' 16-position clip, encoded and read back through ffmpeg at each contrast, gave
   the same: sharp 0.60 to 1.12x, blurred 0.25x.
   Weighting by size^1.5 (the fitted slope) separated them less (sharp from 0.58x,
   blurred up to 0.40x). The unnormalised score put sharp holds at 70 % contrast at
   0.36 to 0.50x, so a darker clip lost its sharp views. Real HERO13 footage (softer
   fisheye edges, sensor noise, which raises the score of a low-contrast view) has not
   been measured yet.
6. **Cap**: past ``max_views`` views in the run, a view that completes a guide
   checkpoint the run is still missing is always kept (the sharpest one per
   checkpoint, so a retake can always fix a RETAKE; the run may then pass the cap by
   at most the 23 checkpoints). The rest are the most varied (farthest pose first,
   starting from the views already kept), and whatever does not fit is ``over_cap``.

Winners wait on disk (``_staging_<clip>/``) until the clip ends, so memory holds only a
couple of frames whatever the clip length. Kept views become ``frames/capture_###.jpg``
(full resolution, grey) and ``overlays/capture_###.jpg`` so ``solve_from_frames`` works
unchanged; numbering continues across clips. A clip that ffmpeg stops reading with an
error, or reads to under ``INCOMPLETE_READ_FRACTION`` of its probed length (a copy from
the card that stopped early), gets a "Whole clip read" mismatch row; what was read is
still used.

One run is one camera: a clip whose serial differs from the run's starts a new run for
its camera (``RecordingJob._switch_camera``) and leaves the previous run as it was. If
that new run cannot hold the clip (its folder cannot be made, or no views could be read
from the clip), the new run's folder is removed and the previous run is open again.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import presets
from .boards import detector_params, resolve_dictionary
from .capture import _discarded_points, _read_exact, default_runs_dir
from .clipcheck import check_clip, compare, fallback_check, ffmpeg_error_line
from .coverage import PoseParams, coverage_summary, pose_distance
from .detection import MarkerDetection, detect_markers, draw_detection, marker_motion
from .guide import default_checkpoints, guide_status, pose_matches_checkpoint
from .models import AppConfig, CameraConfig, RecordingConfig
from .openicc import orbslam3_kb8_yaml
from .solver import solve_from_frames

DROP_REASONS = ("no_board", "blurred", "moving", "duplicate", "over_cap")
# The live motion gate is per frame at about this rate and width (see module docstring).
MOTION_REFERENCE_HZ = 30.0
MOTION_REFERENCE_WIDTH = 1920.0
# The clip-level blur rule judges against this percentile of the reference scores.
BLUR_REFERENCE_PERCENTILE = 90.0
# A clip decoded to less than this share of its probed length was cut short (a copy
# from the card that stopped early): it is flagged, and what was read is still used.
INCOMPLETE_READ_FRACTION = 0.9
BUSY_STATES = ("uploading", "analysing", "solving")
BUSY_MESSAGE = "A clip is still being processed; wait for it to finish."
# A clip name's stem is kept under this many UTF-8 bytes (file names max out at 255).
MAX_NAME_STEM_BYTES = 200
MAX_NAME_SUFFIX_BYTES = 16
DEFAULT_CAMERA_NAME = CameraConfig().camera_name
NOT_USED = "The clip was not used and was removed from this run's folder."


NO_RECORDING_SETTINGS = (
    "This preset has no settings for recording on the camera. Pick a HERO13 lens-mod "
    "preset for the From a recording route."
)


class RecordingError(RuntimeError):
    pass


class MissingToolError(RecordingError):
    """ffmpeg or ffprobe is not installed: the clip is fine and is never removed."""


class RunFolderError(RecordingError):
    """The run folder could not be made (a read-only or full disk)."""


TOOLS_MISSING = (
    "ffmpeg is not installed on this computer. Install ffmpeg (it includes ffprobe), "
    "then drop the clip again."
)


def no_results() -> dict[str, Any]:
    """The status's result keys for a run that has not solved (the live route's shapes)."""
    return {
        "results": None,
        "summary_path": None,
        "acquisition_mode": None,
        "rejected_points": [],
        "needs_more_views": False,
        "orbslam3": None,
    }


def missing_tools() -> list[str]:
    """The video tools this route needs that are not on the PATH."""
    return [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]


def require_tools() -> None:
    if missing_tools():
        raise MissingToolError(TOOLS_MISSING)


def join_names(names: list[str]) -> str:
    """"A", "A and B", "A, B and C"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def empty_counts() -> dict[str, int]:
    return {"samples": 0, "kept": 0, **{reason: 0 for reason in DROP_REASONS}}


def motion_limit_px(max_motion_px: float, width: int, sample_hz: float) -> float:
    return max_motion_px * (width / MOTION_REFERENCE_WIDTH) * (MOTION_REFERENCE_HZ / sample_hz)


def board_sharpness(gray: np.ndarray, detection: MarkerDetection) -> float:
    """Variance of the Laplacian inside the board's bounding box, divided by the
    variance of the box's pixels, so the score does not depend on the contrast."""
    points = detection.all_points()
    h, w = gray.shape[:2]
    x0, y0 = np.floor(points.min(axis=0)).astype(int)
    x1, y1 = np.ceil(points.max(axis=0)).astype(int)
    crop = gray[max(y0, 0) : min(y1 + 1, h), max(x0, 0) : min(x1 + 1, w)]
    if crop.size < 16:
        return 0.0
    crop = crop.astype(np.float64)
    spread = float(crop.var())
    if spread < 1.0:  # a flat box (under one grey level): nothing to judge
        return 0.0
    return float(cv2.Laplacian(crop, cv2.CV_64F).var()) / spread


@dataclass(eq=False)
class _Sample:
    index: int
    time_s: float
    detection: MarkerDetection
    sharpness: float
    motion_px: float
    frame: np.ndarray | None = None

    @property
    def pose(self) -> PoseParams:
        return self.detection.pose

    @property
    def weighted(self) -> float:
        return self.sharpness * max(self.pose.size, 1e-3)


@dataclass(eq=False)  # compared by identity: a sample holds a numpy frame
class _Staged:
    sample: _Sample
    path: Path


@dataclass
class ExtractResult:
    counts: dict[str, int]
    kept: list[dict[str, Any]]
    poses: list[PoseParams]
    image_size: tuple[int, int]
    first_index: int
    next_index: int = 0
    motion_limit_px: float = 0.0
    # How much of the clip ffmpeg decoded (samples / sample rate), its probed length,
    # and ffmpeg's last error line when it stopped with an error.
    read_s: float = 0.0
    duration_s: float = 0.0
    decode_error: str | None = None

    @property
    def incomplete(self) -> bool:
        """ffmpeg stopped with an error, or read well short of the probed length."""
        short = self.duration_s > 0 and self.read_s < INCOMPLETE_READ_FRACTION * self.duration_s
        return self.decode_error is not None or short


@dataclass
class _Selector:
    """Streaming selection state for one clip (see the module docstring)."""

    config: AppConfig
    rec: RecordingConfig
    staging: Path
    prior_poses: list[PoseParams]
    # Size-weighted sharpness of the views the run already kept (earlier clips).
    prior_scores: list[float] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=empty_counts)
    window: list[_Sample] = field(default_factory=list)
    best: _Sample | None = None
    staged: list[_Staged] = field(default_factory=list)
    # Size-weighted sharpness of every held position's best sample in this clip, kept
    # or not: the clip's own reference for the blur rule.
    winner_scores: list[float] = field(default_factory=list)

    def drop(self, reason: str, n: int = 1) -> None:
        self.counts[reason] += n

    def add_still(self, sample: _Sample) -> None:
        half_dist = self.config.capture.min_param_dist / 2.0
        if self.window and pose_distance(sample.pose, self.window[0].pose) >= half_dist:
            self.close_window()
        self.window.append(sample)
        if self.best is None or sample.sharpness > self.best.sharpness:
            if self.best is not None:
                self.best.frame = None  # only the best frame of a window stays in memory
            self.best = sample
        else:
            sample.frame = None

    def close_window(self) -> None:
        if not self.window or self.best is None:
            self.window, self.best = [], None
            return
        best = self.best
        for sample in self.window:
            if sample is not best:
                self._lose_to(sample, best)
        self.window, self.best = [], None
        self.winner_scores.append(best.weighted)
        self._offer(best)

    def _lose_to(self, loser: _Sample, winner: _Sample) -> None:
        blurred = loser.sharpness < self.rec.blur_ratio * winner.sharpness
        self.drop("blurred" if blurred else "duplicate")

    def _offer(self, sample: _Sample) -> None:
        """Novelty against every kept view; a sharper repeat replaces this clip's one."""
        min_dist = self.config.capture.min_param_dist
        if any(pose_distance(sample.pose, pose) < min_dist for pose in self.prior_poses):
            self.drop("duplicate")
            sample.frame = None
            return
        near = [s for s in self.staged if pose_distance(sample.pose, s.sample.pose) < min_dist]
        if near:
            if all(sample.sharpness > s.sample.sharpness for s in near):
                for s in near:
                    self.staged.remove(s)
                    s.path.unlink(missing_ok=True)
                    self._lose_to(s.sample, sample)
            else:
                self._lose_to(sample, max(near, key=lambda s: s.sample.sharpness).sample)
                sample.frame = None
                return
        path = self.staging / f"sample_{sample.index:06d}.jpg"
        cv2.imwrite(str(path), sample.frame)
        sample.frame = None
        self.staged.append(_Staged(sample, path))

    def finish(self) -> list[_Staged]:
        """Clip-level blur rule and the cap; returns the views to keep, in time order."""
        self.close_window()
        staged = sorted(self.staged, key=lambda s: s.sample.index)
        reference = self.prior_scores + self.winner_scores
        if self.prior_scores or len(self.winner_scores) >= 3:
            top = float(np.percentile(reference, BLUR_REFERENCE_PERCENTILE))
            sharp = [s for s in staged if s.sample.weighted >= self.rec.blur_ratio * top]
            for s in staged:
                if s not in sharp:
                    s.path.unlink(missing_ok=True)
            self.drop("blurred", len(staged) - len(sharp))
            staged = sharp
        room = max(self.rec.max_views - len(self.prior_poses), 0)
        if len(staged) > room:
            needed = _completing_views(staged, self.prior_poses, self.config)
            rest = [s for s in staged if s not in needed]
            anchors = self.prior_poses + [s.sample.pose for s in needed]
            chosen = needed + _most_varied(rest, anchors, room - len(needed))
            for s in staged:
                if s not in chosen:
                    s.path.unlink(missing_ok=True)
            self.drop("over_cap", len(staged) - len(chosen))
            staged = sorted(chosen, key=lambda s: s.sample.index)
        return staged


def _completing_views(
    staged: list[_Staged], prior: list[PoseParams], config: AppConfig
) -> list[_Staged]:
    """The sharpest view for each guide checkpoint the run has not completed yet."""
    chosen: list[_Staged] = []
    for checkpoint in default_checkpoints(config.coverage_targets):
        poses = prior + [s.sample.pose for s in chosen]
        if any(pose_matches_checkpoint(pose, checkpoint) for pose in poses):
            continue
        matches = [
            s for s in staged
            if s not in chosen and pose_matches_checkpoint(s.sample.pose, checkpoint)
        ]
        if matches:
            chosen.append(max(matches, key=lambda s: s.sample.sharpness))
    return chosen


def _most_varied(staged: list[_Staged], prior: list[PoseParams], count: int) -> list[_Staged]:
    """Farthest-point selection of ``count`` views, seeded by the views already kept."""
    if count <= 0:
        return []
    remaining = list(staged)
    chosen: list[_Staged] = []
    anchors = list(prior)
    if not anchors:
        first = max(remaining, key=lambda s: s.sample.sharpness)
        chosen.append(first)
        remaining.remove(first)
        anchors.append(first.sample.pose)
    while remaining and len(chosen) < count:
        best = max(
            remaining,
            key=lambda s: min(pose_distance(s.sample.pose, a) for a in anchors),
        )
        chosen.append(best)
        remaining.remove(best)
        anchors.append(best.sample.pose)
    return chosen


def _frame_size(probe: dict[str, Any]) -> tuple[int, int]:
    width, height = int(probe.get("width") or 0), int(probe.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RecordingError(probe.get("error") or "Could not read the video size.")
    # Frames are read as stored (-noautorotate): a rotation flag does not turn them.
    return width, height


def next_capture_index(frames_dir: Path) -> int:
    numbers = [
        int(match.group(1))
        for path in frames_dir.glob("capture_*.jpg")
        if (match := re.fullmatch(r"capture_(\d+)\.jpg", path.name))
    ]
    return max(numbers, default=0) + 1


def extract_views(
    clip: Path,
    *,
    config: AppConfig,
    rec: RecordingConfig,
    probe: dict[str, Any],
    frames_dir: Path,
    overlays_dir: Path,
    prior_poses: list[PoseParams] | None = None,
    prior_scores: list[float] | None = None,
    progress: Callable[[float], None] | None = None,
) -> ExtractResult:
    """Stream ``clip`` through ffmpeg and keep the views worth solving (module docstring).

    ``prior_poses`` and ``prior_scores`` (size-weighted sharpness, the ``weighted`` of
    each kept view) are the views the run already kept, from earlier clips.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MissingToolError(TOOLS_MISSING)
    width, height = _frame_size(probe)
    duration = float(probe.get("duration_s") or 0.0)
    prior = list(prior_poses or [])
    frames_dir.mkdir(parents=True, exist_ok=True)
    overlays_dir.mkdir(parents=True, exist_ok=True)
    staging = frames_dir.parent / f"_staging_{clip.stem}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        return _extract_into(
            clip, config, rec, width, height, duration, prior, list(prior_scores or []),
            frames_dir, overlays_dir, staging, ffmpeg, progress,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _extract_into(
    clip: Path,
    config: AppConfig,
    rec: RecordingConfig,
    width: int,
    height: int,
    duration: float,
    prior: list[PoseParams],
    prior_scores: list[float],
    frames_dir: Path,
    overlays_dir: Path,
    staging: Path,
    ffmpeg: str,
    progress: Callable[[float], None] | None,
) -> ExtractResult:
    board = config.board
    min_markers = config.capture.min_markers
    dictionary = resolve_dictionary(board.aruco_dict)
    params = detector_params()
    half_size = (max(width // 2, 1), max(height // 2, 1))
    limit = motion_limit_px(config.capture.max_motion_px, width, rec.sample_hz)
    selector = _Selector(
        config=config, rec=rec, staging=staging, prior_poses=prior, prior_scores=prior_scores
    )
    nbytes = width * height

    command = [
        # -noautorotate: calibrate the pixels as stored; the clip check flags a rotation.
        ffmpeg, "-v", "error", "-nostdin", "-noautorotate", "-i", str(clip),
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", f"fps={rec.sample_hz:g}", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    prev_centers = None
    index = -1
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr)
        try:
            while True:
                buf = _read_exact(proc.stdout, nbytes)
                if buf is None:
                    break
                index += 1
                time_s = index / rec.sample_hz
                selector.counts["samples"] += 1
                if progress is not None and duration > 0:
                    progress(min(time_s / duration, 1.0))
                gray = np.frombuffer(buf, dtype=np.uint8).reshape((height, width))
                half = cv2.resize(gray, half_size, interpolation=cv2.INTER_AREA)
                quick = detect_markers(half, half_size, board, dictionary, params)
                detection = None
                if quick is not None and quick.marker_count >= min_markers:
                    detection = detect_markers(gray, (width, height), board, dictionary, params)
                if detection is None or detection.marker_count < min_markers:
                    selector.drop("no_board")
                    selector.close_window()
                    prev_centers = None
                    continue
                motion = marker_motion(prev_centers, detection.centers)
                prev_centers = detection.centers
                if motion is None or motion > limit:
                    selector.drop("moving")
                    selector.close_window()
                    continue
                selector.add_still(
                    _Sample(
                        index=index,
                        time_s=time_s,
                        detection=detection,
                        sharpness=board_sharpness(half, quick),
                        motion_px=float(motion),
                        frame=gray.copy(),
                    )
                )
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        decode_error = None
        if proc.returncode not in (0, None):
            stderr.seek(0)
            detail = ffmpeg_error_line(stderr.read().decode(errors="replace"), clip)
            if selector.counts["samples"] == 0:
                raise RecordingError(
                    "ffmpeg could not decode this clip" + (f": {detail}." if detail else ".")
                )
            decode_error = detail or f"ffmpeg stopped with exit code {proc.returncode}"

    kept_staged = selector.finish()
    first = next_capture_index(frames_dir)
    number = first
    kept: list[dict[str, Any]] = []
    written: list[Path] = []
    try:
        for staged in kept_staged:
            sample = staged.sample
            name = f"capture_{number:03d}.jpg"
            written.append(frames_dir / name)
            shutil.move(str(staged.path), frames_dir / name)
            frame = cv2.imread(str(frames_dir / name))
            if frame is None:
                raise RecordingError(f"Could not read back {name} from the frames folder.")
            overlay = draw_detection(
                frame,
                sample.detection,
                f"{name} {clip.name} t={sample.time_s:.1f}s",
                selected=True,
            )
            written.append(overlays_dir / name)
            cv2.imwrite(str(overlays_dir / name), overlay)
            kept.append(
                {
                    "name": name,
                    "clip": clip.name,
                    "time_s": round(sample.time_s, 3),
                    "markers": sample.detection.marker_count,
                    "sharpness": round(sample.sharpness, 4),
                    "weighted": sample.weighted,
                    "motion_px": round(sample.motion_px, 3),
                    "pose": sample.pose.as_dict(),
                }
            )
            number += 1
    except BaseException:
        # Leave frames/ as it was: the run's poses must match the files the solve reads.
        for path in written:
            path.unlink(missing_ok=True)
        raise
    selector.counts["kept"] = len(kept)
    if progress is not None:
        progress(1.0)
    return ExtractResult(
        counts=selector.counts,
        kept=kept,
        poses=[s.sample.pose for s in kept_staged],
        image_size=(width, height),
        first_index=first,
        next_index=number,
        motion_limit_px=limit,
        read_s=selector.counts["samples"] / rec.sample_hz,
        duration_s=duration,
        decode_error=decode_error,
    )


def incomplete_row(result: ExtractResult) -> dict[str, Any]:
    """A clip-check row for a clip ffmpeg could not read to the end."""
    read, total = result.read_s, result.duration_s
    if total > 0 and read < INCOMPLETE_READ_FRACTION * total:
        advice = (
            f"Only the first {read:.0f} s of {total:.0f} s could be read. "
            "Copy the file from the card again."
        )
    else:
        advice = (
            f"ffmpeg reported an error while reading the clip ({result.decode_error}). "
            "Copy the file from the card again."
        )
    return {
        "field": "complete",
        "label": "Whole clip read",
        "expected": None if total <= 0 else f"{total:.0f} s",
        "found": f"{read:.0f} s",
        "status": "mismatch",
        "advice": advice,
    }


# ---------------------------------------------------------------------------
# Guide words for the recording animation
# ---------------------------------------------------------------------------

_PLACE_WORDS = {
    "center": "centre",
    "left": "left edge",
    "right": "right edge",
    "top": "top edge",
    "bottom": "bottom edge",
    "top-left": "top-left corner",
    "top-right": "top-right corner",
    "bottom-left": "bottom-left corner",
    "bottom-right": "bottom-right corner",
    "upper-left": "upper left",
    "upper-right": "upper right",
    "lower-left": "lower left",
    "lower-right": "lower right",
}
_DISTANCE_WORDS = {"small": "far", "medium": "middle distance", "large": "near"}


def recording_guide(config: AppConfig) -> dict[str, Any]:
    """The route checkpoints in order, with plain words for the animation caption."""
    checkpoints = []
    for index, checkpoint in enumerate(default_checkpoints(config.coverage_targets)):
        words = checkpoint.label.split()
        if words[0] == "tilt":
            place, distance, tilted = words[1], "middle distance", True
        else:
            place, distance, tilted = words[0], _DISTANCE_WORDS.get(words[1], words[1]), False
        place_text = _PLACE_WORDS.get(place, place.replace("-", " "))
        parts = (["tilted"] if tilted else [distance]) + [place_text]
        checkpoints.append(
            {
                "index": index,
                "label": checkpoint.label,
                "x": checkpoint.x,
                "y": checkpoint.y,
                "size": checkpoint.size,
                "skew": checkpoint.skew,
                "distance": distance,
                "place": place_text,
                "tilted": tilted,
                "caption": " · ".join(parts),
            }
        )
    return {"checkpoints": checkpoints, "total": len(checkpoints)}


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def _truncate_utf8(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def sanitise_clip_name(name: str) -> str:
    """A safe file name for an upload: no folders, and short enough in bytes (not
    characters) for any file system, with room for a ``_2`` retake suffix."""
    base = Path((name or "").replace("\\", "/")).name
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in base).strip("._")
    if not cleaned:
        cleaned = "clip.mp4"
    path = Path(cleaned)
    suffix = path.suffix if len(path.suffix.encode("utf-8")) <= MAX_NAME_SUFFIX_BYTES else ""
    stem = cleaned[: len(cleaned) - len(suffix)] if suffix else cleaned
    stem = _truncate_utf8(stem, MAX_NAME_STEM_BYTES) or "clip"
    return stem + suffix


def camera_name_from_serial(serial: str) -> str | None:
    digits = re.sub(r"[^A-Za-z0-9]", "", serial or "")
    if len(digits) < 4:
        return None
    return f"gopro13_{digits[-4:].lower()}"


def _shipped_camera_names() -> set[str]:
    names = {DEFAULT_CAMERA_NAME}
    for path in sorted(presets.SHIPPED_PRESETS_DIR.glob("*.yaml")):
        try:
            names.add(presets.load_preset_file(path)[1].camera.camera_name)
        except ValueError:
            continue
    return names


def describe_recording_mode(
    camera: CameraConfig, rec: RecordingConfig, clip_check: dict[str, Any] | None
) -> dict[str, Any]:
    """The acquisition mode of a recording-route run, in the live route's keys where
    they mean the same thing (the UI's mode line reads lens_fov, max_lens_mod,
    frame_size and fps). Lens and mod are what the preset asks for: the file cannot
    confirm them."""
    probe = (clip_check or {}).get("probe") or {}
    meta = (clip_check or {}).get("metadata") or {}
    fps = probe.get("avg_frame_rate") or camera.fps
    mode = {
        "route": "recording",
        "source": "gopro_recording",
        "camera_name": camera.camera_name,
        "frame_size": f"{int(camera.width)}x{int(camera.height)}",
        "fps": round(float(fps), 3),
        "lens_fov": rec.lens,
        "max_lens_mod": f"{rec.lens_mod_name} ({rec.lens_mod})",
        "hypersmooth": rec.hypersmooth,
        "calibration_shutter": rec.calibration_shutter,
        "iso_max": rec.iso_max,
        "codec": probe.get("codec"),
        "camera_model": meta.get("model"),
        "serial": meta.get("serial"),
        "firmware": meta.get("firmware"),
    }
    return mode


# Everything _reset_run replaces, so a camera switch can be undone (_undo_switch).
_RUN_FIELDS = (
    "run_id",
    "output_dir",
    "camera_name",
    "named_from_serial",
    "_timestamp",
    "_poses",
    "_kept",
    "_clips",
    "_image_size",
    "_refused",
    "previous_run_id",
    "previous_output_dir",
    "_camera_switch",
    "_switch_serial",
    "_before_switch",
    "_generation",
    "_active_generation",
)


class RecordingJob:
    """One recording-route run at a time, processed in a background thread.

    Three threads touch it: the event loop (the upload), the threadpool (start, new,
    status) and the job thread. Every state change goes through the lock, and a busy
    check and the state it guards are set under the same hold. ``start`` and ``new``
    are refused while busy, so the job thread owns the run's views until it leaves the
    busy states; its writes also carry the run's generation, so a stale one is dropped.
    """

    def __init__(self, runs_dir: Path | None = None):
        self.runs_dir = runs_dir or default_runs_dir()
        self.config = AppConfig()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._active_generation = 0
        self._reset_run()
        self._status: dict[str, Any] = {}
        self._set_idle("no recording run yet")

    # -- state ----------------------------------------------------------

    def _reset_run(self) -> None:
        self.run_id: str | None = None
        self.output_dir: Path | None = None
        self.camera_name: str | None = None
        self.named_from_serial = False
        self._timestamp = ""
        self._poses: list[PoseParams] = []
        self._kept: list[dict[str, Any]] = []
        self._clips: list[dict[str, Any]] = []
        self._image_size: tuple[int, int] | None = None
        self._refused: dict[str, Any] | None = None
        # Set when a clip from another camera started this run (see _switch_camera).
        self.previous_run_id: str | None = None
        self.previous_output_dir: Path | None = None
        self._camera_switch: dict[str, Any] | None = None
        # The serial of the clip that started this run by a camera switch, so the run
        # knows its camera before any clip has joined it.
        self._switch_serial: str | None = None
        # The previous camera's run, to put back if this one never holds a clip.
        self._before_switch: tuple[dict[str, Any], dict[str, Any]] | None = None
        self._generation += 1

    @property
    def rec(self) -> RecordingConfig:
        return self.config.recording or RecordingConfig()

    @property
    def state(self) -> str:
        with self._lock:
            return self._status.get("state", "idle")

    @property
    def busy(self) -> bool:
        return self.state in BUSY_STATES

    def _run_serial(self) -> str | None:
        """The serial of the camera this run belongs to: its first clip that has one, or
        the clip that started the run by a camera switch."""
        for clip in self._clips:
            serial = (clip.get("metadata") or {}).get("serial")
            if serial:
                return serial
        return self._switch_serial

    def _mismatch_sentence(self) -> str:
        """" 2 settings differ from the preset in A and B: x, y." plus a line for clips
        that could not be read to the end (not a setting, but as loud)."""
        settings: list[str] = []
        setting_clips: list[str] = []
        cut: list[str] = []
        for clip in self._clips:
            rows = [row for row in clip.get("check") or [] if row["status"] == "mismatch"]
            if any(row["field"] == "complete" for row in rows):
                cut.append(clip["name"])
            rows = [row for row in rows if row["field"] != "complete"]
            if rows:
                setting_clips.append(clip["name"])
            for row in rows:
                if row["label"] not in settings:
                    settings.append(row["label"])
        text = ""
        if settings:
            text += (
                f" {len(settings)} setting{'s differ' if len(settings) != 1 else ' differs'} "
                f"from the preset in {join_names(setting_clips)}: {', '.join(settings).lower()}."
            )
        if cut:
            text += (
                f" Only part of {join_names(cut)} could be read: copy "
                f"{'it' if len(cut) == 1 else 'them'} from the card again."
            )
        return text

    def _mismatches(self) -> tuple[list[str], list[str]]:
        """The distinct settings that differ from the preset, and the clips they are in."""
        labels: list[str] = []
        clips: list[str] = []
        for clip in self._clips:
            rows = [row for row in clip.get("check") or [] if row["status"] == "mismatch"]
            if rows:
                clips.append(clip["name"])
            for row in rows:
                if row["label"] not in labels:
                    labels.append(row["label"])
        return labels, clips

    def _run_status(self) -> dict[str, Any]:
        totals = empty_counts()
        for clip in self._clips:
            for key, value in (clip.get("counts") or {}).items():
                totals[key] = totals.get(key, 0) + value
        targets = self.config.coverage_targets
        mismatch_fields, mismatch_clips = self._mismatches()
        return {
            "route": "recording",
            "run_id": self.run_id,
            "run_dir": "" if self.output_dir is None else str(self.output_dir),
            "output_dir": "" if self.output_dir is None else str(self.output_dir),
            "camera_name": self.camera_name,
            "camera_named_from_serial": self.named_from_serial,
            "camera_name_note": (
                f"Named this camera {self.camera_name} from its serial number, so its "
                "files match the physical camera."
                if self.named_from_serial
                else None
            ),
            "serial": self._run_serial(),
            "image_size": None if self._image_size is None else list(self._image_size),
            "captures": len(self._poses),
            "coverage": coverage_summary(self._poses, targets),
            "guide": guide_status(self._poses, None, targets),
            "clips": [dict(clip) for clip in self._clips],
            "counts": totals,
            # Distinct settings that differ from the preset, over every clip of the run.
            "mismatch_count": len(mismatch_fields),
            "mismatch_fields": mismatch_fields,
            "mismatch_clips": mismatch_clips,
            "refused_clip": None if self._refused is None else dict(self._refused),
            # A clip from another camera started this run; the previous camera's run
            # is left exactly as it was.
            "previous_run_id": self.previous_run_id,
            "previous_output_dir": (
                None if self.previous_output_dir is None else str(self.previous_output_dir)
            ),
            "camera_switch": None if self._camera_switch is None else dict(self._camera_switch),
            "min_frames": self.config.solver.min_frames,
            "preview_open": False,
        }

    def _set(self, generation: int | None = None, **updates: Any) -> bool:
        """Update the status; with ``generation``, only while that run is current."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            self._status.update(updates)
            return True

    def _publish(self, generation: int, **updates: Any) -> bool:
        """Update the status with the run's current views, clips and counts."""
        with self._lock:
            if generation != self._generation:
                return False
            self._status.update({**self._run_status(), **updates})
            return True

    def _set_idle(self, message: str, state: str = "idle") -> None:
        with self._lock:
            self._status = {
                "state": state,
                "stage": None,
                "progress": 0.0,
                "message": message,
                "upload": None,
                **no_results(),
                **self._run_status(),
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.status()

    # -- run lifecycle ---------------------------------------------------

    def start(self, config: AppConfig, runs_dir: Path | None = None) -> dict[str, Any]:
        """Open a run. Raises ``ValueError`` for a preset without a recording section
        (live route only) and ``RecordingError`` while a clip is being processed."""
        if config.recording is None:
            raise ValueError(NO_RECORDING_SETTINGS)
        require_tools()
        with self._lock:
            if self.busy:
                raise RecordingError(BUSY_MESSAGE)
            if runs_dir is not None:
                self.runs_dir = Path(runs_dir)
            self._reset_run()
            self.config = config
            self.camera_name = config.camera.camera_name
            self._timestamp = time.strftime("%Y%m%d_%H%M%S")
            try:
                self._make_run_dir()
            except OSError as exc:
                partial = self.output_dir
                self._reset_run()
                if partial is not None:
                    shutil.rmtree(partial, ignore_errors=True)
                message = (
                    f"Could not make the run folder in {self.runs_dir}"
                    + (f" ({exc.strerror})" if exc.strerror else "")
                    + ". Check that it can be written to, then start again."
                )
                self._set_idle(message, state="error")
                raise RunFolderError(message) from exc
            self._set_idle("Ready for the clip.", state="ready")
            return self.status()

    def new(self, config: AppConfig | None = None) -> dict[str, Any]:
        with self._lock:
            if self.busy:
                raise RecordingError(BUSY_MESSAGE)
            self._reset_run()
            if config is not None:
                self.config = config
            self._set_idle("Ready for the next camera.")
            return self.status()

    def _unique_run_dir(self, camera_name: str) -> Path:
        """runs/<camera>_<timestamp>/, with _2, _3... if that folder exists already."""
        base = self.runs_dir / f"{camera_name}_{self._timestamp}"
        path, counter = base, 2
        while path.exists():
            path = base.with_name(f"{base.name}_{counter}")
            counter += 1
        return path

    def _make_run_dir(self) -> None:
        self.output_dir = self._unique_run_dir(self.camera_name or DEFAULT_CAMERA_NAME)
        self.run_id = self.output_dir.name
        for sub in ("clips", "frames", "overlays"):
            (self.output_dir / sub).mkdir(parents=True, exist_ok=True)
        self._write_run_config()

    def _write_run_config(self) -> None:
        if self.output_dir is None:
            return
        config = self.config.model_copy(deep=True)
        # The camera as recorded (clip size and rate), matching acquisition_mode; the
        # preset's webcam size and rate are for the live route only.
        config.camera = self._solve_camera()
        clip_check = self._clips[-1] if self._clips else None
        payload = {
            "config": config.model_dump(),
            "config_note": (
                "config.camera is the camera as recorded (the clip's size and frame rate, "
                "or the preset's recording size before the first clip), not the preset's "
                "webcam settings, which only the live USB route uses."
            ),
            "acquisition_mode": describe_recording_mode(
                self._solve_camera(), self.rec, clip_check
            ),
            "clips": [clip["name"] for clip in self._clips],
        }
        (self.output_dir / "config.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    def _solve_camera(self) -> CameraConfig:
        """The camera as recorded: the clip's size and rate, not the webcam's."""
        rec = self.rec
        width, height = self._image_size or (rec.width, rec.height)
        fps = rec.fps
        if self._clips:
            fps = (self._clips[-1].get("probe") or {}).get("avg_frame_rate") or fps
        return self.config.camera.model_copy(
            update={
                "camera_name": self.camera_name or self.config.camera.camera_name,
                "width": int(width),
                "height": int(height),
                "fps": float(fps),
            }
        )

    # -- upload ----------------------------------------------------------

    def begin_upload(self, name: str, total_bytes: int | None = None) -> Path:
        """Refused (and nothing written) while busy, before a run, or without ffmpeg."""
        with self._lock:
            if self.busy:
                raise RecordingError(BUSY_MESSAGE)
            if self.output_dir is None:
                raise RecordingError("Start a recording run first.")
            if missing_tools():
                self._status.update(
                    state="error", stage="upload", progress=0.0, message=TOOLS_MISSING
                )
                raise MissingToolError(TOOLS_MISSING)
            clips_dir = self.output_dir / "clips"
            try:
                clips_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                message = (
                    f"Could not write to the run folder {self.output_dir}"
                    + (f" ({exc.strerror})" if exc.strerror else "")
                    + ". Check that it can be written to, then drop the clip again."
                )
                self._status.update(state="error", stage="upload", message=message)
                raise RunFolderError(message) from exc
            clean = sanitise_clip_name(name)
            path = clips_dir / clean
            stem, suffix = path.stem, path.suffix
            counter = 2
            while path.exists():
                path = clips_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            self._status.update(
                state="uploading",
                stage="upload",
                progress=0.0,
                message=f"Copying {path.name} into the run folder.",
                upload={"name": path.name, "received_bytes": 0, "total_bytes": total_bytes},
            )
            return path

    def upload_progress(self, received_bytes: int) -> None:
        with self._lock:
            upload = dict(self._status.get("upload") or {})
            upload["received_bytes"] = received_bytes
            total = upload.get("total_bytes")
            self._status["upload"] = upload
            if total:
                self._status["progress"] = min(received_bytes / total, 1.0)

    def upload_failed(self, path: Path, message: str) -> None:
        path.unlink(missing_ok=True)
        with self._lock:
            if self._status.get("state") == "uploading":
                self._status.update(state="error", stage="upload", message=message)

    def start_processing(self, clip: Path) -> dict[str, Any]:
        with self._lock:
            if self._status.get("state") != "uploading":
                raise RecordingError("No upload is in progress for this clip.")
            self._status.update(
                state="analysing", stage="check", progress=0.0, message="Checking the clip."
            )
            generation = self._generation
            self._thread = threading.Thread(
                target=self._process,
                args=(clip, generation),
                name="gopro-charuco-recording",
                daemon=True,
            )
            self._thread.start()
            return self.status()

    # -- processing (background thread) ----------------------------------

    def _process(self, clip: Path, generation: int) -> None:
        # A clip from another camera moves the job to a new run (a new generation).
        self._active_generation = generation
        try:
            self._process_clip(clip, generation)
        except MissingToolError as exc:
            # Not the clip's fault: it stays in clips/, out of the run's list.
            self._publish(self._active_generation, state="error", stage="check", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - the job must end in a state, never hang
            reason = str(exc) or type(exc).__name__
            few = re.match(r"Need at least (\d+) usable frames, found (\d+)", reason)
            if few:
                message = (
                    f"Only {few.group(2)} usable views so far; solving needs {few.group(1)}. "
                    "Record another clip covering the missing positions and drop it here."
                )
            else:
                message = (
                    f"Processing {clip.name} failed: {reason.rstrip('.')}. The views picked "
                    "so far are kept; drop another clip, or try this one again."
                )
            self._publish(
                self._active_generation,
                state="error",
                message=self._with_switch_note(message),
                needs_more_views=bool(few),
            )

    def _check(self, clip: Path, run_serial: str | None) -> dict[str, Any]:
        """The clip check; a failure reading the file's metadata gives unknown rows."""
        try:
            return check_clip(clip, self.rec, run_serial)
        except Exception as exc:  # noqa: BLE001 - the check warns, it never blocks
            return fallback_check(clip, self.rec, exc)

    def _process_clip(self, clip: Path, generation: int) -> None:
        """Check, extract and solve. A clip from another camera starts a new run of its
        own (``_switch_camera``). A clip the run cannot use (not a video, a different
        size, or reading it failed) never joins the run: its file is deleted and it
        shows as ``refused_clip`` (reason ``unreadable``, ``different_size``, ``failed``,
        or ``no_run_folder`` when another camera's run could not be opened), so it cannot
        feed the mismatch warnings, the summary or config.json. If the refused clip had
        started a new run by a camera switch, that run is removed and the previous one
        is current again (``_undo_switch``). A missing ffmpeg is never the clip's fault:
        it stays."""
        require_tools()
        rec = self.rec
        with self._lock:
            self._camera_switch = None  # the note is about the clip that caused it
        run_serial = self._run_serial()
        check = self._check(clip, run_serial)
        serial = check["metadata"].get("serial")
        size = None
        if "error" not in check["probe"]:
            try:
                size = _frame_size(check["probe"])
            except RecordingError:
                size = None
        if size is None:
            detail = check["probe"].get("error") or "no picture size in the file"
            self._refuse(
                clip, check, generation, reason="unreadable",
                message=(
                    f"{clip.name} is not a video ffmpeg can read ({detail}). {NOT_USED} "
                    "Drop the original file from the camera's card."
                ),
            )
            return
        if run_serial and serial and serial != run_serial:
            try:
                clip, generation = self._switch_camera(clip, serial, run_serial)
            except OSError as exc:
                # _switch_camera put this run back as it was.
                self._refuse(
                    clip, check, generation, reason="no_run_folder",
                    message=(
                        f"{clip.name} is from another camera (serial {serial}), but a "
                        "folder for its calibration could not be made "
                        f"({exc.strerror or exc}). {NOT_USED} Make room on the disk, or "
                        "check that the runs folder can be written to, then drop the clip "
                        f"again. This camera's run {self.run_id} is still open."
                    ),
                )
                return
            # In its own run the serial is no longer a mismatch.
            rows = compare(check["probe"], check["metadata"], rec)
            check = {
                **check,
                "check": rows,
                "mismatch_count": sum(1 for row in rows if row["status"] == "mismatch"),
            }
        if self._image_size is not None and size != self._image_size:
            # Only a clip in the camera setup's own mode is worth a run of its own; one in
            # another mode is recorded again with the settings of step 2.
            setup = (rec.width, rec.height)
            matches_setup = size == setup
            if matches_setup:
                advice = (
                    "This clip is in the camera setup's mode and the first one was not. To "
                    "give this clip a run of its own, click Start this camera again, then "
                    "drop it."
                )
            else:
                advice = (
                    f"This clip is not in the camera setup's mode ({setup[0]}x{setup[1]}): "
                    "set the camera up again as in step 2, record the clip again, then drop "
                    "the new clip."
                )
            self._refuse(
                clip, check, generation, reason="different_size",
                message=(
                    f"{clip.name} is {size[0]}x{size[1]} but this run's first clip was "
                    f"{self._image_size[0]}x{self._image_size[1]}. {NOT_USED} Record every "
                    f"clip of one run in the same mode. {advice}"
                ),
                extra={"size": list(size), "matches_setup": matches_setup},
            )
            return
        if self.output_dir is None:
            raise RecordingError("This run has no folder any more; start a new run.")

        entry: dict[str, Any] = {
            "name": clip.name,
            "size_bytes": clip.stat().st_size,
            "check": check["check"],
            "mismatch_count": check["mismatch_count"],
            "probe": check["probe"],
            "metadata": {k: v for k, v in check["metadata"].items() if k != "tags"},
            "counts": None,
        }
        with self._lock:
            self._refused = None
            self._clips.append(entry)
        self._publish(
            generation,
            stage="extract",
            progress=0.0,
            message=self._with_switch_note(f"Picking views from {clip.name}."),
        )
        try:
            result = extract_views(
                clip,
                config=self.config,
                rec=rec,
                probe=check["probe"],
                frames_dir=self.output_dir / "frames",
                overlays_dir=self.output_dir / "overlays",
                prior_poses=self._poses,
                prior_scores=[view["weighted"] for view in self._kept],
                progress=lambda fraction: self._set(generation, progress=round(fraction, 4)),
            )
        except MissingToolError:
            with self._lock:
                self._clips.remove(entry)
                # The clip stays in this run's clips/, so the run is no longer undoable.
                self._before_switch = None
            raise
        except Exception as exc:  # noqa: BLE001 - the clip leaves the run, then the job ends
            with self._lock:
                self._clips.remove(entry)
                # A run this clip started by a camera switch would be left empty.
                undo = self._before_switch is not None and not self._clips
            reason = str(exc) or f"{type(exc).__name__} while reading {clip.name}."
            message = (
                f"Could not pick views from {clip.name}: {reason.rstrip('.')}. "
                f"{NOT_USED} Drop the clip again."
            )
            if undo:
                generation = self._undo_switch()
                message = (
                    f"{clip.name} is from another camera (serial {serial}), but views could "
                    f"not be picked from it: {reason.rstrip('.')}. The clip was not used, "
                    "and the folder made for that camera was removed. Drop the clip again. "
                    f"This camera's run {self.run_id} is still open."
                )
            self._refuse(clip, check, generation, reason="failed", message=message)
            return
        with self._lock:
            # The clip is in the run: a run it started by a camera switch stays.
            self._before_switch = None
            entry["counts"] = result.counts
            entry["kept"] = [view["name"] for view in result.kept]
            entry["motion_limit_px"] = round(result.motion_limit_px, 3)
            entry["read_s"] = round(result.read_s, 3)
            if result.incomplete:
                # Cut short (a copy that stopped early): warn, and use what was read.
                entry["check"] = [*entry["check"], incomplete_row(result)]
                entry["mismatch_count"] += 1
            self._image_size = tuple(result.image_size)
        # Named after the camera only once the clip is in the run (frames move with it).
        self._maybe_name_from_serial(serial, generation)
        with self._lock:
            self._poses.extend(result.poses)
            self._kept.extend(result.kept)
        self._write_run_config()
        self._publish(generation)
        self._solve(generation)

    def _switch_camera(self, clip: Path, serial: str, run_serial: str) -> tuple[Path, int]:
        """Leave this run exactly as it is and start a new run, same config, for the
        camera that recorded ``clip``; the clip is moved (not copied) into it.

        The new run is named from the clip's serial even when the operator named the
        first camera: that name belongs to the previous camera. Returns the clip's new
        path and the new run's generation.
        """
        with self._lock:
            previous_id, previous_dir = self.run_id, self.output_dir
            before = {name: getattr(self, name) for name in _RUN_FIELDS}
            before_results = {key: self._status.get(key) for key in no_results()}
            self._reset_run()
            # From here the job's writes belong to the new run, even if opening it fails.
            self._active_generation = self._generation
            self._before_switch = (before, before_results)
            self._switch_serial = serial
            self.camera_name = camera_name_from_serial(serial) or self.config.camera.camera_name
            self.named_from_serial = camera_name_from_serial(serial) is not None
            self._timestamp = time.strftime("%Y%m%d_%H%M%S")
            try:
                self._make_run_dir()
                assert self.output_dir is not None
                target = self.output_dir / "clips" / clip.name
                try:
                    clip.rename(target)
                except OSError:
                    shutil.move(str(clip), target)  # another file system: still a move
            except BaseException:
                self._undo_switch()
                raise
            message = (
                f"This clip is from another camera (serial {serial}): started a separate "
                f"calibration for it. The previous camera's calibration is in {previous_id}."
            )
            self.previous_run_id = previous_id
            self.previous_output_dir = previous_dir
            self._camera_switch = {
                "clip": target.name,
                "serial": serial,
                "previous_serial": run_serial,
                "previous_run_id": previous_id,
                "message": message,
            }
            generation = self._generation
        # Nothing of the previous camera's calibration may show under this run's name.
        self._publish(generation, message=message, **no_results())
        return target, generation

    def _undo_switch(self) -> int:
        """Put the previous camera's run back, as it was before ``_switch_camera``, when
        the run the switch started never held a clip; that run's folder is removed.
        Returns the previous run's generation, which the job writes under again."""
        with self._lock:
            assert self._before_switch is not None
            before, before_results = self._before_switch
            failed_dir = self.output_dir
            for name, value in before.items():
                setattr(self, name, value)
            self._status.update({**self._run_status(), **before_results})
            generation = self._generation
        if failed_dir is not None:
            # A fresh folder from _unique_run_dir: only this switch wrote into it.
            shutil.rmtree(failed_dir, ignore_errors=True)
        return generation

    def _with_switch_note(self, message: str) -> str:
        """Lead with the camera-switch note while the clip that caused it is processed."""
        switch = self._camera_switch
        if switch is None or message.startswith(switch["message"]):
            return message
        return f"{switch['message']} {message}"

    def _refuse(
        self,
        clip: Path,
        check: dict[str, Any],
        generation: int,
        *,
        reason: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """A clip the run cannot use never joins it: one run is one camera in one mode.
        The run's earlier clips, views and results stay in the status. ``extra`` adds
        reason-specific fields (``different_size``: the clip's ``size`` and whether it
        ``matches_setup``, the camera setup's width and height)."""
        clip.unlink(missing_ok=True)
        with self._lock:
            self._refused = {
                "name": clip.name,
                "reason": reason,
                "serial": check["metadata"].get("serial"),
                "run_serial": self._run_serial(),
                "check": check["check"],
                "message": message,
                **(extra or {}),
            }
        self._publish(
            generation, state="error", stage="check", progress=0.0,
            message=self._with_switch_note(message),
        )

    def _maybe_name_from_serial(self, serial: str | None, generation: int) -> None:
        """Name the run after the camera's serial when the name is still a default."""
        new_name = camera_name_from_serial(serial or "")
        if (
            new_name is None
            or self.named_from_serial
            or self._poses
            or self.camera_name not in _shipped_camera_names()
            or self.output_dir is None
        ):
            return
        old_dir = self.output_dir
        new_dir = self._unique_run_dir(new_name)
        try:
            old_dir.rename(new_dir)
        except OSError:
            return  # keep the preset's name rather than fail the clip
        with self._lock:
            self.camera_name = new_name
            self.named_from_serial = True
            self.output_dir = new_dir
            self.run_id = new_dir.name
        self._write_run_config()
        self._set(generation, message=f"Named this camera {new_name} from its serial number.")

    def _solve(self, generation: int) -> None:
        if self.output_dir is None:
            raise RecordingError("This run has no folder any more; start a new run.")
        min_frames = self.config.solver.min_frames
        if len(self._poses) < min_frames:
            self._publish(
                generation,
                state="error",
                stage="extract",
                needs_more_views=True,
                message=self._with_switch_note(
                    f"Only {len(self._poses)} usable views so far; solving needs {min_frames}. "
                    "Record another clip that holds the board still at more positions, "
                    "and add it to this run."
                ),
            )
            return
        self._set(generation, state="solving", stage="solve", progress=0.0, message="Solving.")
        camera = self._solve_camera()
        summary = solve_from_frames(
            frames_dir=self.output_dir / "frames",
            output_dir=self.output_dir,
            camera=camera,
            board_config=self.config.board,
            solver=self.config.solver,
            coverage_targets=self.config.coverage_targets,
        )
        orbslam3 = self._write_orbslam3(summary)
        clip_check = self._clips[-1] if self._clips else None
        run = self._run_status()
        summary["route"] = "recording"
        summary["acquisition_mode"] = describe_recording_mode(camera, self.rec, clip_check)
        summary["rejected_points"] = _discarded_points(summary)
        summary["recording"] = {
            "clips": run["clips"],
            "counts": run["counts"],
            "mismatch_count": run["mismatch_count"],
            "mismatch_fields": run["mismatch_fields"],
            "mismatch_clips": run["mismatch_clips"],
            "serial": run["serial"],
            "camera_named_from_serial": self.named_from_serial,
            "previous_run_id": self.previous_run_id,
            "views": self._kept,
            "orbslam3": orbslam3,
        }
        summary_path = self.output_dir / "caib_marker_board_calibration_summary.json"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        failed = [r.get("model", "unknown") for r in summary["results"] if r.get("ok") is False]
        message = "Calibration solved."
        if failed:
            message = f"Calibration solved; these models failed: {', '.join(failed)}."
        message += self._mismatch_sentence()
        # One update, state included: the job stays busy until the run is complete.
        self._publish(
            generation,
            state="solved",
            stage="done",
            progress=1.0,
            message=self._with_switch_note(message),
            needs_more_views=False,
            results=summary["results"],
            summary_path=str(summary_path),
            acquisition_mode=summary["acquisition_mode"],
            rejected_points=summary["rejected_points"],
            orbslam3=orbslam3,
        )

    def _write_orbslam3(self, summary: dict[str, Any]) -> dict[str, Any] | None:
        """Rewrite the KB8 block at UMI's SLAM size when the recording is 4:3."""
        kb = next(
            (
                r for r in summary.get("results", [])
                if r.get("model") == "kannala_brandt" and r.get("ok") and r.get("orbslam3_yaml")
            ),
            None,
        )
        if kb is None:
            return None
        rec = self.rec
        width, height = summary["image_size"]
        path = Path(kb["orbslam3_yaml"])
        if width * rec.orbslam3_height == height * rec.orbslam3_width:
            size = (rec.orbslam3_width, rec.orbslam3_height)
            path.write_text(orbslam3_kb8_yaml(kb["openicc"], size), encoding="utf-8")
            note = (
                f"Written for {size[0]}x{size[1]} frames, the size UMI's SLAM runs at; "
                f"the json files stay at {width}x{height}."
            )
        else:
            size = (width, height)
            shape = Fraction(rec.orbslam3_width, rec.orbslam3_height)
            note = (
                f"The clip is {width}x{height}, not {shape.numerator}:{shape.denominator}, "
                "so the block stays at the clip's own size."
            )
        kb["orbslam3_size"] = list(size)
        kb["orbslam3_note"] = note
        return {"path": str(path), "size": list(size), "note": note}
