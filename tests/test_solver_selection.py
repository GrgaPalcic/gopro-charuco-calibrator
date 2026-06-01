import numpy as np

from gopro_charuco_calibrator.coverage import PoseParams
from gopro_charuco_calibrator.solver import DetectionRecord, select_frame_subset


def make_record(name: str, pose: PoseParams) -> DetectionRecord:
    return DetectionRecord(
        name=name,
        corners=[],
        ids=np.empty((0, 1), dtype=np.int32),
        marker_count=10,
        pose=pose,
    )


def test_selection_caps_with_coverage_extremes():
    records = [
        make_record("x_low.jpg", PoseParams(0.1, 0.5, 0.3, 0.1)),
        make_record("x_high.jpg", PoseParams(0.9, 0.5, 0.3, 0.1)),
        make_record("y_low.jpg", PoseParams(0.5, 0.1, 0.3, 0.1)),
        make_record("y_high.jpg", PoseParams(0.5, 0.9, 0.3, 0.1)),
        make_record("center.jpg", PoseParams(0.5, 0.5, 0.3, 0.1)),
    ]
    errors = {record.name: 0.3 for record in records}

    selection = select_frame_subset(
        records,
        errors,
        max_view_error_px=2.5,
        outlier_mad_multiplier=3.5,
        min_selected_frames=3,
        max_selected_frames=4,
    )

    assert len(selection.selected_names) == 4
    assert {"x_low.jpg", "x_high.jpg", "y_low.jpg", "y_high.jpg"} <= set(
        selection.selected_names
    )
    assert selection.capped_max_frames is True
