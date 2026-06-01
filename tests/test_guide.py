from gopro_charuco_calibrator.coverage import PoseParams
from gopro_charuco_calibrator.guide import guide_status
from gopro_charuco_calibrator.models import CoverageTargets


def test_guide_advances_to_next_checkpoint_after_capture():
    targets = CoverageTargets()
    status = guide_status([PoseParams(0.5, 0.5, 0.35, 0.0)], None, targets)

    assert status["complete_count"] == 1
    assert status["current"]["label"] == "left small"


def test_guide_live_match_marks_current_checkpoint():
    targets = CoverageTargets()
    status = guide_status([], PoseParams(0.5, 0.5, 0.35, 0.0), targets)

    assert status["current"]["label"] == "center medium"
    assert status["current"]["live_match"] is True
