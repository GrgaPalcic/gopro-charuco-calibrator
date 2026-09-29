"""Double Sphere and Kannala-Brandt calibration through the external OpenImuCameraCalibrator.

OpenICC (github.com/urbste/OpenImuCameraCalibrator) is AGPL-3.0, so it is kept at
arm's length: we exchange files with its ``calibrate_camera`` binary over a
subprocess boundary (a Docker image by default, or a native build via
``OPENICC_BINARY``). Nothing from OpenICC is imported, linked, or vendored.

The binary consumes a UBJSON "corners" file of arbitrary 3D<->2D correspondences
(no board-structure assumptions), so we feed it our own ArUco detections
directly: scene point ids are ``marker_id * 4 + corner_index`` and views are
keyed by synthetic microsecond timestamps. Backend knobs come from environment
variables so presets stay machine-portable:

- ``OPENICC_DOCKER_IMAGE`` (default ``gopro-charuco-openicc:d75dda5-p1``, built by setup-openicc)
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
# The image name carries the commit and the patch level, so an image built before a
# patch was added is not silently reused: its absence triggers the "run
# setup-openicc" message instead.
DEFAULT_DOCKER_IMAGE = "gopro-charuco-openicc:d75dda5-p1"
DEFAULT_GRID_SIZE = 0.1
DEFAULT_TIMEOUT_S = 360.0
CALIBRATE_CAMERA_BIN = "/OpenImuCameraCalibrator/build/applications/calibrate_camera"
STDERR_TAIL_CHARS = 2000
# Pinned so every machine builds the same calibrate_camera (verified with this
# integration on 2026-09-29); bump deliberately, not by rebuilding HEAD.
OPENICC_REPO = "https://github.com/urbste/OpenImuCameraCalibrator"
OPENICC_COMMIT = "d75dda57285c6c1fda0f41e5f4ad901483f73fff"
DEFAULT_SOURCE_DIR = Path("~/.cache/gopro-charuco-calibrator/openicc")
# Source patches applied to the pinned checkout before the image is built: (file, old,
# new). At d75dda5, calibrate_camera fits the distortion only in its first bundle
# adjustment, with the principal point held at the image centre. The later stages free
# the principal point, but the final one refines the distortion only for PINHOLE, so for
# DOUBLE_SPHERE and FISHEYE the distortion stays fitted around the wrong centre. The
# HERO13's principal point sits ~10 px off centre, which left both models 0.6-4.2 px off
# the true lens on synthetic Max Lens Mod data (5 runs per scene, 2026-09-29; patched:
# 0.1-0.5 px; the ranges are in tests/test_hero13_readiness.py). The patch lets the final
# adjustment refine the distortion for every model except PINHOLE_RADIAL_TANGENTIAL,
# which keeps its own tangential-only branch.
OPENICC_PATCHES = [
    (
        "src/core/camera_calibrator.cc",
        """      theia::OptimizeIntrinsicsType::ASPECT_RATIO;

  if (camera_model_ == "PINHOLE") {""",
        """      theia::OptimizeIntrinsicsType::ASPECT_RATIO;

  if (camera_model_ != "PINHOLE_RADIAL_TANGENTIAL") {""",
    ),
]

# calibrate_camera crashed with SIGSEGV (exit 139) once in ~165 runs, in the FISHEYE
# view initialisation (2026-09-29); the same corners solved fine on every other run, so
# a run that dies from a signal is run once more before it counts as a failure.
CRASH_RETRIES = 1


@dataclass(frozen=True)
class OpenICCModel:
    name: str  # our model name, as in SolverConfig.models
    flag: str  # OpenICC --camera_model_to_calibrate
    work_dir: str  # per-model folder under the run, so logs and results never collide
    params: tuple[str, ...]  # distortion keys in OpenICC's "intrinsics", in order


OPENICC_MODELS = {
    "double_sphere": OpenICCModel("double_sphere", "DOUBLE_SPHERE", "openicc", ("xi", "alpha")),
    # OpenICC's FISHEYE is Kannala-Brandt with k1..k4 on theta (cv2.fisheye's model),
    # and its JSON is exactly the layout of UMI's gopro_intrinsics_2_7k.json.
    "kannala_brandt": OpenICCModel(
        "kannala_brandt",
        "FISHEYE",
        "openicc_kannala_brandt",
        tuple(f"radial_distortion_{i}" for i in range(1, 5)),
    ),
}


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
    model_name: str = "double_sphere",
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
            f"OpenICC {model_name} needs at least {OPENICC_MIN_VIEWS} views with "
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


def apply_source_patches(source_dir: Path) -> None:
    """Apply OPENICC_PATCHES to a checkout; idempotent, and loud if the source moved."""
    for relative, old, new in OPENICC_PATCHES:
        path = source_dir / relative
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise OpenICCError(
                f"Cannot patch {path}: the file is missing. The checkout is not OpenICC "
                f"{OPENICC_COMMIT[:7]}; delete {source_dir} and run setup-openicc."
            ) from exc
        if new in text:
            continue
        if text.count(old) != 1:
            raise OpenICCError(
                f"Cannot patch {path}: the expected code is not there once. The checkout is "
                f"not OpenICC {OPENICC_COMMIT[:7]}; delete {source_dir} and run setup-openicc."
            )
        path.write_text(text.replace(old, new), encoding="utf-8")


def build_image_commands(source_dir: Path, image: str) -> list[list[str]]:
    """Commands that fetch the pinned OpenICC source and build its Docker image.

    ``build_image`` applies OPENICC_PATCHES between the checkout and ``docker build``.
    """
    src = str(source_dir)
    commands: list[list[str]] = []
    if not (source_dir / ".git").is_dir():
        commands += [
            ["git", "init", "-q", src],
            ["git", "-C", src, "remote", "add", "origin", OPENICC_REPO],
        ]
    commands += [
        ["git", "-C", src, "fetch", "-q", "--depth", "1", "origin", OPENICC_COMMIT],
        ["git", "-C", src, "checkout", "-q", "--force", "FETCH_HEAD"],
        ["docker", "build", "-t", image, src],
    ]
    return commands


def build_image(source_dir: Path | None = None, image: str | None = None) -> None:
    """Build the OpenICC Docker image the OpenICC models run in (~10 min)."""
    source_dir = (source_dir or DEFAULT_SOURCE_DIR).expanduser()
    image = image or settings_from_env().docker_image
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    commands = build_image_commands(source_dir, image)
    for command in commands[:-1]:
        print("$", " ".join(command), flush=True)
        subprocess.run(command, check=True)
    print(f"patching {source_dir} ({len(OPENICC_PATCHES)} patch)", flush=True)
    apply_source_patches(source_dir)
    print("$", " ".join(commands[-1]), flush=True)
    subprocess.run(commands[-1], check=True)


def _calibrate_flags(
    corners: str, out_prefix: str, grid_size: float, camera_model: str = "DOUBLE_SPHERE"
) -> list[str]:
    return [
        f"--input_corners={corners}",
        f"--camera_model_to_calibrate={camera_model}",
        f"--save_path_calib_dataset={out_prefix}",
        f"--grid_size={grid_size}",
        "--optimize_board_points=false",
        "--verbose=true",
        "--logtostderr=1",
    ]


def build_command(
    work_dir: Path,
    settings: OpenICCSettings,
    container_name: str | None,
    camera_model: str = "DOUBLE_SPHERE",
) -> list[str]:
    if settings.binary_path:
        return [settings.binary_path] + _calibrate_flags(
            str(work_dir / "corners.uson"), str(work_dir / "out"), settings.grid_size, camera_model
        )
    command = ["docker", "run", "--rm"]
    if container_name:
        command += ["--name", container_name]
    if settings.docker_run_as_user:
        command += ["--user", f"{os.getuid()}:{os.getgid()}"]
    command += ["-v", f"{work_dir.resolve()}:/data", settings.docker_image]
    command += [CALIBRATE_CAMERA_BIN] + _calibrate_flags(
        "/data/corners.uson", "/data/out", settings.grid_size, camera_model
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


def _died_from_signal(returncode: int) -> bool:
    # A native binary killed by a signal reports -signal; Docker reports 128 + signal
    # (139 = SIGSEGV). Docker's own failures are 125-127 and are not retried.
    return returncode < 0 or returncode >= 128


def _run_calibrate_camera_once(
    work_dir: Path, settings: OpenICCSettings, camera_model: str
) -> tuple[subprocess.CompletedProcess, list[str]]:
    container_name = None if settings.binary_path else f"openicc-{uuid.uuid4().hex[:12]}"
    command = build_command(work_dir, settings, container_name, camera_model)
    # A re-solve of the same run (auto-solve at route end, then Resume + Solve)
    # reuses work_dir; drop old results so a run that writes none cannot be
    # mistaken for a fresh solve.
    for stale in [*work_dir.glob("out*.json"), *work_dir.glob("out/*.json")]:
        stale.unlink(missing_ok=True)
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
    except OSError as exc:  # e.g. OPENICC_BINARY is not executable
        raise OpenICCError(f"Could not start '{command[0]}': {exc}") from exc
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
    return completed, command


def run_calibrate_camera(
    work_dir: Path, settings: OpenICCSettings, camera_model: str = "DOUBLE_SPHERE"
) -> dict[str, Any]:
    log_path = work_dir / "calibrate_camera.log"
    crash_logs = []
    for attempt in range(CRASH_RETRIES + 1):
        completed, command = _run_calibrate_camera_once(work_dir, settings, camera_model)
        log = (
            f"$ {' '.join(command)}\n\n--- stdout ---\n{completed.stdout}\n"
            f"--- stderr ---\n{completed.stderr}\n"
        )
        if not _died_from_signal(completed.returncode) or attempt == CRASH_RETRIES:
            break
        crash_logs.append(f"--- crashed (exit {completed.returncode}), run again ---\n{log}\n")
    log_path.write_text("".join(crash_logs) + log, encoding="utf-8")
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "")[-STDERR_TAIL_CHARS:]
        if not settings.binary_path:
            lowered = tail.lower()
            if "unable to find image" in lowered or "pull access denied" in lowered:
                raise OpenICCError(
                    f"Docker image '{settings.docker_image}' is not built on this machine. "
                    "Run `uv run gopro-charuco setup-openicc` once (about 10 minutes), "
                    "then solve again."
                )
            if "permission denied" in lowered and "docker" in lowered:
                raise OpenICCError(
                    "This user cannot talk to the Docker daemon. Add it to the docker "
                    "group (`sudo usermod -aG docker $USER`, then log out and back in) "
                    "or set OPENICC_BINARY to a native calibrate_camera build."
                )
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


def _require_finite(result: Mapping[str, Any], params: Sequence[str] = ("xi", "alpha")) -> None:
    intrinsics = result.get("intrinsics")
    if not isinstance(intrinsics, Mapping):
        raise OpenICCError("OpenICC result has no 'intrinsics' object")
    # aspect_ratio is optional (1.0 when absent), but a NaN one would reach the summary
    # json as a bare NaN, which the browser cannot parse.
    optional = ("aspect_ratio",) if "aspect_ratio" in intrinsics else ()
    values = [result.get("final_reproj_error")] + [
        intrinsics.get(key)
        for key in ("focal_length", "principal_pt_x", "principal_pt_y", *params, *optional)
    ]
    for value in values:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise OpenICCError(
                "OpenICC did not converge (non-finite calibration result); "
                "improve board coverage and retry"
            )


def orbslam3_kb8_yaml(raw: Mapping[str, Any]) -> str:
    """The camera block of an ORB-SLAM3 KannalaBrandt8 settings file, from OpenICC FISHEYE.

    UMI's SLAM settings live in a fixed file inside its Docker image; this block replaces
    its Camera1.* and Camera.* lines. fx = fy = focal_length, as UMI's own loader does.
    """
    intr = raw["intrinsics"]
    k = [float(intr[f"radial_distortion_{i}"]) for i in range(1, 5)]
    focal = float(intr["focal_length"])
    lines = [
        "# Camera block for an ORB-SLAM3 settings file (File.version 1.0), from OpenICC FISHEYE.",
        "# Merge into your existing settings: the IMU.* block is not calibrated here.",
        'Camera.type: "KannalaBrandt8"',
        f"Camera1.fx: {focal!r}",
        f"Camera1.fy: {focal!r}",
        f"Camera1.cx: {float(intr['principal_pt_x'])!r}",
        f"Camera1.cy: {float(intr['principal_pt_y'])!r}",
        *(f"Camera1.k{i}: {value!r}" for i, value in enumerate(k, start=1)),
        f"Camera.width: {int(raw['image_width'])}",
        f"Camera.height: {int(raw['image_height'])}",
    ]
    fps = raw.get("fps")
    if fps:
        lines.append(f"Camera.fps: {round(float(fps))}")
    return "\n".join(lines) + "\n"


def run_openicc_model(
    *,
    model: str,
    output_dir: Path,
    camera: CameraConfig,
    board_config: BoardConfig,
    image_size: tuple[int, int],
    records: list[DetectionRecord],
    obj_by_id: dict[int, np.ndarray],
    settings: OpenICCSettings | None = None,
) -> dict[str, Any]:
    spec = OPENICC_MODELS[model]
    settings = settings or settings_from_env()
    work_dir = output_dir / spec.work_dir
    downscale = max(1.0, image_size[1] / MAX_SOLVE_HEIGHT)
    payload = build_corners_payload(
        records=records,
        obj_by_id=obj_by_id,
        image_size=image_size,
        square_size_m=board_config.square_m,
        fps=camera.fps,
        downscale=downscale,
        model_name=model,
    )
    write_corners_file(work_dir / "corners.uson", payload)

    raw = run_calibrate_camera(work_dir, settings, spec.flag)
    _require_finite(raw, spec.params)

    # Scale intrinsics back to the native resolution (xi, alpha and k1..k4 are scale-free).
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
    artifact_path = output_dir / f"{camera.camera_name}_{model}.json"
    artifact_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    orbslam3_path = None
    if model == "kannala_brandt":
        orbslam3_path = output_dir / f"{camera.camera_name}_kannala_brandt_orbslam3.yaml"
        orbslam3_path.write_text(orbslam3_kb8_yaml(raw), encoding="utf-8")

    error = float(raw["final_reproj_error"])
    camera_matrix = intrinsics_to_camera_matrix(intrinsics)
    distortion = [float(intrinsics[key]) for key in spec.params]
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
        "model": model,
        "ok": True,
        "rms": error,
        "median_view_error_px": error,
        "worst_view_error_px": None,
        # OpenICC reports one aggregate reprojection error, not per-view medians.
        "error_metric": "openicc_final_reproj_error_px",
        "camera_matrix": camera_matrix,
        "distortion": distortion,
        "distortion_model": model,
        "yaml": None,
        "json": str(artifact_path),
        "orbslam3_yaml": None if orbslam3_path is None else str(orbslam3_path),
        "all_frames": {"frame_count": len(payload["views"])},
        "selected": selected,
        "diagnostics_csv": None,
        "openicc": raw,
    }


def run_double_sphere_model(**kwargs: Any) -> dict[str, Any]:
    return run_openicc_model(model="double_sphere", **kwargs)
