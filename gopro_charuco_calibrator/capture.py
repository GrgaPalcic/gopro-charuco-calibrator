from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import cv2

from .boards import detector_params, resolve_dictionary
from .bridge import ensure_gopro_video_bridge
from .coverage import PoseParams, coverage_summary, pose_distance
from .detection import detect_markers, draw_detection, marker_motion
from .gopro import apply_gopro_settings
from .guide import guide_status
from .models import AppConfig
from .solver import solve_from_frames


def default_runs_dir() -> Path:
    return Path.cwd() / "runs"


def _camera_stream_key(config: AppConfig) -> tuple[Any, ...]:
    camera = config.camera
    return (
        camera.device,
        camera.width,
        camera.height,
        camera.fps,
        camera.fourcc,
        config.board.aruco_dict,
    )


class CaptureSession:
    def __init__(self, config: AppConfig | None = None, runs_dir: Path | None = None):
        self.config = config or AppConfig()
        self.runs_dir = runs_dir or default_runs_dir()
        self.preview_dir = self.runs_dir / "_preview"
        self.run_id: str | None = None
        self.output_dir: Path | None = None
        self.frames_dir: Path | None = None
        self.overlays_dir: Path | None = None
        self.latest_detection_path = self.preview_dir / "latest_detection.jpg"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._manual_capture = False
        self._capture_enabled = False
        self._paused = False
        self._prev_centers = None
        self._captured_poses: list[PoseParams] = []
        self._capture_count = 0
        self._last_capture = 0.0
        self._last_preview = 0.0
        self._run_start_time = 0.0
        self._state = "idle"
        self._last_gopro_result: dict[str, Any] | None = None
        self._last_bridge_result: dict[str, Any] | None = None
        self._status: dict[str, Any] = self._base_status("idle", "no active preview")

    def open_preview(self, config: AppConfig | None = None) -> dict[str, Any]:
        if config is not None and _camera_stream_key(config) != _camera_stream_key(self.config):
            self.close()
        if config is not None:
            self.config = config
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        self.latest_detection_path = self.preview_dir / "latest_detection.jpg"
        if self.config.gopro.enabled and self.config.gopro.apply_on_preview:
            self._last_gopro_result = apply_gopro_settings(self.config.gopro)
            if not self._last_gopro_result.get("ok", False):
                self._set_status(
                    state="error",
                    message=str(self._last_gopro_result.get("error", "GoPro setup failed")),
                    gopro=self._last_gopro_result,
                )
                return self.status()
        if self._thread is None or not self._thread.is_alive():
            self._state = "preview"
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="gopro-charuco-preview",
                daemon=True,
            )
            self._thread.start()
        else:
            self._state = "preview" if not self._capture_enabled else self._state
            self._set_status(state=self._state, message="preview already open")
        return self.status()

    def start_run(self, config: AppConfig | None = None) -> dict[str, Any]:
        self.open_preview(config)
        if self._state == "error":
            return self.status()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.run_id = f"{self.config.camera.camera_name}_{timestamp}"
        self.output_dir = self.runs_dir / self.run_id
        self.frames_dir = self.output_dir / "frames"
        self.overlays_dir = self.output_dir / "overlays"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.overlays_dir.mkdir(parents=True, exist_ok=True)
        self.latest_detection_path = self.output_dir / "latest_detection.jpg"
        self._captured_poses = []
        self._capture_count = 0
        self._manual_capture = False
        self._paused = False
        self._capture_enabled = True
        self._prev_centers = None
        self._last_capture = 0.0
        self._last_preview = 0.0
        self._run_start_time = time.monotonic()
        self._state = "capturing"
        self._write_run_config()
        self._set_status(state="capturing", message="new run started")
        return self.status()

    def pause(self) -> dict[str, Any]:
        self._paused = True
        if self._capture_enabled:
            self._state = "paused"
        self._set_status(state=self._state, message="capture paused")
        return self.status()

    def resume(self) -> dict[str, Any]:
        if self.output_dir is not None:
            self._capture_enabled = True
            self._paused = False
            self._state = "capturing"
            self._set_status(state="capturing", message="capture resumed")
        return self.status()

    def next_camera(self, config: AppConfig) -> dict[str, Any]:
        self._capture_enabled = False
        self._paused = False
        self._captured_poses = []
        self._capture_count = 0
        self.run_id = None
        self.output_dir = None
        self.frames_dir = None
        self.overlays_dir = None
        return self.open_preview(config)

    def close(self) -> dict[str, Any]:
        self._capture_enabled = False
        self._paused = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
        self._state = "idle"
        self._set_status(state="idle", message="preview closed")
        return self.status()

    def request_capture(self) -> dict[str, Any]:
        with self._lock:
            self._manual_capture = True
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def solve(self) -> dict[str, Any]:
        if self.frames_dir is None or self.output_dir is None:
            raise RuntimeError("No active run to solve")
        self._capture_enabled = False
        self._paused = False
        self._state = "solving"
        self._set_status(state="solving", message="solving current run")
        summary = solve_from_frames(
            frames_dir=self.frames_dir,
            output_dir=self.output_dir,
            camera=self.config.camera,
            board_config=self.config.board,
            solver=self.config.solver,
            coverage_targets=self.config.coverage_targets,
        )
        summary["gopro"] = self._last_gopro_result
        summary["video_bridge"] = self._last_bridge_result
        summary_path = self.output_dir / "caib_marker_board_calibration_summary.json"
        with summary_path.open("w", encoding="utf-8") as stream:
            json.dump(summary, stream, indent=2)
        self._state = "solved"
        self._set_status(
            state="solved",
            message="calibration solve complete",
            summary_path=str(summary_path),
            results=summary["results"],
        )
        return summary

    def _write_run_config(self) -> None:
        if self.output_dir is None:
            return
        payload = {
            "config": self.config.model_dump(),
            "gopro_apply_result": self._last_gopro_result,
            "video_bridge_result": self._last_bridge_result,
        }
        with (self.output_dir / "config.json").open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)

    def _base_status(self, state: str, message: str) -> dict[str, Any]:
        return {
            "state": state,
            "run_id": self.run_id,
            "message": message,
            "captures": self._capture_count,
            "target_samples": self.config.capture.target_samples,
            "coverage": coverage_summary(self._captured_poses, self.config.coverage_targets),
            "guide": guide_status([], None, self.config.coverage_targets),
            "latest_detection_url": "/api/session/latest.jpg",
            "run_dir": "" if self.output_dir is None else str(self.output_dir),
            "preview_dir": str(self.preview_dir),
            "gopro": self._last_gopro_result,
            "video_bridge": self._last_bridge_result,
        }

    def _set_status(self, **updates) -> None:
        with self._lock:
            self._status.update(updates)

    def _run(self) -> None:
        camera = self.config.camera
        board = self.config.board
        dictionary = resolve_dictionary(board.aruco_dict)
        params = detector_params()
        self._last_bridge_result = ensure_gopro_video_bridge(self.config, self._last_gopro_result)
        if not self._last_bridge_result.get("ok", False):
            self._state = "error"
            self._set_status(
                state="error",
                message=str(self._last_bridge_result.get("error", "GoPro video bridge failed")),
                gopro=self._last_gopro_result,
                video_bridge=self._last_bridge_result,
            )
            return

        cap = self._open_capture()
        if not cap.isOpened():
            self._state = "error"
            message = f"failed to open {camera.device}"
            if self._last_bridge_result and self._last_bridge_result.get("enabled"):
                message += (
                    "; GoPro HTTP setup ran, but the V4L2 bridge is not producing a usable device"
                )
            self._set_status(
                state="error",
                message=message,
                gopro=self._last_gopro_result,
                video_bridge=self._last_bridge_result,
            )
            return

        image_size = (
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or camera.width,
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or camera.height,
        )
        self._set_status(
            state=self._state,
            message=f"previewing {camera.device} at {image_size[0]}x{image_size[1]}",
            image_size=list(image_size),
        )

        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    self._set_status(message="camera read failed")
                    time.sleep(0.2)
                    continue
                self._process_frame(frame, image_size, dictionary, params)
        finally:
            cap.release()

    def _open_capture(self):
        camera = self.config.camera
        deadline = time.monotonic() + (8.0 if self.config.gopro.enabled else 0.0)
        while True:
            cap = cv2.VideoCapture(camera.device, cv2.CAP_V4L2)
            if camera.fourcc:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*camera.fourcc[:4]))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, camera.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, camera.height)
            cap.set(cv2.CAP_PROP_FPS, camera.fps)
            if cap.isOpened():
                return cap
            if time.monotonic() >= deadline:
                return cap
            cap.release()
            self._set_status(
                state=self._state,
                message=f"waiting for {camera.device}",
                gopro=self._last_gopro_result,
                video_bridge=self._last_bridge_result,
            )
            time.sleep(0.5)

    def _process_frame(self, frame, image_size, dictionary, params) -> None:
        capture = self.config.capture
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        detection = detect_markers(gray, image_size, self.config.board, dictionary, params)
        now = time.monotonic()
        message = "no board"
        capture_now = False
        capture_reason = ""
        pose = None
        motion = None
        manual_capture = False
        with self._lock:
            manual_capture = self._manual_capture

        capture_active = self._capture_enabled and not self._paused and self._state == "capturing"
        if detection is not None:
            pose = detection.pose
            motion = marker_motion(self._prev_centers, detection.centers)
            nearest = min((pose_distance(pose, old) for old in self._captured_poses), default=999.0)
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
                    capture_active
                    and capture.auto_capture
                    and motion <= capture.max_motion_px
                    and nearest >= capture.min_param_dist
                    and now - self._last_capture >= capture.capture_cooldown_s
                )
                capture_reason = "auto" if capture_now else ""
            manual_ready = now - self._last_capture >= capture.capture_cooldown_s
            if capture_active and manual_capture and manual_ready:
                capture_now = detection.marker_count >= capture.min_markers
                capture_reason = "manual" if capture_now else ""
            if capture_active:
                warmup_remaining = capture.warmup_s - (now - self._run_start_time)
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

        if self._paused:
            message = f"paused; {message}"
        overlay = draw_detection(
            frame,
            detection,
            f"{self._capture_count}/{capture.target_samples} {message}",
            selected=capture_now,
        )
        if capture_now and pose is not None:
            self._save_capture(frame, overlay, pose, capture_reason)
            message = f"captured {self._capture_count:03d} ({capture_reason})"

        latest_dir = self.output_dir or self.preview_dir
        latest_dir.mkdir(parents=True, exist_ok=True)
        if now - self._last_preview >= 0.2:
            cv2.imwrite(
                str(latest_dir / "latest_detection.jpg"),
                overlay,
                [cv2.IMWRITE_JPEG_QUALITY, 82],
            )
            cv2.imwrite(
                str(latest_dir / "latest_frame.jpg"),
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, 82],
            )
            self.latest_detection_path = latest_dir / "latest_detection.jpg"
            self._last_preview = now

        if self._capture_count >= capture.target_samples and self._state == "capturing":
            self._capture_enabled = False
            self._state = "complete"
            message = f"complete with {self._capture_count} captures"

        self._set_status(
            state=self._state,
            message=message,
            run_id=self.run_id,
            captures=self._capture_count,
            target_samples=capture.target_samples,
            markers=0 if detection is None else detection.marker_count,
            pose=None if pose is None else pose.as_dict(),
            motion_px=motion,
            manual_capture_pending=self._manual_capture,
            coverage=coverage_summary(self._captured_poses, self.config.coverage_targets),
            guide=guide_status(self._captured_poses, pose, self.config.coverage_targets),
            run_dir="" if self.output_dir is None else str(self.output_dir),
            preview_dir=str(self.preview_dir),
            gopro=self._last_gopro_result,
            video_bridge=self._last_bridge_result,
        )

    def _save_capture(self, frame, overlay, pose: PoseParams, reason: str) -> None:
        if self.frames_dir is None or self.overlays_dir is None:
            return
        self._capture_count += 1
        self._captured_poses.append(pose)
        frame_path = self.frames_dir / f"capture_{self._capture_count:03d}.jpg"
        overlay_path = self.overlays_dir / f"capture_{self._capture_count:03d}.jpg"
        cv2.imwrite(str(frame_path), frame)
        cv2.imwrite(str(overlay_path), overlay)
        self._last_capture = time.monotonic()
        with self._lock:
            self._manual_capture = False
        self._set_status(message=f"captured {self._capture_count:03d} ({reason})")
