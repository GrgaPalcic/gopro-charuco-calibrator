from gopro_charuco_calibrator.capture import _high_error_points


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


def test_high_error_points_only_flags_high_error_discards():
    points = _high_error_points(_summary())
    # Only the high-error frame (capture_001), not the surplus-capped one (capture_002).
    assert len(points) == 1
    point = points[0]
    assert point["x"] == 0.2
    assert point["y"] == 0.3
    assert point["error"] == 3.1


def test_high_error_points_handles_empty():
    assert _high_error_points({"frames": [], "results": []}) == []
    assert _high_error_points({}) == []
