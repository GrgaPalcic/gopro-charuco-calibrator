import numpy as np

from gopro_charuco_calibrator.boards import caib_marker_object_points
from gopro_charuco_calibrator.models import BoardConfig, caib_marker_count


def test_caib_marker_count_matches_a3_preset():
    assert caib_marker_count(11, 8) == 44


def test_caib_object_points_start_id_and_margin():
    config = BoardConfig(
        cols=3,
        rows=2,
        square_m=0.04,
        marker_m=0.02,
        start_id=7,
        marker_count=3,
    )
    obj_by_id = caib_marker_object_points(config)

    assert sorted(obj_by_id) == [7, 8, 9]
    np.testing.assert_allclose(obj_by_id[7][0], [0.01, 0.01, 0.0])
    np.testing.assert_allclose(obj_by_id[8][0], [0.09, 0.01, 0.0])
    np.testing.assert_allclose(obj_by_id[9][0], [0.05, 0.05, 0.0])
