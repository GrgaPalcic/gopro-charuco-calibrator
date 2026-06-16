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


def caib_marker_object_points(config: BoardConfig, flipped: bool = False) -> dict[int, np.ndarray]:
    """Marker corner coordinates for a caib.io board.

    Markers sit on alternating checkerboard cells, but which parity the first
    row starts on depends on the board's corner colour, which caib.io varies
    with the grid dimensions. ``flipped=False`` is the historical layout (row 0
    markers at even columns); ``flipped=True`` is the mirrored parity (row 0 at
    odd columns). Use ``solver.detect_board_layout`` to pick the right one from
    real detections.
    """
    obj_by_id: dict[int, np.ndarray] = {}
    marker_id = int(config.start_id)
    margin = 0.5 * (config.square_m - config.marker_m)
    end_id = config.start_id + config.marker_count
    first = 1 if flipped else 0
    for row in range(config.rows):
        start = (first + row) % 2
        marker_cols = range(start, config.cols, 2)
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


def make_caib_board(config: BoardConfig, dictionary=None, flipped: bool = False):
    dictionary = dictionary or resolve_dictionary(config.aruco_dict)
    obj_by_id = caib_marker_object_points(config, flipped=flipped)
    ids = np.asarray(sorted(obj_by_id), dtype=np.int32)
    obj_points = [obj_by_id[int(marker_id)] for marker_id in ids]
    if hasattr(aruco, "Board_create"):
        board = aruco.Board_create(obj_points, dictionary, ids)
    else:
        board = aruco.Board(obj_points, dictionary, ids)
    return board, obj_by_id
