from __future__ import annotations

import cv2.aruco as aruco
import numpy as np

from .models import BoardConfig


def resolve_dictionary(name: str):
    attr = name if name.startswith("DICT_") else f"DICT_{name}"
    if not hasattr(aruco, attr):
        raise ValueError(f"Unknown ArUco dictionary: {name}")
    return aruco.getPredefinedDictionary(getattr(aruco, attr))


def detector_params():
    if hasattr(aruco, "DetectorParameters_create"):
        params = aruco.DetectorParameters_create()
    else:
        params = aruco.DetectorParameters()
    params.cornerRefinementMethod = aruco.CORNER_REFINE_SUBPIX
    return params


def caib_marker_object_points(config: BoardConfig) -> dict[int, np.ndarray]:
    obj_by_id: dict[int, np.ndarray] = {}
    marker_id = int(config.start_id)
    margin = 0.5 * (config.square_m - config.marker_m)
    end_id = config.start_id + config.marker_count
    for row in range(config.rows):
        marker_cols = range(0, config.cols, 2) if row % 2 == 0 else range(1, config.cols, 2)
        for col in marker_cols:
            if marker_id >= end_id:
                return obj_by_id
            x = col * config.square_m + margin
            y = row * config.square_m + margin
            obj_by_id[marker_id] = np.asarray(
                [
                    [x, y, 0.0],
                    [x + config.marker_m, y, 0.0],
                    [x + config.marker_m, y + config.marker_m, 0.0],
                    [x, y + config.marker_m, 0.0],
                ],
                dtype=np.float32,
            )
            marker_id += 1
    return obj_by_id


def make_caib_board(config: BoardConfig, dictionary=None):
    dictionary = dictionary or resolve_dictionary(config.aruco_dict)
    obj_by_id = caib_marker_object_points(config)
    ids = np.asarray(sorted(obj_by_id), dtype=np.int32)
    obj_points = [obj_by_id[int(marker_id)] for marker_id in ids]
    if hasattr(aruco, "Board_create"):
        board = aruco.Board_create(obj_points, dictionary, ids)
    else:
        board = aruco.Board(obj_points, dictionary, ids)
    return board, obj_by_id
