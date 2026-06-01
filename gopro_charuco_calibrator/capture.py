from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import cv2

from .boards import detector_params, resolve_dictionary
from .coverage import PoseParams, coverage_summary, pose_distance
from .detection import detect_markers, draw_detection, marker_motion
from .models import AppConfig
from .solver import solve_from_frames


def default_runs_dir() -> Path:
    return Path.cwd() / "runs"


class CaptureSession:
    def __init__(self, config: AppConfig, runs_dir: Path | None = None):
        self.config = config
        self.runs_dir = runs_dir or default_runs_dir()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.run_id = f"{self.config.camera.camera_name}_{timestamp}"
        self.output_dir = self.runs_dir / self.run_id
        self.frames_dir = self.output_dir / "frames"
        self.overlays_dir = self.output_dir / "overlays"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._manual_capture = False
        self._prev_centers = None
        self._captured_poses: list[PoseParams] = []
        self._capture_count = 0
        self._last_capture = 0.0
        self._last_preview = 0.0
        self._start_time = 0.0
        self._status: dict[str, Any] = {
            "state": "created",
            "run_id": self.run_id,
            "message": "not started",
            "captures": 0,
            "target_samples": self.config.capture.target_samples,
            "coverage": coverage_summary([], self.config.coverage_targets),
            "latest_detection_url": "/api/session/latest.jpg",
            "run_dir": str(self.output_dir),
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.overlays_dir.mkdir(parents=True, exist_ok=True)
        with (self.output_dir / "config.json").open("w", encoding="utf-8") as stream:
            json.dump(self.config.model_dump(), stream, indent=2)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"capture-{self.run_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def request_capture(self) -> None:
        with self._lock:
            self._manual_capture = True

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def solve(self) -> dict[str, Any]:
        if self._thread is not None and self._thread.is_alive():
            self.stop()
        summary = solve_from_frames(
            frames_dir=self.frames_dir,
            output_dir=self.output_dir,
            camera=self.config.camera,
            board_config=self.config.board,
            solver=self.config.solver,
            coverage_targets=self.config.coverage_targets,
        )
        with self._lock:
            self._status.update(
                {
                    "state": "solved",
                    "message": "calibration solve complete",
                    "summary_path": str(
                        self.output_dir / "caib_marker_board_calibration_summary.json"
                    ),
                    "results": summary["results"],
                }
            )
        return summary

    def _set_status(self, **updates) -> None:
        with self._lock:
            self._status.update(updates)

    def _run(self) -> None:
        camera = self.config.camera
        capture = self.config.capture
        board = self.config.board
        dictionary = resolve_dictionary(board.aruco_dict)
        params = detector_params()
        cap = cv2.VideoCapture(camera.device, cv2.CAP_V4L2)
        if camera.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*camera.fourcc[:4]))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, camera.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, camera.height)
        cap.set(cv2.CAP_PROP_FPS, camera.fps)
        if not cap.isOpened():
            self._set_status(state="error", message=f"failed to open {camera.device}")
            return

        image_size = (
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or camera.width,
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or camera.height,
        )
        self._start_time = time.monotonic()
        self._set_status(
            state="capturing",
            message=f"capturing {camera.device} at {image_size[0]}x{image_size[1]}",
            image_size=list(image_size),
        )

        try:
            while not self._stop.is_set() and self._capture_count < capture.target_samples:
                ok, frame = cap.read()
                if not ok or frame is None:
                    self._set_status(message="camera read failed")
                    time.sleep(0.2)
                    continue

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                detection = detect_markers(gray, image_size, board, dictionary, params)
                now = time.monotonic()
                message = "no board"
                capture_now = False
                capture_reason = ""
                pose = None
                motion = None
                manual_capture = False
                with self._lock:
                    manual_capture = self._manual_capture

                if detection is not None:
                    pose = detection.pose
                    motion = marker_motion(self._prev_centers, detection.centers)
                    nearest = min(
                        (pose_distance(pose, old) for old in self._captured_poses),
                        default=999.0,
                    )
                    message = f"{detection.marker_count} markers"
                    if detection.marker_count < capture.min_markers:
                        message += f"; need {capture.min_markers}"
                    elif motion is None:
                        message += "; hold still"
                    else:
                        message += (
                            f" motion={motion:.1f}px nearest={nearest:.2f}"
                            f" x={pose.x:.2f} y={pose.y:.2f}"
                            f" size={pose.size:.2f} skew={pose.skew:.2f}"
                        )
                        capture_now = (
                            capture.auto_capture
                            and motion <= capture.max_motion_px
                            and nearest >= capture.min_param_dist
                            and now - self._last_capture >= capture.capture_cooldown_s
                        )
                        capture_reason = "auto" if capture_now else ""
                    if manual_capture and now - self._last_capture >= capture.capture_cooldown_s:
                        capture_now = detection.marker_count >= capture.min_markers
                        capture_reason = "manual" if capture_now else ""
                    warmup_remaining = capture.warmup_s - (now - self._start_time)
                    if warmup_remaining > 0.0:
                        capture_now = False
                        capture_reason = ""
                        message += f" warmup={warmup_remaining:.1f}s"
                    self._prev_centers = {
                        marker_id: center.copy() for marker_id, center in detection.centers.items()
                    }
                else:
                    self._prev_centers = None
                    if manual_capture:
                        message = "manual pending; no board"

                overlay = draw_detection(
                    frame,
                    detection,
                    f"{self._capture_count}/{capture.target_samples} {message}",
                    selected=capture_now,
                )
                if capture_now and pose is not None:
                    self._capture_count += 1
                    self._captured_poses.append(pose)
                    frame_path = self.frames_dir / f"capture_{self._capture_count:03d}.jpg"
                    overlay_path = self.overlays_dir / f"capture_{self._capture_count:03d}.jpg"
                    cv2.imwrite(str(frame_path), frame)
                    cv2.imwrite(str(overlay_path), overlay)
                    self._last_capture = now
                    with self._lock:
                        self._manual_capture = False
                    message = f"captured {self._capture_count:03d} ({capture_reason})"

                if now - self._last_preview >= 0.2:
                    cv2.imwrite(
                        str(self.output_dir / "latest_detection.jpg"),
                        overlay,
                        [cv2.IMWRITE_JPEG_QUALITY, 82],
                    )
                    cv2.imwrite(
                        str(self.output_dir / "latest_frame.jpg"),
                        frame,
                        [cv2.IMWRITE_JPEG_QUALITY, 82],
                    )
                    self._last_preview = now

                self._set_status(
                    state="capturing",
                    message=message,
                    captures=self._capture_count,
                    target_samples=capture.target_samples,
                    markers=0 if detection is None else detection.marker_count,
                    pose=None if pose is None else pose.as_dict(),
                    motion_px=motion,
                    manual_capture_pending=self._manual_capture,
                    coverage=coverage_summary(self._captured_poses, self.config.coverage_targets),
                    run_dir=str(self.output_dir),
                )

            final_state = "complete" if self._capture_count >= capture.target_samples else "stopped"
            self._set_status(
                state=final_state,
                message=f"{final_state} with {self._capture_count} captures",
            )
        finally:
            cap.release()
