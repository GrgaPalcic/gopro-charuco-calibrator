from gopro_charuco_calibrator.coverage import PoseParams, coverage_summary
from gopro_charuco_calibrator.models import CoverageTargets


def test_coverage_summary_hits_xy_band_size_and_skew():
    poses = [
        PoseParams(x=0.20, y=0.50, size=0.18, skew=0.10),
        PoseParams(x=0.80, y=0.20, size=0.55, skew=0.20),
        PoseParams(x=0.50, y=0.80, size=0.35, skew=0.50),
    ]

    summary = coverage_summary(poses, CoverageTargets())

    assert summary["x"]["low_hit"] is True
    assert summary["x"]["high_hit"] is True
    assert summary["y"]["low_hit"] is True
    assert summary["y"]["high_hit"] is True
    assert summary["size"]["progress"] == 1.0
    assert summary["skew"]["hit"] is True
    assert summary["overall"] == 1.0
