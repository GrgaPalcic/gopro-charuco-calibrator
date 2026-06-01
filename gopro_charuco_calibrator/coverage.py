from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .models import CoverageTargets

COVERAGE_FIELDS = ("x", "y", "size", "skew")


@dataclass(frozen=True)
class PoseParams:
    x: float
    y: float
    size: float
    skew: float

    def as_list(self) -> list[float]:
        return [self.x, self.y, self.size, self.skew]

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "size": self.size, "skew": self.skew}


def coverage_params(points: np.ndarray, image_size: tuple[int, int]) -> PoseParams:
    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    image_w, image_h = image_size
    min_xy = np.min(points, axis=0)
    max_xy = np.max(points, axis=0)
    center = 0.5 * (min_xy + max_xy)
    width = max(float(max_xy[0] - min_xy[0]), 0.0) / max(float(image_w), 1.0)
    height = max(float(max_xy[1] - min_xy[1]), 0.0) / max(float(image_h), 1.0)
    size = float(np.sqrt(width * height))
    x = float(center[0] / max(float(image_w), 1.0))
    y = float(center[1] / max(float(image_h), 1.0))

    if len(points) >= 2:
        cov = np.cov(points.T)
        denom = float(np.sqrt(max(cov[0, 0], 0.0) * max(cov[1, 1], 0.0)))
        skew = float(abs(cov[0, 1]) / denom) if denom > 1e-9 else 0.0
    else:
        skew = 0.0
    return PoseParams(
        x=float(np.clip(x, 0.0, 1.0)),
        y=float(np.clip(y, 0.0, 1.0)),
        size=float(np.clip(size, 0.0, 1.0)),
        skew=float(np.clip(skew, 0.0, 1.0)),
    )


def pose_distance(left: PoseParams, right: PoseParams) -> float:
    return float(sum(abs(a - b) for a, b in zip(left.as_list(), right.as_list(), strict=True)))


def _two_sided_progress(values: np.ndarray, low: float, high: float) -> dict[str, float | bool]:
    if values.size == 0:
        return {"min": 0.0, "max": 0.0, "low_hit": False, "high_hit": False, "progress": 0.0}
    mid = 0.5 * (low + high)
    seen_min = float(np.min(values))
    seen_max = float(np.max(values))
    low_progress = np.clip((mid - seen_min) / max(mid - low, 1e-9), 0.0, 1.0)
    high_progress = np.clip((seen_max - mid) / max(high - mid, 1e-9), 0.0, 1.0)
    return {
        "min": seen_min,
        "max": seen_max,
        "low_hit": bool(seen_min <= low),
        "high_hit": bool(seen_max >= high),
        "progress": float(0.5 * (low_progress + high_progress)),
    }


def coverage_summary(
    poses: list[PoseParams],
    targets: CoverageTargets | None = None,
) -> dict[str, object]:
    targets = targets or CoverageTargets()
    if not poses:
        empty_axis = {"min": 0.0, "max": 0.0, "low_hit": False, "high_hit": False, "progress": 0.0}
        return {
            "count": 0,
            "overall": 0.0,
            "x": dict(empty_axis),
            "y": dict(empty_axis),
            "size": dict(empty_axis),
            "skew": {"max": 0.0, "target": targets.skew_max, "progress": 0.0, "hit": False},
            "points": [],
        }

    matrix = np.asarray([pose.as_list() for pose in poses], dtype=np.float64)
    x_summary = _two_sided_progress(matrix[:, 0], targets.x_min, targets.x_max)
    y_summary = _two_sided_progress(matrix[:, 1], targets.y_min, targets.y_max)
    size_summary = _two_sided_progress(matrix[:, 2], targets.size_min, targets.size_max)
    skew_max = float(np.max(matrix[:, 3]))
    skew_progress = float(np.clip(skew_max / max(targets.skew_max, 1e-9), 0.0, 1.0))
    parts = [
        float(x_summary["progress"]),
        float(y_summary["progress"]),
        float(size_summary["progress"]),
        skew_progress,
    ]
    return {
        "count": len(poses),
        "overall": float(np.mean(parts)),
        "x": x_summary,
        "y": y_summary,
        "size": size_summary,
        "skew": {
            "max": skew_max,
            "target": targets.skew_max,
            "progress": skew_progress,
            "hit": bool(skew_max >= targets.skew_max),
        },
        "points": [pose.as_dict() for pose in poses],
    }
