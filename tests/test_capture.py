from gopro_charuco_calibrator.capture import _discarded_points


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
