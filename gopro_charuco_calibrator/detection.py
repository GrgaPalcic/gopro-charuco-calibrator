from __future__ import annotations

from dataclasses import dataclass

import cv2
import cv2.aruco as aruco
import numpy as np

from .boards import detector_params, resolve_dictionary
from .coverage import PoseParams, coverage_params
from .models import BoardConfig


@dataclass(frozen=True)
class MarkerDetection:
    corners: list[np.ndarray]
    ids: np.ndarray
    centers: dict[int, np.ndarray]
    pose: PoseParams

    @property
    def marker_count(self) -> int:
        return len(self.corners)

    def all_points(self) -> np.ndarray:
        return np.concatenate([corner.reshape(-1, 2) for corner in self.corners], axis=0)


def detect_markers(
    gray: np.ndarray,
    image_size: tuple[int, int],
    board_config: BoardConfig,
    dictionary=None,
    params=None,
) -> MarkerDetection | None:
    dictionary = dictionary or resolve_dictionary(board_config.aruco_dict)
    params = params or detector_params()
    corners, ids, _rejected = aruco.detectMarkers(gray, dictionary, parameters=params)
    if ids is None:
        return None

    valid_ids = set(range(board_config.start_id, board_config.start_id + board_config.marker_count))
    kept_corners: list[np.ndarray] = []
    kept_ids: list[list[int]] = []
    centers: dict[int, np.ndarray] = {}
    for corner, marker_id in zip(corners, ids.ravel(), strict=True):
        marker_id = int(marker_id)
        if marker_id not in valid_ids:
            continue
        pts = corner.reshape(4, 2).astype(np.float32)
        kept_corners.append(corner.astype(np.float32))
        kept_ids.append([marker_id])
        centers[marker_id] = pts.mean(axis=0)

    if not kept_corners:
        return None

    points = np.concatenate([corner.reshape(-1, 2) for corner in kept_corners], axis=0)
    return MarkerDetection(
        corners=kept_corners,
        ids=np.asarray(kept_ids, dtype=np.int32),
        centers=centers,
        pose=coverage_params(points, image_size),
    )


def marker_motion(prev: dict[int, np.ndarray] | None, cur: dict[int, np.ndarray]) -> float | None:
    if prev is None:
        return None
    shared = sorted(set(prev) & set(cur))
    if not shared:
        return None
    deltas = np.asarray(
        [cur[marker_id] - prev[marker_id] for marker_id in shared],
        dtype=np.float64,
    )
    return float(np.mean(np.linalg.norm(deltas, axis=1)))


def draw_detection(
    frame: np.ndarray,
    detection: MarkerDetection | None,
    text: str,
    *,
    selected: bool = False,
) -> np.ndarray:
    display = frame.copy()
    if detection is not None and detection.corners:
        color = (0, 255, 0) if selected else (0, 255, 255)
        aruco.drawDetectedMarkers(display, detection.corners, detection.ids, borderColor=color)
    cv2.putText(
        display,
        text,
        (16, 34),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.85,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return display
