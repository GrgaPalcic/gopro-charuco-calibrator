from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .boards import detector_params, resolve_dictionary
from .bridge import (
    bridge_log_path,
    start_decode,
    stop_gopro_video_bridge,
    stream_disabled_result,
    stream_error_result,
    stream_ok_result,
)
from .coverage import PoseParams, coverage_summary, pose_distance
from .detection import detect_markers, draw_detection, marker_motion
from .gopro import (
    apply_gopro_settings,
    describe_acquisition_mode,
    restart_gopro_webcam,
    stop_gopro_webcam,
)
from .guide import guide_status
from .models import AppConfig
from .solver import solve_from_frames


def _discarded_points(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Board positions of frames the recommended solve did not use, with a kind.

    kind="error" means discarded for high reprojection error (repeat that area);
    kind="surplus" means a good frame trimmed only because the selection cap was
    reached (no action needed). Maps each discarded frame name to its pose via the
    summary frame list.
    """
    results = summary.get("results") or []
    if not results:
        return []
    recommended = min(
        results,
        key=lambda r: r.get("median_view_error_px", r.get("rms", 1e9)) or 1e9,
    )
    rejected = (recommended.get("selected") or {}).get("rejected_frames") or []
    pose_by_name = {
        frame["name"]: frame
        for frame in (summary.get("frames") or [])
        if "name" in frame
    }
    points: list[dict[str, Any]] = []
    for entry in rejected:
        frame = pose_by_name.get(entry.get("name"))
        if frame is None:
            continue
        reason = str(entry.get("reason", ""))
        kind = "error" if reason.startswith("view_error_px") else "surplus"
        points.append(
            {
                "x": frame.get("x"),
                "y": frame.get("y"),
                "size": frame.get("size"),
                "skew": frame.get("skew"),
                "error": entry.get("all_view_error_px"),
                "kind": kind,
            }
        )
    return points


def _read_exact(stream, nbytes: int) -> bytes | None:
    """Read exactly ``nbytes`` from a pipe, or None on EOF (producer died)."""
    chunks: list[bytes] = []
    remaining = nbytes
    while remaining > 0:
        block = stream.read(remaining)
        if not block:
            return None
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


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
        # Live MJPEG frame buffer. A dedicated condition (not self._lock) decouples
        # frame delivery from status updates and keeps lock-hold times minimal.
        self._frame_cond = threading.Condition()
        self._latest_jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._jpeg_quality = 82
        self._manual_capture = False
        self._capture_enabled = False
        self._paused = False
        self._route_done = False
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
        self._route_done = False
        self._prev_centers = None
        self._last_capture = 0.0
        self._last_preview = 0.0
        self._run_start_time = time.monotonic()
        self._state = "capturing"
        self._write_run_config()
        self._set_status(state="capturing", message="new run started", rejected_points=[])
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

    def next_camera(self, config: AppConfig | None = None) -> dict[str, Any]:
        # Cleanly end the current camera: stop the stream and exit webcam mode
        # (close() joins the capture thread and kills ffmpeg), then reset the run
        # and go idle so the operator can swap the camera and click Open Preview.
        # This avoids reopening preview on a stale thread, which froze the frame.
        self.close()
        self._captured_poses = []
        self._capture_count = 0
        self._route_done = False
        self.run_id = None
        self.output_dir = None
        self.frames_dir = None
        self.overlays_dir = None
        if config is not None:
            self.config = config
        self._set_status(state="idle", message="stopped; ready for next camera")
        return self.status()

    def close(self) -> dict[str, Any]:
        self._capture_enabled = False
        self._paused = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
        # Leave a clean state: the capture thread has released the V4L2 device, so
        # now stop the local ffmpeg bridge (frees /dev/video*, so the loopback can
        # be removed and a reopen starts a fresh low-latency bridge) and tell the
        # GoPro to exit webcam mode.
        if self.config.gopro.enabled:
            try:
                stop_gopro_video_bridge(self.config)
                stop_gopro_webcam(self._last_gopro_result)
            except Exception:  # noqa: BLE001 - close() must never raise
                pass
        # Drop the last frame so a reopened stream starts blank instead of showing
        # the previous camera's still image until new frames arrive.
        with self._frame_cond:
            self._latest_jpeg = None
            self._jpeg_seq += 1
            self._frame_cond.notify_all()
        self._state = "idle"
        self._set_status(state="idle", message="preview closed")
        return self.status()

    def request_capture(self) -> dict[str, Any]:
        with self._lock:
            self._manual_capture = True
        return self.status()

    def _publish_jpeg(self, image) -> None:
        """Encode a frame to JPEG in memory and wake any MJPEG stream consumers."""
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality])
        if not ok:
            return
        data = buf.tobytes()
        with self._frame_cond:
            self._latest_jpeg = data
            self._jpeg_seq += 1
            self._frame_cond.notify_all()

    def wait_for_jpeg(self, last_seq: int, timeout: float) -> tuple[bytes | None, int]:
        """Block until a frame newer than ``last_seq`` is available (or timeout).

        Returns ``(jpeg_bytes, seq)``. The timeout lets a streaming generator
        re-check client/stop state instead of blocking forever on a dead client.
        """
        with self._frame_cond:
            if self._jpeg_seq == last_seq:
                self._frame_cond.wait(timeout)
            return self._latest_jpeg, self._jpeg_seq

    def latest_jpeg(self) -> tuple[bytes | None, int]:
        with self._frame_cond:
            return self._latest_jpeg, self._jpeg_seq

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
        summary["acquisition_mode"] = describe_acquisition_mode(self.config)
        summary["rejected_points"] = _discarded_points(summary)
        summary_path = self.output_dir / "caib_marker_board_calibration_summary.json"
        with summary_path.open("w", encoding="utf-8") as stream:
            json.dump(summary, stream, indent=2)
        self._state = "solved"
        self._set_status(
            state="solved",
            message="calibration solve complete",
            summary_path=str(summary_path),
            results=summary["results"],
            acquisition_mode=summary["acquisition_mode"],
            rejected_points=summary["rejected_points"],
        )
        return summary

    def _write_run_config(self) -> None:
        if self.output_dir is None:
            return
        payload = {
            "config": self.config.model_dump(),
            "acquisition_mode": describe_acquisition_mode(self.config),
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
            "rejected_points": [],
        }

    def _set_status(self, **updates) -> None:
        with self._lock:
            self._status.update(updates)

    def _fail_stream(self, result: dict[str, Any]) -> None:
        self._last_bridge_result = result
        self._state = "error"
        self._set_status(
            state="error",
            message=str(result.get("error", "video stream failed")),
            gopro=self._last_gopro_result,
            video_bridge=result,
        )

    def _run(self) -> None:
        camera = self.config.camera
        board = self.config.board
        dictionary = resolve_dictionary(board.aruco_dict)
        params = detector_params()
        width, height = int(camera.width), int(camera.height)
        log_path = bridge_log_path(camera.device, self.config.gopro.webcam_port)

        # GoPro: decode the UDP stream with ffmpeg straight into a pipe (no
        # v4l2loopback, no mux/demux hop). Otherwise open the V4L2 device directly.
        proc = None
        cap = None
        if self.config.gopro.enabled:
            started = start_decode(self.config, log_path)
            if isinstance(started, str):
                self._fail_stream(
                    stream_error_result(self.config, self._last_gopro_result, log_path, started)
                )
                return
            proc = started
            nbytes = width * height * 3
            stdout = proc.stdout

            def read_one():
                buf = _read_exact(stdout, nbytes)
                if buf is None:
                    return None
                return np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 3)).copy()
        else:
            cap = self._open_capture()
            if not cap.isOpened():
                self._fail_stream(
                    stream_error_result(
                        self.config, self._last_gopro_result, log_path,
                        f"failed to open {camera.device}.",
                    )
                )
                return
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or width
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or height
            self._last_bridge_result = stream_disabled_result()

            def read_one():
                ok, frame = cap.read()
                return frame if ok else None

        image_size = (width, height)

        # The reader thread keeps only the NEWEST frame; the processing loop (slower,
        # because of ChArUco detection) consumes the latest and drops anything in
        # between, so latency stays ~one frame instead of growing a backlog.
        reader_stop = threading.Event()
        frame_cond = threading.Condition()
        latest: dict[str, Any] = {"frame": None, "seq": 0, "alive": True}

        def _reader() -> None:
            try:
                while not reader_stop.is_set() and not self._stop.is_set():
                    frame = read_one()
                    if frame is None:
                        break
                    with frame_cond:
                        latest["frame"] = frame
                        latest["seq"] += 1
                        frame_cond.notify_all()
            finally:
                with frame_cond:
                    latest["alive"] = False
                    frame_cond.notify_all()

        reader = threading.Thread(target=_reader, name="gopro-charuco-reader", daemon=True)
        reader.start()

        try:
            # Phase 1: wait for the first frame. A GoPro can take several seconds
            # to actually emit UDP after entering webcam mode, and sometimes the
            # first start_webcam does not take, so we wait patiently and re-kick the
            # webcam periodically. This is what makes a single Open Preview reliable
            # instead of needing several clicks.
            start = time.monotonic()
            deadline = start + 25.0
            next_rekick = start + 7.0
            while not self._stop.is_set():
                with frame_cond:
                    if latest["seq"] == 0 and latest["alive"]:
                        frame_cond.wait(0.5)
                    seq = latest["seq"]
                    alive = latest["alive"]
                if seq > 0:
                    break
                if proc is not None and proc.poll() is not None:
                    self._fail_stream(stream_error_result(
                        self.config, self._last_gopro_result, log_path,
                        f"ffmpeg exited early (code {proc.returncode}).",
                    ))
                    return
                if not alive:
                    self._fail_stream(stream_error_result(
                        self.config, self._last_gopro_result, log_path,
                        "Decoder ended before any frame arrived.",
                    ))
                    return
                now = time.monotonic()
                if now > deadline:
                    self._fail_stream(stream_error_result(
                        self.config, self._last_gopro_result, log_path,
                        "No video frames after 25s. The camera may not have entered "
                        "webcam mode, or a firewall is blocking the UDP video. If you "
                        "already allowed the firewall, replug the GoPro and try again.",
                    ))
                    return
                # Re-kick the webcam if it has not started streaming yet (GoPro only).
                if proc is not None and now >= next_rekick:
                    restart_gopro_webcam(self.config.gopro, self._last_gopro_result)
                    next_rekick = now + 7.0
                self._set_status(
                    state=self._state,
                    message=f"waiting for GoPro stream ({int(now - start)}s)",
                    gopro=self._last_gopro_result,
                    video_bridge=self._last_bridge_result,
                )
            if self._stop.is_set():
                return

            if proc is not None:
                self._last_bridge_result = stream_ok_result(log_path)
            self._set_status(
                state=self._state,
                message=f"previewing {width}x{height}",
                image_size=[width, height],
                video_bridge=self._last_bridge_result,
            )

            # Phase 2: process the freshest frame as fast as detection allows.
            last_seq = 0
            while not self._stop.is_set():
                with frame_cond:
                    if latest["seq"] == last_seq and latest["alive"]:
                        frame_cond.wait(0.5)
                    frame = latest["frame"]
                    last_seq = latest["seq"]
                    alive = latest["alive"]
                if frame is not None:
                    self._process_frame(frame, image_size, dictionary, params)
                if not alive and latest["seq"] == last_seq:
                    break
        finally:
            reader_stop.set()
            if proc is not None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            reader.join(timeout=2.0)
            if proc is not None:
                try:
                    proc.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()
            if cap is not None:
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
            # Keep only the most recent frame so the reader never serves stale ones.
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
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

        # Publish every processed frame to the in-memory MJPEG stream (the smooth
        # live video source). Disk JPEGs are only a low-rate fallback for
        # /api/session/latest.jpg, so they stay throttled.
        self._publish_jpeg(overlay)
        latest_dir = self.output_dir or self.preview_dir
        latest_dir.mkdir(parents=True, exist_ok=True)
        if now - self._last_preview >= 0.2:
            cv2.imwrite(
                str(latest_dir / "latest_detection.jpg"),
                overlay,
                [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality],
            )
            cv2.imwrite(
                str(latest_dir / "latest_frame.jpg"),
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality],
            )
            self.latest_detection_path = latest_dir / "latest_detection.jpg"
            self._last_preview = now

        guide = guide_status(self._captured_poses, pose, self.config.coverage_targets)
        # A run finishes when the guide route is complete (capture and guide
        # reinforce each other) or the safety cap is hit, NOT merely when the
        # recommended sample target is reached -- otherwise hitting target_samples
        # would disable capture and freeze the guide before the last (tilt) poses.
        target_reached = self._capture_count >= capture.target_samples
        if self._state == "capturing":
            if guide.get("complete") and not self._route_done:
                # First time the route is complete: stop and let the operator get a
                # quality check (the UI auto-solves). _route_done means a later
                # Resume can keep capturing extra/repeat poses without re-completing.
                self._route_done = True
                self._capture_enabled = False
                self._state = "complete"
                message = f"guide route complete with {self._capture_count} captures"
            elif self._capture_count >= capture.max_samples:
                self._capture_enabled = False
                self._state = "complete"
                message = (
                    f"capture limit ({capture.max_samples}) reached; guide route "
                    "incomplete but coverage may be sufficient -- check results"
                )
            elif target_reached:
                message = f"{message}; minimum reached, finish the guide or Solve"

        self._set_status(
            state=self._state,
            message=message,
            run_id=self.run_id,
            captures=self._capture_count,
            target_samples=capture.target_samples,
            max_samples=capture.max_samples,
            target_reached=target_reached,
            markers=0 if detection is None else detection.marker_count,
            pose=None if pose is None else pose.as_dict(),
            motion_px=motion,
            manual_capture_pending=self._manual_capture,
            coverage=coverage_summary(self._captured_poses, self.config.coverage_targets),
            guide=guide,
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
