import pytest

from gopro_charuco_calibrator import capture
from gopro_charuco_calibrator.capture import CaptureSession, _discarded_points


def _summary():
    return {
        "frames": [
            {"name": "capture_001.jpg", "x": 0.2, "y": 0.3, "size": 0.30, "skew": 0.1},
            {"name": "capture_002.jpg", "x": 0.8, "y": 0.7, "size": 0.40, "skew": 0.2},
            {"name": "capture_003.jpg", "x": 0.5, "y": 0.5, "size": 0.35, "skew": 0.0},
        ],
        "results": [
            {
                "model": "plumb_bob",
                "median_view_error_px": 1.2,
                "selected": {
                    "rejected_frames": [
                        {
                            "name": "capture_001.jpg",
                            "reason": "view_error_px 3.100 > threshold_px 2.500",
                            "all_view_error_px": 3.1,
                        },
                        {
                            "name": "capture_002.jpg",
                            "reason": "over_max_selected_frames",
                            "all_view_error_px": 0.9,
                        },
                    ]
                },
            },
            {
                "model": "rational_polynomial",
                "median_view_error_px": 2.0,
                "selected": {"rejected_frames": []},
            },
        ],
    }


def test_discarded_points_classifies_error_vs_surplus():
    points = _discarded_points(_summary())
    by_name = {(round(p["x"], 3), round(p["y"], 3)): p for p in points}
    assert len(points) == 2
    # capture_001 was discarded for high error.
    assert by_name[(0.2, 0.3)]["kind"] == "error"
    assert by_name[(0.2, 0.3)]["error"] == 3.1
    # capture_002 was trimmed as surplus (over the selection cap).
    assert by_name[(0.8, 0.7)]["kind"] == "surplus"


def test_discarded_points_handles_empty():
    assert _discarded_points({"frames": [], "results": []}) == []
    assert _discarded_points({}) == []


def test_failed_solve_after_stop_returns_to_idle(tmp_path, monkeypatch):
    session = CaptureSession(runs_dir=tmp_path)
    session.output_dir = tmp_path / "run"
    session.frames_dir = session.output_dir / "frames"
    session.frames_dir.mkdir(parents=True)

    def broken_solve(**_kwargs):
        raise RuntimeError("no usable frames")

    monkeypatch.setattr(capture, "solve_from_frames", broken_solve)
    with pytest.raises(RuntimeError, match="no usable frames"):
        session.solve()
    status = session.status()
    # No preview is streaming (as after Stop), so there is nothing to pause on.
    assert status["state"] == "idle"
    assert status["preview_open"] is False
    assert "solve failed: no usable frames" in status["message"]


def test_failed_solve_with_a_live_preview_parks_the_run_paused(tmp_path, monkeypatch):
    session = CaptureSession(runs_dir=tmp_path)
    session.output_dir = tmp_path / "run"
    session.frames_dir = session.output_dir / "frames"
    session.frames_dir.mkdir(parents=True)
    monkeypatch.setattr(session, "preview_open", lambda: True)

    def broken_solve(**_kwargs):
        raise RuntimeError("no usable frames")

    monkeypatch.setattr(capture, "solve_from_frames", broken_solve)
    with pytest.raises(RuntimeError):
        session.solve()
    assert session.status()["state"] == "paused"


def test_resume_after_stop_does_not_pretend_to_capture(tmp_path):
    session = CaptureSession(runs_dir=tmp_path)
    session.output_dir = tmp_path / "run"
    session._state = "solved"
    session._set_status(state="solved")
    status = session.resume()
    assert status["state"] == "solved"
    assert "open the preview" in status["message"]


def _offline_session(tmp_path, monkeypatch, report):
    # No camera on the bus: stub the two calls close() makes to it.
    monkeypatch.setattr(capture, "stop_gopro_video_bridge", lambda _config: None)
    monkeypatch.setattr(capture, "stop_gopro_webcam", lambda _result: None)
    session = CaptureSession(runs_dir=tmp_path)
    session.config.gopro.enabled = True
    session._last_gopro_result = report
    session._set_status(gopro=report)
    return session


def test_next_camera_forgets_the_previous_camera_report(tmp_path, monkeypatch):
    session = _offline_session(tmp_path, monkeypatch, {"ok": True})
    session.next_camera()
    assert session.status()["gopro"] is None
    assert session._last_gopro_result is None


def test_next_camera_clears_the_previous_run_from_the_status(tmp_path, monkeypatch):
    session = _offline_session(tmp_path, monkeypatch, {"ok": True})
    session._set_status(
        run_id="cam1_x",
        captures=40,
        results=[{"model": "fisheye"}],
        summary_path="x.json",
        rejected_points=[{"x": 0.5, "y": 0.5}],
        markers=36,
        pose={"x": 0.5, "y": 0.5, "size": 0.3, "skew": 0.1},
    )
    status = session.next_camera()
    assert status["run_id"] is None
    assert status["captures"] == 0
    assert status["results"] is None
    assert status["summary_path"] is None
    assert status["rejected_points"] == []
    assert status["coverage"]["count"] == 0
    assert status["markers"] == 0
    assert status["pose"] is None


def test_stop_keeps_the_camera_report_for_a_later_solve(tmp_path, monkeypatch):
    # Stop then Solve is a normal flow; the summary must still say what the camera applied.
    report = {"ok": True}
    session = _offline_session(tmp_path, monkeypatch, report)
    session.close()
    assert session._last_gopro_result is report
