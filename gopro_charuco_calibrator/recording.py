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
   is under ``blur_ratio`` x the median over every held position of the clip (kept or
   not, so a retake that repeats positions still has a reference) is ``blurred``. Sharpness is
   the variance of the Laplacian inside the board's box on the half-size image; a far
   board has denser edges and so scores higher, which multiplying by the board's
   apparent size roughly offsets.
6. **Cap**: past ``max_views`` views in the run, the most varied are kept (farthest
   pose first, starting from the views already kept) and the rest are ``over_cap``.

Winners wait on disk (``_staging_<clip>/``) until the clip ends, so memory holds only a
couple of frames whatever the clip length. Kept views become ``frames/capture_###.jpg``
(full resolution, grey) and ``overlays/capture_###.jpg`` so ``solve_from_frames`` works
unchanged; numbering continues across clips.
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
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import presets
from .boards import detector_params, resolve_dictionary
from .capture import _discarded_points, _read_exact, default_runs_dir
from .clipcheck import check_clip
from .coverage import PoseParams, coverage_summary, pose_distance
from .detection import MarkerDetection, detect_markers, draw_detection, marker_motion
from .guide import default_checkpoints, guide_status
from .models import AppConfig, CameraConfig, RecordingConfig
from .openicc import orbslam3_kb8_yaml
from .solver import solve_from_frames

DROP_REASONS = ("no_board", "blurred", "moving", "duplicate", "over_cap")
# The live motion gate is per frame at about this rate and width (see module docstring).
MOTION_REFERENCE_HZ = 30.0
MOTION_REFERENCE_WIDTH = 1920.0
BUSY_STATES = ("uploading", "analysing", "solving")
DEFAULT_CAMERA_NAME = CameraConfig().camera_name


class RecordingError(RuntimeError):
    pass


def empty_counts() -> dict[str, int]:
    return {"samples": 0, "kept": 0, **{reason: 0 for reason in DROP_REASONS}}


def motion_limit_px(max_motion_px: float, width: int, sample_hz: float) -> float:
    return max_motion_px * (width / MOTION_REFERENCE_WIDTH) * (MOTION_REFERENCE_HZ / sample_hz)


def board_sharpness(gray: np.ndarray, detection: MarkerDetection) -> float:
    """Variance of the Laplacian inside the board's bounding box."""
    points = detection.all_points()
    h, w = gray.shape[:2]
    x0, y0 = np.floor(points.min(axis=0)).astype(int)
    x1, y1 = np.ceil(points.max(axis=0)).astype(int)
    crop = gray[max(y0, 0) : min(y1 + 1, h), max(x0, 0) : min(x1 + 1, w)]
    if crop.size < 16:
        return 0.0
    return float(cv2.Laplacian(crop, cv2.CV_64F).var())


@dataclass
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


@dataclass
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


@dataclass
class _Selector:
    """Streaming selection state for one clip (see the module docstring)."""

    config: AppConfig
    rec: RecordingConfig
    staging: Path
    prior_poses: list[PoseParams]
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
        if len(self.winner_scores) >= 3:
            median = float(np.median(self.winner_scores))
            sharp = [s for s in staged if s.sample.weighted >= self.rec.blur_ratio * median]
            self.drop("blurred", len(staged) - len(sharp))
            staged = sharp
        room = max(self.rec.max_views - len(self.prior_poses), 0)
        if len(staged) > room:
            chosen = _most_varied(staged, self.prior_poses, room)
            self.drop("over_cap", len(staged) - len(chosen))
            staged = sorted(chosen, key=lambda s: s.sample.index)
        return staged


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
    # ffmpeg applies the rotation stored in the file, so the frames come out turned.
    if abs(int(probe.get("rotation") or 0)) % 180 == 90:
        width, height = height, width
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
    progress: Callable[[float], None] | None = None,
) -> ExtractResult:
    """Stream ``clip`` through ffmpeg and keep the views worth solving (module docstring)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RecordingError("ffmpeg is not installed; install it and drop the clip again.")
    width, height = _frame_size(probe)
    duration = float(probe.get("duration_s") or 0.0)
    prior = list(prior_poses or [])
    frames_dir.mkdir(parents=True, exist_ok=True)
    overlays_dir.mkdir(parents=True, exist_ok=True)
    staging = frames_dir.parent / f"_staging_{clip.stem}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    board = config.board
    min_markers = config.capture.min_markers
    dictionary = resolve_dictionary(board.aruco_dict)
    params = detector_params()
    half_size = (max(width // 2, 1), max(height // 2, 1))
    limit = motion_limit_px(config.capture.max_motion_px, width, rec.sample_hz)
    selector = _Selector(config=config, rec=rec, staging=staging, prior_poses=prior)
    nbytes = width * height

    command = [
        ffmpeg, "-v", "error", "-nostdin", "-i", str(clip),
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
        if proc.returncode not in (0, None) and selector.counts["samples"] == 0:
            stderr.seek(0)
            tail = stderr.read().decode(errors="replace").strip().splitlines()
            shutil.rmtree(staging, ignore_errors=True)
            raise RecordingError(
                "ffmpeg could not decode this clip"
                + (f": {tail[-1]}" if tail else ".")
            )

    kept_staged = selector.finish()
    first = next_capture_index(frames_dir)
    number = first
    kept: list[dict[str, Any]] = []
    for staged in kept_staged:
        sample = staged.sample
        name = f"capture_{number:03d}.jpg"
        shutil.move(str(staged.path), frames_dir / name)
        frame = cv2.imread(str(frames_dir / name))
        overlay = draw_detection(
            frame,
            sample.detection,
            f"{name} {clip.name} t={sample.time_s:.1f}s",
            selected=True,
        )
        cv2.imwrite(str(overlays_dir / name), overlay)
        kept.append(
            {
                "name": name,
                "clip": clip.name,
                "time_s": round(sample.time_s, 3),
                "markers": sample.detection.marker_count,
                "sharpness": round(sample.sharpness, 2),
                "motion_px": round(sample.motion_px, 3),
                "pose": sample.pose.as_dict(),
            }
        )
        number += 1
    shutil.rmtree(staging, ignore_errors=True)
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
    )


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


def sanitise_clip_name(name: str) -> str:
    base = Path((name or "").replace("\\", "/")).name
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in base).strip("._")
    if not cleaned:
        cleaned = "clip.mp4"
    return cleaned[:120]


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


class RecordingJob:
    """One recording-route run at a time, processed in a background thread."""

    def __init__(self, runs_dir: Path | None = None):
        self.runs_dir = runs_dir or default_runs_dir()
        self.config = AppConfig()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
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
        self._state = "idle"

    @property
    def rec(self) -> RecordingConfig:
        return self.config.recording or RecordingConfig()

    @property
    def busy(self) -> bool:
        return self._state in BUSY_STATES

    def _run_status(self) -> dict[str, Any]:
        totals = empty_counts()
        for clip in self._clips:
            for key, value in (clip.get("counts") or {}).items():
                totals[key] = totals.get(key, 0) + value
        targets = self.config.coverage_targets
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
            "image_size": None if self._image_size is None else list(self._image_size),
            "captures": len(self._poses),
            "coverage": coverage_summary(self._poses, targets),
            "guide": guide_status(self._poses, None, targets),
            "clips": [dict(clip) for clip in self._clips],
            "counts": totals,
            "mismatch_count": sum(clip.get("mismatch_count", 0) for clip in self._clips),
            "min_frames": self.config.solver.min_frames,
            "preview_open": False,
        }

    def _set(self, **updates: Any) -> None:
        with self._lock:
            self._status.update(updates)

    def _set_idle(self, message: str) -> None:
        with self._lock:
            self._status = {
                "state": "idle",
                "stage": None,
                "progress": 0.0,
                "message": message,
                "upload": None,
                "results": None,
                "summary_path": None,
                "acquisition_mode": None,
                "rejected_points": [],
                "needs_more_views": False,
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

    def start(self, config: AppConfig) -> dict[str, Any]:
        if self.busy:
            raise RecordingError("A clip is still being processed; wait for it to finish.")
        self._reset_run()
        self.config = config
        self.camera_name = config.camera.camera_name
        self._timestamp = time.strftime("%Y%m%d_%H%M%S")
        self._make_run_dir()
        self._state = "ready"
        self._set_idle("Ready for the clip.")
        self._set(state="ready")
        return self.status()

    def new(self, config: AppConfig | None = None) -> dict[str, Any]:
        if self.busy:
            raise RecordingError("A clip is still being processed; wait for it to finish.")
        self._reset_run()
        if config is not None:
            self.config = config
        self._set_idle("Ready for the next camera.")
        return self.status()

    def _make_run_dir(self) -> None:
        self.run_id = f"{self.camera_name}_{self._timestamp}"
        self.output_dir = self.runs_dir / self.run_id
        for sub in ("clips", "frames", "overlays"):
            (self.output_dir / sub).mkdir(parents=True, exist_ok=True)
        self._write_run_config()

    def _write_run_config(self) -> None:
        if self.output_dir is None:
            return
        config = self.config.model_copy(deep=True)
        config.camera.camera_name = self.camera_name or config.camera.camera_name
        clip_check = self._clips[-1] if self._clips else None
        payload = {
            "config": config.model_dump(),
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
        if self.busy:
            raise RecordingError("A clip is still being processed; wait for it to finish.")
        if self.output_dir is None:
            raise RecordingError("Start a recording run first.")
        clips_dir = self.output_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        clean = sanitise_clip_name(name)
        path = clips_dir / clean
        stem, suffix = path.stem, path.suffix
        counter = 2
        while path.exists():
            path = clips_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        self._state = "uploading"
        self._set(
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
        self._state = "error"
        self._set(state="error", stage="upload", message=message)

    def start_processing(self, clip: Path) -> dict[str, Any]:
        self._state = "analysing"
        self._set(state="analysing", stage="check", progress=0.0, message="Checking the clip.")
        self._thread = threading.Thread(
            target=self._process, args=(clip,), name="gopro-charuco-recording", daemon=True
        )
        self._thread.start()
        return self.status()

    # -- processing (background thread) ----------------------------------

    def _process(self, clip: Path) -> None:
        try:
            self._process_clip(clip)
        except Exception as exc:  # noqa: BLE001 - the job must end in a state, never hang
            self._state = "error"
            message = str(exc)
            self._set(
                state="error",
                message=message,
                # the solver's own "Need at least N usable frames"
                needs_more_views=message.startswith("Need at least"),
                **self._run_status(),
            )

    def _process_clip(self, clip: Path) -> None:
        rec = self.rec
        check = check_clip(clip, rec)
        entry: dict[str, Any] = {
            "name": clip.name,
            "size_bytes": clip.stat().st_size,
            "check": check["check"],
            "mismatch_count": check["mismatch_count"],
            "probe": check["probe"],
            "metadata": {k: v for k, v in check["metadata"].items() if k != "tags"},
            "counts": None,
        }
        self._clips.append(entry)
        clip = self._maybe_name_from_serial(clip, check["metadata"].get("serial"))
        self._set(**self._run_status())
        if "error" in check["probe"]:
            raise RecordingError(f"Could not read {clip.name}: {check['probe']['error']}")

        assert self.output_dir is not None
        size = _frame_size(check["probe"])
        if self._image_size is not None and size != self._image_size:
            raise RecordingError(
                f"{clip.name} is {size[0]}x{size[1]} but this run's first clip was "
                f"{self._image_size[0]}x{self._image_size[1]}. Record every clip of one run "
                "in the same mode, or start a new run for this one."
            )
        self._set(stage="extract", progress=0.0, message=f"Picking views from {clip.name}.")
        result = extract_views(
            clip,
            config=self.config,
            rec=rec,
            probe=check["probe"],
            frames_dir=self.output_dir / "frames",
            overlays_dir=self.output_dir / "overlays",
            prior_poses=self._poses,
            progress=lambda fraction: self._set(progress=round(fraction, 4)),
        )
        self._image_size = tuple(result.image_size)
        entry["counts"] = result.counts
        entry["kept"] = [view["name"] for view in result.kept]
        entry["motion_limit_px"] = round(result.motion_limit_px, 3)
        self._poses.extend(result.poses)
        self._kept.extend(result.kept)
        self._write_run_config()
        self._set(**self._run_status())
        self._solve()

    def _maybe_name_from_serial(self, clip: Path, serial: str | None) -> Path:
        """Name the run after the camera's serial when the name is still a default."""
        new_name = camera_name_from_serial(serial or "")
        if (
            new_name is None
            or self.named_from_serial
            or self._poses
            or self.camera_name not in _shipped_camera_names()
            or self.output_dir is None
        ):
            return clip
        old_dir = self.output_dir
        self.camera_name = new_name
        self.named_from_serial = True
        new_dir = self.runs_dir / f"{new_name}_{self._timestamp}"
        if not new_dir.exists():
            old_dir.rename(new_dir)
            self.output_dir = new_dir
            self.run_id = new_dir.name
            clip = new_dir / "clips" / clip.name
        self._write_run_config()
        self._set(message=f"Named this camera {new_name} from its serial number.")
        return clip

    def _solve(self) -> None:
        assert self.output_dir is not None
        min_frames = self.config.solver.min_frames
        if len(self._poses) < min_frames:
            self._state = "error"
            self._set(
                state="error",
                stage="extract",
                needs_more_views=True,
                message=(
                    f"Only {len(self._poses)} usable views so far; solving needs {min_frames}. "
                    "Record another clip that holds the board still at more positions, "
                    "and add it to this run."
                ),
                **self._run_status(),
            )
            return
        self._state = "solving"
        self._set(state="solving", stage="solve", progress=0.0, message="Solving.")
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
        summary["route"] = "recording"
        summary["acquisition_mode"] = describe_recording_mode(camera, self.rec, clip_check)
        summary["rejected_points"] = _discarded_points(summary)
        summary["recording"] = {
            "clips": [dict(clip) for clip in self._clips],
            "counts": self._run_status()["counts"],
            "mismatch_count": sum(clip.get("mismatch_count", 0) for clip in self._clips),
            "serial": next(
                (c["metadata"].get("serial") for c in self._clips if c["metadata"].get("serial")),
                None,
            ),
            "camera_named_from_serial": self.named_from_serial,
            "views": self._kept,
            "orbslam3": orbslam3,
        }
        summary_path = self.output_dir / "caib_marker_board_calibration_summary.json"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        failed = [r.get("model", "unknown") for r in summary["results"] if r.get("ok") is False]
        self._state = "solved"
        mismatches = summary["recording"]["mismatch_count"]
        message = "Calibration solved."
        if failed:
            message = f"Calibration solved; these models failed: {', '.join(failed)}."
        if mismatches:
            message += (
                f" {mismatches} setting{'s' if mismatches != 1 else ''} in the clip "
                "differ from the preset."
            )
        self._set(
            state="solved",
            stage="done",
            progress=1.0,
            message=message,
            needs_more_views=False,
            results=summary["results"],
            summary_path=str(summary_path),
            acquisition_mode=summary["acquisition_mode"],
            rejected_points=summary["rejected_points"],
            orbslam3=orbslam3,
            **self._run_status(),
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
            note = (
                f"The clip is {width}x{height}, not {rec.orbslam3_width}:{rec.orbslam3_height}, "
                "so the block stays at the clip's own size."
            )
        kb["orbslam3_size"] = list(size)
        kb["orbslam3_note"] = note
        return {"path": str(path), "size": list(size), "note": note}

