from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import yaml


def save_camera_info_yaml(
    path: Path,
    *,
    camera_name: str,
    image_size: tuple[int, int],
    camera_matrix: np.ndarray,
    dist_coeffs: np.ndarray,
    distortion_model: str,
    rectify_alpha: float = 0.0,
) -> None:
    camera_matrix = np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3)
    dist_coeffs = np.asarray(dist_coeffs, dtype=np.float64).ravel()
    new_camera_matrix, _roi = cv2.getOptimalNewCameraMatrix(
        camera_matrix,
        dist_coeffs,
        image_size,
        float(rectify_alpha),
    )
    rectification = np.eye(3, dtype=np.float64)
    projection = np.zeros((3, 4), dtype=np.float64)
    projection[:3, :3] = new_camera_matrix
    info = {
        "image_width": int(image_size[0]),
        "image_height": int(image_size[1]),
        "camera_name": camera_name,
        "camera_matrix": {"rows": 3, "cols": 3, "data": camera_matrix.flatten().tolist()},
        "distortion_model": distortion_model,
        "distortion_coefficients": {
            "rows": 1,
            "cols": int(len(dist_coeffs)),
            "data": dist_coeffs.tolist(),
        },
        "rectification_matrix": {"rows": 3, "cols": 3, "data": rectification.flatten().tolist()},
        "projection_matrix": {"rows": 3, "cols": 4, "data": projection.flatten().tolist()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(info, stream, sort_keys=False)
