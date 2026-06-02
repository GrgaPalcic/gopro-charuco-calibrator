from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .coverage import PoseParams
from .models import CoverageTargets


@dataclass(frozen=True)
class GuideCheckpoint:
    label: str
    x: float
    y: float
    size: float
    skew: float = 0.0

    def as_dict(self, *, complete: bool, current: bool, live_match: bool) -> dict[str, Any]:
        return {
            "label": self.label,
            "x": self.x,
            "y": self.y,
            "size": self.size,
            "skew": self.skew,
            "complete": complete,
            "current": current,
            "live_match": live_match,
        }


ROUTE_POINTS = [
    ("center", 0.50, 0.50),
    ("left", 0.20, 0.50),
    ("top-left", 0.20, 0.20),
    ("top", 0.50, 0.20),
    ("top-right", 0.80, 0.20),
    ("right", 0.80, 0.50),
    ("bottom-right", 0.80, 0.80),
    ("bottom", 0.50, 0.80),
    ("bottom-left", 0.20, 0.80),
]


def _ring(offset: float) -> list[tuple[str, float, float]]:
    """The 8-point route + center, at ``offset`` from frame center on each axis."""
    lo, hi = 0.5 - offset, 0.5 + offset
    return [
        ("left", lo, 0.50),
        ("top-left", lo, lo),
        ("top", 0.50, lo),
        ("top-right", hi, lo),
        ("right", hi, 0.50),
        ("bottom-right", hi, hi),
        ("bottom", 0.50, hi),
        ("bottom-left", lo, hi),
        ("center", 0.50, 0.50),
    ]


def default_checkpoints(targets: CoverageTargets) -> list[GuideCheckpoint]:
    small = max(targets.size_min, 0.16)
    medium = min(max(0.35, targets.size_min), targets.size_max)
    large = min(targets.size_max, 0.62)
    # A board large enough to fill ~`large` of the frame spills off two edges if it
    # is centered in a frame corner, so those poses cannot keep enough markers in
    # view. Pull the large route onto a tighter ring so big boards stay in frame.
    large_offset = max(0.12, min(0.20, (1.0 - large) / 2.0 - 0.02))
    checkpoints = [GuideCheckpoint("center medium", 0.50, 0.50, medium)]
    checkpoints.extend(
        GuideCheckpoint(f"{label} small", x, y, small)
        for label, x, y in ROUTE_POINTS[1:] + ROUTE_POINTS[:1]
    )
    checkpoints.extend(
        GuideCheckpoint(f"{label} large", x, y, large)
        for label, x, y in _ring(large_offset)
    )
    # Tilt poses: a board can't be held at an exact apparent size while also being
    # tilted, so the guide asks for a clearly-tilted board (a margin below the
    # coverage skew target) and relaxes the size gate below.
    tilt = round(min(targets.skew_max, max(0.30, 0.8 * targets.skew_max)), 2)
    checkpoints.extend(
        [
            GuideCheckpoint("tilt upper-left", 0.30, 0.30, medium, tilt),
            GuideCheckpoint("tilt upper-right", 0.70, 0.30, medium, tilt),
            GuideCheckpoint("tilt lower-right", 0.70, 0.70, medium, tilt),
            GuideCheckpoint("tilt lower-left", 0.30, 0.70, medium, tilt),
        ]
    )
    return checkpoints


def pose_matches_checkpoint(
    pose: PoseParams,
    checkpoint: GuideCheckpoint,
    *,
    xy_tol: float = 0.085,
    size_tol: float = 0.105,
) -> bool:
    if abs(pose.x - checkpoint.x) > xy_tol:
        return False
    if abs(pose.y - checkpoint.y) > xy_tol:
        return False
    # Tilting a board changes its apparent bounding-box size, so don't also demand
    # a precise size for tilt checkpoints; position + adequate tilt is enough.
    effective_size_tol = size_tol if checkpoint.skew <= 0.0 else max(size_tol, 0.20)
    if abs(pose.size - checkpoint.size) > effective_size_tol:
        return False
    if checkpoint.skew > 0.0 and pose.skew < checkpoint.skew:
        return False
    return True


def guide_status(
    captured_poses: list[PoseParams],
    live_pose: PoseParams | None,
    targets: CoverageTargets,
) -> dict[str, Any]:
    checkpoints = default_checkpoints(targets)
    complete_flags = [
        any(pose_matches_checkpoint(pose, checkpoint) for pose in captured_poses)
        for checkpoint in checkpoints
    ]
    incomplete = [index for index, done in enumerate(complete_flags) if not done]
    if not incomplete:
        # Everything done: park on the last checkpoint.
        current_index = len(checkpoints) - 1
    else:
        # Sequential: always guide to the first unmet checkpoint. This keeps the
        # highlighted target stable (it does not hop around as the live pose
        # jitters); the route is ordered so checkpoints stay reachable in turn.
        current_index = incomplete[0]

    points = []
    for index, checkpoint in enumerate(checkpoints):
        live_match = live_pose is not None and pose_matches_checkpoint(live_pose, checkpoint)
        points.append(
            checkpoint.as_dict(
                complete=complete_flags[index],
                current=index == current_index and not all(complete_flags),
                live_match=live_match,
            )
        )

    current = points[current_index] if points else None
    return {
        "checkpoints": points,
        "current": current,
        "complete_count": sum(1 for flag in complete_flags if flag),
        "total_count": len(checkpoints),
        "complete": all(complete_flags) if complete_flags else False,
    }
