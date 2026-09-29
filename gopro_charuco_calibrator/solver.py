from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import cv2.aruco as aruco
import numpy as np

from .boards import detector_params, make_caib_board, resolve_dictionary
from .coverage import COVERAGE_FIELDS, PoseParams, coverage_summary
from .detection import detect_markers
from .models import BoardConfig, CameraConfig, CoverageTargets, SolverConfig
from .openicc import run_double_sphere_model
from .ros_yaml import save_camera_info_yaml

ROBUST_SIGMA_FLOOR_PX = 0.05
# Smallest frame set we will keep when cv2.fisheye keeps rejecting ill-conditioned
# views; below this we stop dropping and solve without the conditioning check.
FISHEYE_MIN_KEEP_FRAMES = 6


@dataclass(frozen=True)
class CalibModelSpec:
    """A camera model the solver can fit and emit.

    ``name`` is the file/summary label; ``distortion_model`` is the ROS
    distortion_model string written into the camera_info YAML (fisheye uses the
    canonical ``equidistant``). ``flags`` only applies to the pinhole path.
    """

    name: str
    fisheye: bool
    flags: int
    distortion_model: str


CALIB_MODELS: dict[str, CalibModelSpec] = {
    "plumb_bob": CalibModelSpec("plumb_bob", False, 0, "plumb_bob"),
    "rational_polynomial": CalibModelSpec(
        "rational_polynomial", False, cv2.CALIB_RATIONAL_MODEL, "rational_polynomial"
    ),
    "fisheye": CalibModelSpec("fisheye", True, 0, "equidistant"),
}

_ILL_COND_RE = re.compile(r"input array (\d+)")


@dataclass(frozen=True)
class DetectionRecord:
    name: str
    corners: list[np.ndarray]
    ids: np.ndarray
    marker_count: int
    pose: PoseParams


@dataclass(frozen=True)
class CalibrationSolve:
    rms: float
    camera_matrix: np.ndarray
    dist_coeffs: np.ndarray
    rvecs: tuple[np.ndarray, ...]
    tvecs: tuple[np.ndarray, ...]
    view_errors_px: dict[str, float]


@dataclass(frozen=True)
class SelectionResult:
    threshold_px: float
    median_px: float
    robust_sigma_px: float
    selected_names: list[str]
    selected_reasons: dict[str, str]
    rejected_reasons: dict[str, str]
    fallback_min_frames: bool
    capped_max_frames: bool
    input_frame_count: int
    residual_candidate_count: int
    effective_min_selected_frames: int
    effective_max_selected_frames: int


def detect_frames(
    *,
    frames_dir: Path,
    board_config: BoardConfig,
    min_markers: int,
    glob_pattern: str = "capture_*.jpg",
) -> tuple[tuple[int, int], list[DetectionRecord]]:
    dictionary = resolve_dictionary(board_config.aruco_dict)
    params = detector_params()
    records: list[DetectionRecord] = []
    image_size: tuple[int, int] | None = None
    for frame_path in sorted(frames_dir.glob(glob_pattern)):
        image = cv2.imread(str(frame_path))
        if image is None:
            continue
        image_size = (image.shape[1], image.shape[0])
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        detection = detect_markers(gray, image_size, board_config, dictionary, params)
        if detection is None or detection.marker_count < min_markers:
            continue
        records.append(
            DetectionRecord(
                name=frame_path.name,
                corners=detection.corners,
                ids=detection.ids,
                marker_count=detection.marker_count,
                pose=detection.pose,
            )
        )
    if image_size is None:
        raise RuntimeError(f"No readable frames found in {frames_dir}")
    return image_size, records


def flatten_records(
    records: list[DetectionRecord],
) -> tuple[list[np.ndarray], np.ndarray, np.ndarray]:
    all_corners: list[np.ndarray] = []
    all_ids: list[np.ndarray] = []
    counter: list[int] = []
    for record in records:
        all_corners.extend(record.corners)
        all_ids.extend(record.ids.reshape(-1, 1))
        counter.append(len(record.corners))
    return (
        all_corners,
        np.asarray(all_ids, dtype=np.int32),
        np.asarray(counter, dtype=np.int32),
    )


def view_errors(
    records: list[DetectionRecord],
    obj_by_id: dict[int, np.ndarray],
    camera_matrix: np.ndarray,
    dist_coeffs: np.ndarray,
    rvecs: tuple[np.ndarray, ...],
    tvecs: tuple[np.ndarray, ...],
) -> dict[str, float]:
    errors: dict[str, float] = {}
    for record, rvec, tvec in zip(records, rvecs, tvecs, strict=True):
        object_points = []
        image_points = []
        for corner, marker_id in zip(record.corners, record.ids.ravel(), strict=True):
            object_points.append(obj_by_id[int(marker_id)])
            image_points.append(corner.reshape(4, 2))
        obj = np.concatenate(object_points, axis=0).astype(np.float32)
        img = np.concatenate(image_points, axis=0).astype(np.float32)
        projected, _jacobian = cv2.projectPoints(obj, rvec, tvec, camera_matrix, dist_coeffs)
        diff = img - projected.reshape(-1, 2)
        errors[record.name] = float(np.sqrt(np.mean(np.sum(diff * diff, axis=1))))
    return errors


def calibration_solve(
    image_size: tuple[int, int],
    records: list[DetectionRecord],
    board,
    obj_by_id: dict[int, np.ndarray],
    flags: int,
) -> CalibrationSolve:
    corners, ids, counter = flatten_records(records)
    rms, camera_matrix, dist_coeffs, rvecs, tvecs = aruco.calibrateCameraAruco(
        corners,
        ids,
        counter,
        board,
        image_size,
        None,
        None,
        flags=flags,
    )
    rvec_tuple = tuple(np.asarray(rvec) for rvec in rvecs)
    tvec_tuple = tuple(np.asarray(tvec) for tvec in tvecs)
    errors = view_errors(records, obj_by_id, camera_matrix, dist_coeffs, rvec_tuple, tvec_tuple)
    return CalibrationSolve(
        rms=float(rms),
        camera_matrix=np.asarray(camera_matrix, dtype=np.float64),
        dist_coeffs=np.asarray(dist_coeffs, dtype=np.float64),
        rvecs=rvec_tuple,
        tvecs=tvec_tuple,
        view_errors_px=errors,
    )


def _fisheye_view_points(
    records: list[DetectionRecord],
    obj_by_id: dict[int, np.ndarray],
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Build per-view (N,1,3) object and (N,1,2) image point arrays.

    cv2.fisheye is strict about the channel layout: object points must be a
    3-channel Nx1 array and image points a 2-channel Nx1 array, both float64.
    """
    object_points: list[np.ndarray] = []
    image_points: list[np.ndarray] = []
    for record in records:
        objp: list[np.ndarray] = []
        imgp: list[np.ndarray] = []
        for corner, marker_id in zip(record.corners, record.ids.ravel(), strict=True):
            objp.append(obj_by_id[int(marker_id)])
            imgp.append(np.asarray(corner, dtype=np.float64).reshape(4, 2))
        obj = np.concatenate(objp, axis=0).astype(np.float64).reshape(-1, 1, 3)
        img = np.concatenate(imgp, axis=0).astype(np.float64).reshape(-1, 1, 2)
        object_points.append(obj)
        image_points.append(img)
    return object_points, image_points


def fisheye_solve(
    image_size: tuple[int, int],
    records: list[DetectionRecord],
    obj_by_id: dict[int, np.ndarray],
) -> CalibrationSolve:
    """Calibrate the Kannala-Brandt fisheye model (cv2.fisheye).

    cv2.fisheye is numerically fragile: CALIB_CHECK_COND raises on an
    ill-conditioned (typically extreme-edge) view, and the documented cure is to
    drop that view and retry rather than disable the check. We iteratively drop
    offending frames (tagging them with an infinite view error so the existing
    selection logic rejects them) and only fall back to solving without the
    conditioning check if too few frames would remain.
    """
    object_points, image_points = _fisheye_view_points(records, obj_by_id)
    flags = (
        cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC
        | cv2.fisheye.CALIB_FIX_SKEW
        | cv2.fisheye.CALIB_CHECK_COND
    )
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-6)
    point_counts = [int(points.shape[0]) for points in object_points]

    kept = list(range(len(records)))
    dropped: list[int] = []
    while True:
        objp = [object_points[i] for i in kept]
        imgp = [image_points[i] for i in kept]
        try:
            rms, camera_matrix, dist, rvecs, tvecs = cv2.fisheye.calibrate(
                objp,
                imgp,
                tuple(image_size),
                np.eye(3, dtype=np.float64),
                np.zeros((4, 1), dtype=np.float64),
                flags=flags,
                criteria=criteria,
            )
            break
        except cv2.error as exc:
            if len(kept) <= FISHEYE_MIN_KEEP_FRAMES:
                raise RuntimeError(
                    "Fisheye (Kannala-Brandt) calibration could not converge on these views. "
                    "This is expected for SuperView/HyperView captures, whose anamorphic stretch "
                    "is not a radially-symmetric fisheye projection - use the Wide lens for the "
                    "fisheye model, or the pinhole model for Linear."
                ) from exc
            match = _ILL_COND_RE.search(str(exc))
            if match is not None and 0 <= int(match.group(1)) < len(kept):
                # OpenCV named the ill-conditioned view by its position in kept.
                dropped.append(kept.pop(int(match.group(1))))
            else:
                # No index (e.g. InitExtrinsics norm_u1 == 0). Drop the kept view
                # with the fewest correspondences - the likeliest degenerate one.
                victim = min(kept, key=lambda index: point_counts[index])
                kept.remove(victim)
                dropped.append(victim)

    errors: dict[str, float] = {}
    for position, index in enumerate(kept):
        projected, _ = cv2.fisheye.projectPoints(
            object_points[index], rvecs[position], tvecs[position], camera_matrix, dist
        )
        diff = image_points[index].reshape(-1, 2) - projected.reshape(-1, 2)
        errors[records[index].name] = float(np.sqrt(np.mean(np.sum(diff * diff, axis=1))))
    for index in dropped:
        errors[records[index].name] = float("inf")

    return CalibrationSolve(
        rms=float(rms),
        camera_matrix=np.asarray(camera_matrix, dtype=np.float64),
        dist_coeffs=np.asarray(dist, dtype=np.float64).ravel(),
        rvecs=tuple(np.asarray(rvec) for rvec in rvecs),
        tvecs=tuple(np.asarray(tvec) for tvec in tvecs),
        view_errors_px=errors,
    )


def solve_model(
    model: CalibModelSpec,
    image_size: tuple[int, int],
    records: list[DetectionRecord],
    board,
    obj_by_id: dict[int, np.ndarray],
) -> CalibrationSolve:
    if model.fisheye:
        return fisheye_solve(image_size, records, obj_by_id)
    return calibration_solve(image_size, records, board, obj_by_id, model.flags)


def robust_threshold(errors: list[float], max_view_error_px: float, mad_multiplier: float):
    values = np.asarray(errors, dtype=np.float64)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return float(max_view_error_px), float("inf"), float("inf")
    median = float(np.median(finite))
    mad = float(np.median(np.abs(finite - median)))
    robust_sigma = max(1.4826 * mad, ROBUST_SIGMA_FLOOR_PX)
    threshold = min(float(max_view_error_px), median + float(mad_multiplier) * robust_sigma)
    return threshold, median, robust_sigma


def normalized_coverage(
    records: list[DetectionRecord],
    indices: list[int],
) -> dict[int, np.ndarray]:
    matrix = np.asarray(
        [[getattr(records[index].pose, field) for field in COVERAGE_FIELDS] for index in indices],
        dtype=np.float64,
    )
    mins = np.min(matrix, axis=0)
    spans = np.max(matrix, axis=0) - mins
    spans[spans < 1e-9] = 1.0
    normalized = (matrix - mins) / spans
    return {index: normalized[row] for row, index in enumerate(indices)}


def _add_reason(reasons: dict[int, str], index: int, reason: str) -> None:
    if index in reasons:
        parts = reasons[index].split(",")
        if reason not in parts:
            reasons[index] = f"{reasons[index]},{reason}"
    else:
        reasons[index] = reason


def coverage_preserving_subset(
    records: list[DetectionRecord],
    candidate_indices: list[int],
    errors_by_name: dict[str, float],
    max_count: int,
) -> tuple[set[int], dict[int, str]]:
    if len(candidate_indices) <= max_count:
        return set(candidate_indices), {
            index: "below_residual_threshold" for index in candidate_indices
        }

    selected: set[int] = set()
    selected_reasons: dict[int, str] = {}
    normalized = normalized_coverage(records, candidate_indices)

    def add(index: int, reason: str) -> None:
        if len(selected) >= max_count and index not in selected:
            return
        selected.add(index)
        _add_reason(selected_reasons, index, reason)

    for col, field in enumerate(COVERAGE_FIELDS):
        min_index = min(
            candidate_indices,
            key=lambda index: (normalized[index][col], errors_by_name[records[index].name]),
        )
        max_index = max(
            candidate_indices,
            key=lambda index: (normalized[index][col], -errors_by_name[records[index].name]),
        )
        add(min_index, f"coverage_extreme:{field}_min")
        add(max_index, f"coverage_extreme:{field}_max")
        if len(selected) >= max_count:
            break

    if not selected:
        lowest_error = min(candidate_indices, key=lambda index: errors_by_name[records[index].name])
        add(lowest_error, "lowest_error_seed")

    while len(selected) < max_count:
        remaining = [index for index in candidate_indices if index not in selected]
        if not remaining:
            break

        def novelty(index: int) -> tuple[float, float, int]:
            min_distance = min(
                float(np.linalg.norm(normalized[index] - normalized[other])) for other in selected
            )
            return min_distance, -errors_by_name[records[index].name], records[index].marker_count

        add(max(remaining, key=novelty), "pose_novelty")

    return selected, selected_reasons


def select_frame_subset(
    records: list[DetectionRecord],
    errors_by_name: dict[str, float],
    *,
    max_view_error_px: float,
    outlier_mad_multiplier: float,
    min_selected_frames: int,
    max_selected_frames: int,
) -> SelectionResult:
    if not records:
        raise ValueError("No frame records supplied for selection")

    errors = [errors_by_name[record.name] for record in records]
    threshold, median, robust_sigma = robust_threshold(
        errors,
        max_view_error_px,
        outlier_mad_multiplier,
    )
    effective_min = min(max(int(min_selected_frames), 1), len(records))
    effective_max = min(max(int(max_selected_frames), effective_min), len(records))

    candidate_indices = [
        index
        for index, record in enumerate(records)
        if errors_by_name[record.name] <= threshold
    ]
    fallback_min_frames = False
    capped_max_frames = False

    if len(candidate_indices) < effective_min:
        fallback_min_frames = True
        selected_indices = set(
            sorted(
                range(len(records)),
                key=lambda index: (
                    errors_by_name[records[index].name],
                    -records[index].marker_count,
                ),
            )[:effective_min]
        )
        selected_reasons_by_index = {
            index: "minimum_frame_fallback" for index in selected_indices
        }
    else:
        selected_indices = set(candidate_indices)
        selected_reasons_by_index = {
            index: "below_residual_threshold" for index in selected_indices
        }

    if len(selected_indices) > effective_max:
        capped_max_frames = True
        selected_indices, selected_reasons_by_index = coverage_preserving_subset(
            records,
            [index for index in range(len(records)) if index in selected_indices],
            errors_by_name,
            effective_max,
        )

    selected_names = [
        record.name for index, record in enumerate(records) if index in selected_indices
    ]
    selected_reasons = {
        records[index].name: selected_reasons_by_index.get(index, "selected")
        for index in selected_indices
    }
    rejected_reasons: dict[str, str] = {}
    for index, record in enumerate(records):
        if index in selected_indices:
            continue
        error = errors_by_name[record.name]
        if error > threshold:
            rejected_reasons[record.name] = (
                f"view_error_px {error:.3f} > threshold_px {threshold:.3f}"
            )
        else:
            rejected_reasons[record.name] = "over_max_selected_frames"

    return SelectionResult(
        threshold_px=float(threshold),
        median_px=float(median),
        robust_sigma_px=float(robust_sigma),
        selected_names=selected_names,
        selected_reasons=selected_reasons,
        rejected_reasons=rejected_reasons,
        fallback_min_frames=fallback_min_frames,
        capped_max_frames=capped_max_frames,
        input_frame_count=len(records),
        residual_candidate_count=len(candidate_indices),
        effective_min_selected_frames=effective_min,
        effective_max_selected_frames=effective_max,
    )


def solve_summary(solve: CalibrationSolve, records: list[DetectionRecord], yaml_path: Path) -> dict:
    errors = [solve.view_errors_px[record.name] for record in records]
    # Fisheye marks conditioning-dropped views with inf; exclude them from the
    # aggregate stats and report them as null per-frame so the JSON stays valid.
    finite = [error for error in errors if math.isfinite(error)]
    return {
        "frame_count": len(records),
        "rms": float(solve.rms),
        "median_view_error_px": float(np.median(finite)) if finite else None,
        "worst_view_error_px": float(np.max(finite)) if finite else None,
        "camera_matrix": solve.camera_matrix.tolist(),
        "distortion": solve.dist_coeffs.ravel().tolist(),
        "yaml": str(yaml_path),
        "view_errors_px": {
            record.name: (
                float(error) if math.isfinite(error := solve.view_errors_px[record.name]) else None
            )
            for record in records
        },
    }


def json_error_value(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def selection_summary(selection: SelectionResult, pass_index: int) -> dict:
    return {
        "pass": pass_index,
        "input_frame_count": selection.input_frame_count,
        "residual_candidate_count": selection.residual_candidate_count,
        "selected_frame_count": len(selection.selected_names),
        "threshold_px": selection.threshold_px,
        "median_px": selection.median_px,
        "robust_sigma_px": selection.robust_sigma_px,
        "fallback_min_frames": selection.fallback_min_frames,
        "capped_max_frames": selection.capped_max_frames,
        "effective_min_selected_frames": selection.effective_min_selected_frames,
        "effective_max_selected_frames": selection.effective_max_selected_frames,
        "selected_frames": selection.selected_names,
        "rejected_frames": [
            {"name": name, "reason": reason}
            for name, reason in sorted(selection.rejected_reasons.items())
        ],
    }


def frame_summary(records: list[DetectionRecord]) -> list[dict[str, Any]]:
    return [
        {
            "name": record.name,
            "marker_count": record.marker_count,
            **record.pose.as_dict(),
        }
        for record in records
    ]


def write_diagnostics_csv(
    path: Path,
    records: list[DetectionRecord],
    all_solve: CalibrationSolve,
    selected_solve: CalibrationSolve,
    selected_names: set[str],
    frame_reasons: dict[str, str],
) -> None:
    selected_errors = selected_solve.view_errors_px
    fieldnames = [
        "frame",
        "marker_count",
        "x",
        "y",
        "size",
        "skew",
        "all_frame_error_px",
        "final_error_px",
        "selected",
        "reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "frame": record.name,
                    "marker_count": record.marker_count,
                    "x": f"{record.pose.x:.6f}",
                    "y": f"{record.pose.y:.6f}",
                    "size": f"{record.pose.size:.6f}",
                    "skew": f"{record.pose.skew:.6f}",
                    "all_frame_error_px": f"{all_solve.view_errors_px[record.name]:.6f}",
                    "final_error_px": (
                        f"{selected_errors[record.name]:.6f}"
                        if record.name in selected_errors
                        else ""
                    ),
                    "selected": "true" if record.name in selected_names else "false",
                    "reason": frame_reasons.get(record.name, ""),
                }
            )


def run_model(
    *,
    output_dir: Path,
    camera: CameraConfig,
    solver: SolverConfig,
    image_size: tuple[int, int],
    records: list[DetectionRecord],
    board,
    obj_by_id: dict[int, np.ndarray],
    model: CalibModelSpec,
) -> dict[str, Any]:
    model_name = model.name
    all_solve = solve_model(model, image_size, records, board, obj_by_id)
    all_yaml_path = output_dir / f"{camera.camera_name}_all_frames_{model_name}.yaml"
    save_camera_info_yaml(
        all_yaml_path,
        camera_name=camera.camera_name,
        image_size=image_size,
        camera_matrix=all_solve.camera_matrix,
        dist_coeffs=all_solve.dist_coeffs,
        distortion_model=model.distortion_model,
        rectify_alpha=solver.rectify_alpha,
        fisheye=model.fisheye,
    )

    frame_reasons: dict[str, str] = {record.name: "auto_select_disabled" for record in records}
    selection_passes: list[dict[str, Any]] = []
    selected_records = records
    selected_solve = all_solve

    if solver.auto_select:
        frame_reasons = {}
        current_records = records
        current_solve = all_solve
        min_selected = solver.min_selected_frames or solver.min_frames
        for pass_index in range(1, solver.selection_passes + 1):
            selection = select_frame_subset(
                current_records,
                current_solve.view_errors_px,
                max_view_error_px=solver.max_view_error_px,
                outlier_mad_multiplier=solver.outlier_mad_multiplier,
                min_selected_frames=min_selected,
                max_selected_frames=solver.max_selected_frames,
            )
            selection_passes.append(selection_summary(selection, pass_index))
            frame_reasons.update(selection.selected_reasons)
            frame_reasons.update(selection.rejected_reasons)

            selected_name_set = set(selection.selected_names)
            next_records = [
                record for record in current_records if record.name in selected_name_set
            ]
            changed = len(next_records) != len(current_records) or any(
                left.name != right.name
                for left, right in zip(next_records, current_records, strict=False)
            )
            selected_records = next_records
            if changed:
                selected_solve = solve_model(
                    model,
                    image_size,
                    selected_records,
                    board,
                    obj_by_id,
                )
                current_records = selected_records
                current_solve = selected_solve
            else:
                selected_solve = current_solve
                break

        for record in records:
            frame_reasons.setdefault(record.name, "selected")

    selected_yaml_path = output_dir / f"{camera.camera_name}_{model_name}.yaml"
    save_camera_info_yaml(
        selected_yaml_path,
        camera_name=camera.camera_name,
        image_size=image_size,
        camera_matrix=selected_solve.camera_matrix,
        dist_coeffs=selected_solve.dist_coeffs,
        distortion_model=model.distortion_model,
        rectify_alpha=solver.rectify_alpha,
        fisheye=model.fisheye,
    )

    selected_names = {record.name for record in selected_records}
    diagnostics_csv_path = output_dir / f"{camera.camera_name}_{model_name}_frame_diagnostics.csv"
    write_diagnostics_csv(
        diagnostics_csv_path,
        records,
        all_solve,
        selected_solve,
        selected_names,
        frame_reasons,
    )

    all_summary = solve_summary(all_solve, records, all_yaml_path)
    selected_summary = solve_summary(selected_solve, selected_records, selected_yaml_path)
    rejected = [
        {
            "name": record.name,
            "reason": frame_reasons.get(record.name, ""),
            "all_view_error_px": json_error_value(all_solve.view_errors_px[record.name]),
        }
        for record in records
        if record.name not in selected_names
    ]
    selected_summary.update(
        {
            "frames": [record.name for record in selected_records],
            "rejected_frames": rejected,
            "selection_passes": selection_passes,
        }
    )

    return {
        "model": model_name,
        "ok": True,
        "rms": selected_summary["rms"],
        "median_view_error_px": selected_summary["median_view_error_px"],
        "worst_view_error_px": selected_summary["worst_view_error_px"],
        "camera_matrix": selected_summary["camera_matrix"],
        "distortion": selected_summary["distortion"],
        "yaml": str(selected_yaml_path),
        "all_frames": all_summary,
        "selected": selected_summary,
        "diagnostics_csv": str(diagnostics_csv_path),
    }


def failed_model_result(model_name: str, exc: Exception) -> dict[str, Any]:
    return {
        "model": model_name,
        "ok": False,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "rms": None,
        "median_view_error_px": None,
        "worst_view_error_px": None,
        "camera_matrix": None,
        "distortion": None,
        "yaml": None,
        "all_frames": None,
        "selected": None,
        "diagnostics_csv": None,
    }


def _layout_residual_px(records: list[DetectionRecord], obj_by_id: dict[int, np.ndarray]) -> float:
    """Median per-corner homography residual of the layout against detections.

    A planar homography absorbs pose but not lens distortion, so residuals stay
    small (a few px at the board scale) when the marker layout matches reality
    and jump by roughly a square size when it does not.
    """
    residuals: list[float] = []
    candidates = sorted(records, key=lambda record: -record.marker_count)[:5]
    for record in candidates:
        object_xy = []
        image_xy = []
        for corner, marker_id in zip(record.corners, record.ids.ravel(), strict=True):
            if int(marker_id) not in obj_by_id:
                continue
            object_xy.append(obj_by_id[int(marker_id)][:, :2])
            image_xy.append(np.asarray(corner, dtype=np.float64).reshape(4, 2))
        if len(object_xy) < 4:
            continue
        obj = np.concatenate(object_xy, axis=0)
        img = np.concatenate(image_xy, axis=0)
        homography, _mask = cv2.findHomography(obj, img, cv2.RANSAC, 5.0)
        if homography is None:
            continue
        projected = cv2.perspectiveTransform(obj.reshape(-1, 1, 2), homography).reshape(-1, 2)
        residuals.extend(np.linalg.norm(projected - img, axis=1).tolist())
    return float(np.median(residuals)) if residuals else float("inf")


def detect_board_layout(
    records: list[DetectionRecord],
    board_config: BoardConfig,
    dictionary=None,
):
    """Pick the caib.io marker-column parity that matches the detections.

    caib.io flips which checkerboard cells carry markers depending on the board
    dimensions, so the same cols/rows/ids config can describe two mirrored
    layouts. Assuming the wrong one silently offsets every odd-row marker by a
    full square and ruins calibration. Score both against the detections and
    keep the better fit.
    """
    standard = make_caib_board(board_config, dictionary, flipped=False)
    flipped = make_caib_board(board_config, dictionary, flipped=True)
    standard_residual = _layout_residual_px(records, standard[1])
    flipped_residual = _layout_residual_px(records, flipped[1])
    if flipped_residual < standard_residual:
        return flipped[0], flipped[1], "flipped"
    return standard[0], standard[1], "standard"


def recommended_model(results: list[dict[str, Any]]) -> str | None:
    """Model the operator should use, or None when nothing solved.

    double_sphere wins whenever it solved: it is the only model valid over the
    whole ultra-wide image. Comparing error numbers across models is biased here,
    because the cv2 models' auto-select drops the hard edge views (their median
    then looks better) while OpenICC reports one aggregate error. Otherwise the
    lowest median view error wins, falling back to RMS.
    """
    solved = [
        result
        for result in results
        if result.get("ok") is not False
        and (result.get("median_view_error_px") is not None or result.get("rms") is not None)
    ]
    if not solved:
        return None
    if any(result.get("model") == "double_sphere" for result in solved):
        return "double_sphere"
    best = min(
        solved,
        key=lambda r: (
            r["median_view_error_px"] if r.get("median_view_error_px") is not None else r["rms"]
        ),
    )
    return best.get("model")


def solve_from_frames(
    *,
    frames_dir: Path,
    output_dir: Path,
    camera: CameraConfig,
    board_config: BoardConfig,
    solver: SolverConfig,
    coverage_targets: CoverageTargets | None = None,
    glob_pattern: str = "capture_*.jpg",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    dictionary = resolve_dictionary(board_config.aruco_dict)
    image_size, records = detect_frames(
        frames_dir=frames_dir,
        board_config=board_config,
        min_markers=solver.min_markers,
        glob_pattern=glob_pattern,
    )
    min_required = int(solver.min_frames)
    if len(records) < min_required:
        raise RuntimeError(f"Need at least {min_required} usable frames, found {len(records)}")
    board, obj_by_id, board_layout = detect_board_layout(records, board_config, dictionary)

    results = []
    for model_name in solver.models:
        try:
            if model_name == "double_sphere":
                # External OpenICC backend (subprocess boundary); it does its own
                # view selection, so the cv2 solve + auto-select path is bypassed.
                results.append(
                    run_double_sphere_model(
                        output_dir=output_dir,
                        camera=camera,
                        board_config=board_config,
                        image_size=image_size,
                        records=records,
                        obj_by_id=obj_by_id,
                    )
                )
            else:
                results.append(
                    run_model(
                        output_dir=output_dir,
                        camera=camera,
                        solver=solver,
                        image_size=image_size,
                        records=records,
                        board=board,
                        obj_by_id=obj_by_id,
                        model=CALIB_MODELS[model_name],
                    )
                )
        except (cv2.error, RuntimeError, ValueError, np.linalg.LinAlgError) as exc:
            results.append(failed_model_result(model_name, exc))
    recommended = recommended_model(results)
    for result in results:
        result["recommended"] = result.get("model") == recommended
    summary = {
        "image_size": list(image_size),
        "frames": frame_summary(records),
        "coverage": coverage_summary([record.pose for record in records], coverage_targets),
        "board": {
            **board_config.model_dump(),
            "pattern_width_m": board_config.pattern_width_m,
            "pattern_height_m": board_config.pattern_height_m,
            "layout": board_layout,
        },
        "camera": camera.model_dump(),
        "selection": solver.model_dump(),
        "recommended_model": recommended,
        "results": results,
    }
    summary_path = output_dir / "caib_marker_board_calibration_summary.json"
    with summary_path.open("w", encoding="utf-8") as stream:
        json.dump(summary, stream, indent=2)
    return summary
