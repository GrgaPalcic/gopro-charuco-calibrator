import cv2
import numpy as np

from gopro_charuco_calibrator.boards import make_caib_board
from gopro_charuco_calibrator.coverage import coverage_params
from gopro_charuco_calibrator.models import BoardConfig
from gopro_charuco_calibrator.solver import DetectionRecord, calibration_solve


def test_synthetic_aruco_solve_recovers_intrinsics():
    board_config = BoardConfig(
        cols=5,
        rows=4,
        square_m=0.04,
        marker_m=0.028,
        start_id=20,
        marker_count=10,
    )
    board, obj_by_id = make_caib_board(board_config)
    image_size = (1280, 720)
    k_true = np.asarray([[720.0, 0.0, 640.0], [0.0, 725.0, 360.0], [0.0, 0.0, 1.0]])
    d_true = np.zeros(5)
    records = []

    for index in range(18):
        rvec = np.asarray(
            [
                0.12 * np.sin(index * 0.7),
                0.18 * np.cos(index * 0.5),
                -0.20 + index * 0.025,
            ],
            dtype=np.float64,
        )
        tvec = np.asarray(
            [
                -0.08 + 0.01 * (index % 5),
                -0.04 + 0.01 * (index % 4),
                0.65 + 0.015 * (index % 3),
            ],
            dtype=np.float64,
        )
        corners = []
        ids = []
        all_points = []
        for marker_id, obj in obj_by_id.items():
            projected, _ = cv2.projectPoints(obj, rvec, tvec, k_true, d_true)
            corner = projected.reshape(1, 4, 2).astype(np.float32)
            corners.append(corner)
            ids.append([marker_id])
            all_points.append(corner.reshape(4, 2))
        points = np.concatenate(all_points, axis=0)
        records.append(
            DetectionRecord(
                name=f"view_{index:03d}.jpg",
                corners=corners,
                ids=np.asarray(ids, dtype=np.int32),
                marker_count=len(corners),
                pose=coverage_params(points, image_size),
            )
        )

    solve = calibration_solve(image_size, records, board, obj_by_id, flags=0)

    assert solve.rms < 1e-3
    np.testing.assert_allclose(solve.camera_matrix[0, 0], k_true[0, 0], rtol=0.02)
    np.testing.assert_allclose(solve.camera_matrix[1, 1], k_true[1, 1], rtol=0.02)
    np.testing.assert_allclose(solve.camera_matrix[0, 2], k_true[0, 2], atol=2.0)
    np.testing.assert_allclose(solve.camera_matrix[1, 2], k_true[1, 2], atol=2.0)
