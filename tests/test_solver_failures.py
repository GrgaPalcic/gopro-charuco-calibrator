import json

import numpy as np

from gopro_charuco_calibrator.capture import _discarded_points
from gopro_charuco_calibrator.coverage import PoseParams
from gopro_charuco_calibrator.models import BoardConfig, CameraConfig, SolverConfig
from gopro_charuco_calibrator.solver import DetectionRecord, solve_from_frames


def _record(index: int) -> DetectionRecord:
    return DetectionRecord(
        name=f"capture_{index:03d}.jpg",
        corners=[],
        ids=np.empty((0, 1), dtype=np.int32),
        marker_count=12,
        pose=PoseParams(0.4, 0.5, 0.3, 0.1),
    )


def test_solve_from_frames_records_failed_model(monkeypatch, tmp_path):
    records = [_record(index) for index in range(1, 26)]

    monkeypatch.setattr(
        "gopro_charuco_calibrator.solver.detect_frames",
        lambda **_kwargs: ((1920, 1080), records),
    )
    monkeypatch.setattr(
        "gopro_charuco_calibrator.solver.make_caib_board",
        lambda *_args, **_kwargs: (object(), {}),
    )

    def fail_model(**_kwargs):
        raise RuntimeError("fisheye did not converge")

    monkeypatch.setattr("gopro_charuco_calibrator.solver.run_model", fail_model)

    summary = solve_from_frames(
        frames_dir=tmp_path / "frames",
        output_dir=tmp_path / "out",
        camera=CameraConfig(),
        board_config=BoardConfig(),
        solver=SolverConfig(models=["fisheye"]),
    )

    assert summary["results"] == [
        {
            "model": "fisheye",
            "ok": False,
            "error_type": "RuntimeError",
            "error": "fisheye did not converge",
            "rms": None,
            "median_view_error_px": None,
            "worst_view_error_px": None,
            "camera_matrix": None,
            "distortion": None,
            "yaml": None,
            "all_frames": None,
            "selected": None,
            "diagnostics_csv": None,
        }
    ]
    summary_path = tmp_path / "out" / "caib_marker_board_calibration_summary.json"
    assert json.loads(summary_path.read_text())["results"][0]["ok"] is False


def test_discarded_points_ignores_failed_models():
    assert _discarded_points(
        {
            "frames": [{"name": "capture_001.jpg", "x": 0.4, "y": 0.5}],
            "results": [
                {"model": "fisheye", "ok": False, "error": "did not converge"},
            ],
        }
    ) == []
