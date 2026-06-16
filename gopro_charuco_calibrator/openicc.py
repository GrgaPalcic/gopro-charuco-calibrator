"""Double Sphere calibration through the external OpenImuCameraCalibrator.

OpenICC (github.com/urbste/OpenImuCameraCalibrator) is AGPL-3.0, so it is kept at
arm's length: we exchange files with its ``calibrate_camera`` binary over a
subprocess boundary (a Docker image by default, or a native build via
``OPENICC_BINARY``). Nothing from OpenICC is imported, linked, or vendored.

The binary consumes a UBJSON "corners" file of arbitrary 3D<->2D correspondences
(no board-structure assumptions), so we feed it our own ArUco detections
directly: scene point ids are ``marker_id * 4 + corner_index`` and views are
keyed by synthetic microsecond timestamps. Backend knobs come from environment
variables so presets stay machine-portable:

- ``OPENICC_DOCKER_IMAGE`` (default ``openicc``)
- ``OPENICC_BINARY`` (path to a native calibrate_camera; bypasses Docker)
- ``OPENICC_GRID_SIZE`` (pose voxel filter, default 0.1)
- ``OPENICC_TIMEOUT_S`` (default 360)
- ``OPENICC_DOCKER_ROOT`` (set to 1 to skip the --user uid:gid mapping)
"""

from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from .models import BoardConfig, CameraConfig

if TYPE_CHECKING:
    from .solver import DetectionRecord

OPENICC_MIN_VIEWS = 10
MIN_POINTS_PER_VIEW = 4
# OpenICC removes views above an absolute ~2px reprojection threshold, which
# unfairly rejects 4K inputs (2px at 4K == 1px at 1080p). Solve at <=1080p-scale
# coordinates and scale the intrinsics back afterwards (matches the
# downsample_factor=2 workflow OpenICC itself documents for 4K GoPro footage).
MAX_SOLVE_HEIGHT = 1080
DEFAULT_DOCKER_IMAGE = "openicc"
DEFAULT_GRID_SIZE = 0.1
DEFAULT_TIMEOUT_S = 360.0
CALIBRATE_CAMERA_BIN = "/OpenImuCameraCalibrator/build/applications/calibrate_camera"
STDERR_TAIL_CHARS = 2000


class OpenICCError(RuntimeError):
    """Failure of the external OpenICC backend (setup, run, or output)."""


# ---------------------------------------------------------------------------
# Minimal UBJSON (draft-12) encoder. The venv has no pip, so no py-ubjson;
# nlohmann::json::from_ubjson accepts this standard, unoptimized encoding.
# ---------------------------------------------------------------------------


def _ubjson_int(value: int) -> bytes:
    if -128 <= value <= 127:
        return b"i" + struct.pack(">b", value)
    if 0 <= value <= 255:
        return b"U" + struct.pack(">B", value)
    if -32768 <= value <= 32767:
        return b"I" + struct.pack(">h", value)
    if -(2**31) <= value <= 2**31 - 1:
        return b"l" + struct.pack(">i", value)
    return b"L" + struct.pack(">q", value)


def _ubjson_key(key: str) -> bytes:
    # Object keys are length-prefixed strings WITHOUT the leading 'S' marker.
    data = key.encode("utf-8")
    return _ubjson_int(len(data)) + data


def ubjson_dumps(value: Any) -> bytes:
    if value is None:
        return b"Z"
    if isinstance(value, bool):  # before int: bool is an int subclass
        return b"T" if value else b"F"
    if isinstance(value, (int, np.integer)):
        return _ubjson_int(int(value))
    if isinstance(value, (float, np.floating)):
        return b"D" + struct.pack(">d", float(value))
    if isinstance(value, str):
        data = value.encode("utf-8")
        return b"S" + _ubjson_int(len(data)) + data
    if isinstance(value, dict):
        parts = [b"{"]
        for key, item in value.items():
            parts.append(_ubjson_key(str(key)))
            parts.append(ubjson_dumps(item))
        parts.append(b"}")
        return b"".join(parts)
    if isinstance(value, (list, tuple)):
        parts = [b"["]
        for item in value:
            parts.append(ubjson_dumps(item))
        parts.append(b"]")
        return b"".join(parts)
    raise TypeError(f"ubjson_dumps cannot encode {type(value).__name__}")


# ---------------------------------------------------------------------------
# Corners file (the calibrate_camera input)
# ---------------------------------------------------------------------------


def scene_point_id(marker_id: int, corner_index: int) -> int:
    return marker_id * 4 + corner_index


def build_corners_payload(
    *,
    records: Sequence[DetectionRecord],
    obj_by_id: dict[int, np.ndarray],
    image_size: tuple[int, int],
    square_size_m: float,
    fps: float,
    downscale: float = 1.0,
) -> dict[str, Any]:
    scene_pts: dict[str, list[float]] = {}
    for marker_id, corners3d in obj_by_id.items():
        points = np.asarray(corners3d, dtype=np.float64).reshape(4, 3)
        for corner_index in range(4):
            scene_pts[str(scene_point_id(int(marker_id), corner_index))] = [
                float(points[corner_index, 0]),
                float(points[corner_index, 1]),
                float(points[corner_index, 2]),
            ]

    step_us = max(1, round(1_000_000 / fps)) if fps > 0 else 33_333
    views: dict[str, Any] = {}
    for index, record in enumerate(records):
        image_points: dict[str, list[float]] = {}
        for corner, marker_id in zip(record.corners, record.ids.ravel(), strict=True):
            if int(marker_id) not in obj_by_id:
                continue
            pixels = np.asarray(corner, dtype=np.float64).reshape(4, 2) / downscale
            for corner_index in range(4):
                image_points[str(scene_point_id(int(marker_id), corner_index))] = [
                    float(pixels[corner_index, 0]),
                    float(pixels[corner_index, 1]),
                ]
        if len(image_points) < MIN_POINTS_PER_VIEW:
            continue
        views[str(index * step_us)] = {"image_points": image_points}

    if len(views) < OPENICC_MIN_VIEWS:
        raise OpenICCError(
            f"OpenICC double_sphere needs at least {OPENICC_MIN_VIEWS} views with "
            f"detections, found {len(views)}"
        )

    return {
        "calibration_board_type": 0,
        "square_size_meter": float(square_size_m),
        "camera_fps": float(fps),
        "image_width": int(round(image_size[0] / downscale)),
        "image_height": int(round(image_size[1] / downscale)),
        "scene_pts": scene_pts,
        "views": views,
    }


def write_corners_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ubjson_dumps(payload))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenICCSettings:
    docker_image: str = DEFAULT_DOCKER_IMAGE
    binary_path: str | None = None
    grid_size: float = DEFAULT_GRID_SIZE
    timeout_s: float = DEFAULT_TIMEOUT_S
    docker_run_as_user: bool = True


def settings_from_env(environ: Mapping[str, str] | None = None) -> OpenICCSettings:
    env = os.environ if environ is None else environ
    return OpenICCSettings(
        docker_image=env.get("OPENICC_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE),
        binary_path=env.get("OPENICC_BINARY") or None,
        grid_size=float(env.get("OPENICC_GRID_SIZE", DEFAULT_GRID_SIZE)),
        timeout_s=float(env.get("OPENICC_TIMEOUT_S", DEFAULT_TIMEOUT_S)),
        docker_run_as_user=env.get("OPENICC_DOCKER_ROOT", "") not in ("1", "true"),
    )


def _calibrate_flags(corners: str, out_prefix: str, grid_size: float) -> list[str]:
    return [
        f"--input_corners={corners}",
        "--camera_model_to_calibrate=DOUBLE_SPHERE",
        f"--save_path_calib_dataset={out_prefix}",
        f"--grid_size={grid_size}",
        "--optimize_board_points=false",
        "--verbose=true",
        "--logtostderr=1",
    ]


def build_command(
    work_dir: Path, settings: OpenICCSettings, container_name: str | None
) -> list[str]:
    if settings.binary_path:
        return [settings.binary_path] + _calibrate_flags(
            str(work_dir / "corners.uson"), str(work_dir / "out"), settings.grid_size
        )
    command = ["docker", "run", "--rm"]
    if container_name:
        command += ["--name", container_name]
    if settings.docker_run_as_user:
        command += ["--user", f"{os.getuid()}:{os.getgid()}"]
    command += ["-v", f"{work_dir.resolve()}:/data", settings.docker_image]
    command += [CALIBRATE_CAMERA_BIN] + _calibrate_flags(
        "/data/corners.uson", "/data/out", settings.grid_size
    )
    return command


def _result_json_path(work_dir: Path) -> Path:
    expected = work_dir / "out.json"
    if expected.is_file():
        return expected
    candidates = sorted(work_dir.glob("out*.json")) + sorted(work_dir.glob("out/*.json"))
    if len(candidates) == 1:
        return candidates[0]
    listing = ", ".join(sorted(p.name for p in work_dir.iterdir())) or "<empty>"
    raise OpenICCError(
        f"OpenICC produced no result JSON in {work_dir} (contents: {listing})"
    )


def run_calibrate_camera(work_dir: Path, settings: OpenICCSettings) -> dict[str, Any]:
    container_name = None if settings.binary_path else f"openicc-{uuid.uuid4().hex[:12]}"
    command = build_command(work_dir, settings, container_name)
    log_path = work_dir / "calibrate_camera.log"
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=settings.timeout_s
        )
    except FileNotFoundError as exc:
        raise OpenICCError(
            f"'{command[0]}' not found — install Docker and build the "
            f"'{settings.docker_image}' image, or set OPENICC_BINARY to a native "
            "calibrate_camera build"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        if container_name:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=10,
                check=False,
            )
        raise OpenICCError(
            f"OpenICC calibrate_camera timed out after {settings.timeout_s:.0f}s"
        ) from exc

    log_path.write_text(
        f"$ {' '.join(command)}\n\n--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}\n",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "")[-STDERR_TAIL_CHARS:]
        raise OpenICCError(
            f"OpenICC calibrate_camera failed (exit {completed.returncode}): {tail}"
        )

    result_path = _result_json_path(work_dir)
    try:
        return json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenICCError(f"Could not parse OpenICC result {result_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Result mapping
# ---------------------------------------------------------------------------


def intrinsics_to_camera_matrix(intrinsics: Mapping[str, Any]) -> list[list[float]]:
    # Theia/OpenCameraCalibrator convention: fy = focal_length * aspect_ratio.
    focal = float(intrinsics["focal_length"])
    aspect = float(intrinsics.get("aspect_ratio", 1.0))
    skew = float(intrinsics.get("skew", 0.0))
    cx = float(intrinsics["principal_pt_x"])
    cy = float(intrinsics["principal_pt_y"])
    return [[focal, skew, cx], [0.0, focal * aspect, cy], [0.0, 0.0, 1.0]]


def _require_finite(result: Mapping[str, Any]) -> None:
    intrinsics = result.get("intrinsics")
    if not isinstance(intrinsics, Mapping):
        raise OpenICCError("OpenICC result has no 'intrinsics' object")
    values = [result.get("final_reproj_error")] + [
        intrinsics.get(key)
        for key in ("focal_length", "principal_pt_x", "principal_pt_y", "xi", "alpha")
    ]
    for value in values:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise OpenICCError(
                "OpenICC did not converge (non-finite calibration result); "
                "improve board coverage and retry"
            )


def run_double_sphere_model(
    *,
    output_dir: Path,
    camera: CameraConfig,
    board_config: BoardConfig,
    image_size: tuple[int, int],
    records: list[DetectionRecord],
    obj_by_id: dict[int, np.ndarray],
    settings: OpenICCSettings | None = None,
) -> dict[str, Any]:
    settings = settings or settings_from_env()
    work_dir = output_dir / "openicc"
    downscale = max(1.0, image_size[1] / MAX_SOLVE_HEIGHT)
    payload = build_corners_payload(
        records=records,
        obj_by_id=obj_by_id,
        image_size=image_size,
        square_size_m=board_config.square_m,
        fps=camera.fps,
        downscale=downscale,
    )
    write_corners_file(work_dir / "corners.uson", payload)

    raw = run_calibrate_camera(work_dir, settings)
    _require_finite(raw)

    # Scale intrinsics back to the native resolution (xi/alpha are scale-free).
    intrinsics = dict(raw["intrinsics"])
    for key in ("focal_length", "principal_pt_x", "principal_pt_y"):
        intrinsics[key] = float(intrinsics[key]) * downscale
    raw = {
        **raw,
        "intrinsics": intrinsics,
        # error reported at native pixel scale so it compares fairly with the
        # cv2 models' errors in the summary/UI
        "final_reproj_error": float(raw["final_reproj_error"]) * downscale,
        "image_width": int(image_size[0]),
        "image_height": int(image_size[1]),
        "solve_downsample_factor": downscale,
    }
    artifact_path = output_dir / f"{camera.camera_name}_double_sphere.json"
    artifact_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    error = float(raw["final_reproj_error"])
    camera_matrix = intrinsics_to_camera_matrix(intrinsics)
    distortion = [float(intrinsics["xi"]), float(intrinsics["alpha"])]
    views_used = int(raw.get("nr_calib_images") or 0)
    selected = {
        "frame_count": views_used,
        "rms": error,
        "median_view_error_px": error,
        "worst_view_error_px": None,
        "camera_matrix": camera_matrix,
        "distortion": distortion,
        "yaml": None,
    }
    return {
        "model": "double_sphere",
        "ok": True,
        "rms": error,
        "median_view_error_px": error,
        "worst_view_error_px": None,
        # OpenICC reports one aggregate reprojection error, not per-view medians.
        "error_metric": "openicc_final_reproj_error_px",
        "camera_matrix": camera_matrix,
        "distortion": distortion,
        "distortion_model": "double_sphere",
        "yaml": None,
        "json": str(artifact_path),
        "all_frames": {"frame_count": len(payload["views"])},
        "selected": selected,
        "diagnostics_csv": None,
        "openicc": raw,
    }
