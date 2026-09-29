"""HERO13 + Max Lens Mod readiness: camera state read-back, model recommendation,
OpenICC failure messages and image build, and a synthetic end-to-end Double
Sphere solve through the real OpenICC image (skipped when it is not built)."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from gopro_charuco_calibrator.boards import make_caib_board
from gopro_charuco_calibrator.capture import _discarded_points
from gopro_charuco_calibrator.coverage import coverage_params
from gopro_charuco_calibrator.gopro import (
    GoProClient,
    apply_gopro_settings,
    camera_state_warnings,
    describe_acquisition_mode,
    read_camera_state,
)
from gopro_charuco_calibrator.models import (
    AppConfig,
    BoardConfig,
    CameraConfig,
    GoProSettingsConfig,
)
from gopro_charuco_calibrator.openicc import (
    OPENICC_COMMIT,
    OPENICC_REPO,
    OpenICCError,
    OpenICCSettings,
    build_image_commands,
    run_calibrate_camera,
    run_double_sphere_model,
)
from gopro_charuco_calibrator.solver import DetectionRecord, recommended_model

# --- fake GoPro HTTP API ---


class _Response:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


def _fake_urlopen(calls, state_body: bytes | None):
    def fake(url, timeout):
        calls.append(url)
        if url.endswith("/gopro/camera/state"):
            if state_body is None:
                raise OSError("state unavailable")
            return _Response(state_body)
        return _Response(b'{"status": "ok"}')

    return fake


def _state(settings: dict[str, int]) -> bytes:
    return json.dumps({"status": {}, "settings": settings}).encode()


def test_read_camera_state_parses_lens_settings(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "gopro_charuco_calibrator.gopro.urlopen",
        _fake_urlopen(calls, _state({"43": 0, "189": 2, "135": 0, "2": 9})),
    )
    state = read_camera_state(GoProClient("http://172.20.144.51:8080"))
    assert state == {
        "ok": True,
        "settings": {"webcam_digital_lens": 0, "max_lens_mod": 2, "hypersmooth": 0},
        "labels": {
            "webcam_digital_lens": "Wide (0)",
            "max_lens_mod": "Max Lens 2.0 (2)",
            "hypersmooth": "Off (0)",
        },
    }


def test_read_camera_state_failure_is_not_fatal(monkeypatch):
    monkeypatch.setattr(
        "gopro_charuco_calibrator.gopro.urlopen", _fake_urlopen([], state_body=None)
    )
    state = read_camera_state(GoProClient("http://172.20.144.51:8080"))
    assert state["ok"] is False and "state unavailable" in state["error"]


def test_camera_state_warnings_flag_lens_and_mod_mismatch():
    config = GoProSettingsConfig(enabled=True, webcam_fov=0, max_lens_mod=2)
    matching = {"settings": {"webcam_digital_lens": 0, "max_lens_mod": 2}}
    assert camera_state_warnings(config, matching) == []
    wrong = {"settings": {"webcam_digital_lens": 3, "max_lens_mod": 0}}
    warnings = camera_state_warnings(config, wrong)
    assert len(warnings) == 2
    assert "SuperView" in warnings[0] and "Wide (0)" in warnings[0]
    assert "None (0)" in warnings[1] and "Max Lens 2.0 (2)" in warnings[1]
    # No requested mod means nothing to compare against.
    assert camera_state_warnings(GoProSettingsConfig(webcam_fov=0), wrong)[1:] == []


def test_apply_settings_records_state_without_affecting_ok(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "gopro_charuco_calibrator.gopro.urlopen", _fake_urlopen(calls, state_body=None)
    )
    result = apply_gopro_settings(
        GoProSettingsConfig(
            enabled=True, base_url="http://172.20.144.51:8080", webcam_fov=0, max_lens_mod=2
        )
    )
    assert result["ok"] is True  # a failed read-back must never fail the preview
    assert result["camera_state"]["ok"] is False
    assert result["warnings"] == []
    assert any("setting=189&option=2" in call for call in calls)
    assert calls[-1].endswith("/gopro/camera/state")


def test_acquisition_mode_includes_reported_settings(monkeypatch):
    monkeypatch.setattr(
        "gopro_charuco_calibrator.gopro.urlopen",
        _fake_urlopen([], _state({"43": 0, "189": 2})),
    )
    config = AppConfig(
        gopro=GoProSettingsConfig(
            enabled=True, base_url="http://172.20.144.51:8080", webcam_fov=0, max_lens_mod=2
        )
    )
    result = apply_gopro_settings(config.gopro)
    mode = describe_acquisition_mode(config, result)
    assert mode["reported_webcam_digital_lens"] == "Wide (0)"
    assert mode["reported_max_lens_mod"] == "Max Lens 2.0 (2)"
    assert "reported_max_lens_mod" not in describe_acquisition_mode(config)


# --- recommendation ---


def _result(model, median=None, rms=None, ok=True, **extra):
    return {"model": model, "ok": ok, "median_view_error_px": median, "rms": rms, **extra}


def test_recommended_model_prefers_double_sphere_when_it_solved():
    results = [_result("fisheye", median=0.9, rms=1.0), _result("double_sphere", 1.1, 1.1)]
    assert recommended_model(results) == "double_sphere"


def test_recommended_model_falls_back_to_lowest_median():
    results = [
        _result("fisheye", median=1.2, rms=1.3),
        _result("rational_polynomial", median=0.8, rms=2.0),
        _result("double_sphere", ok=False),
    ]
    assert recommended_model(results) == "rational_polynomial"
    assert recommended_model([_result("plumb_bob", rms=1.5)]) == "plumb_bob"
    assert recommended_model([_result("fisheye", ok=False)]) is None


def test_discarded_points_skip_double_sphere_but_follow_recommendation():
    frames = [{"name": "capture_001.jpg", "x": 0.1, "y": 0.2, "size": 0.3, "skew": 0.1}]
    rejected = {"rejected_frames": [{"name": "capture_001.jpg", "reason": "view_error_px 9"}]}
    summary = {
        "frames": frames,
        "results": [
            _result("double_sphere", 1.0, 1.0, recommended=True, selected={"frame_count": 30}),
            _result("rational_polynomial", 0.5, 0.5, recommended=False, selected={
                "rejected_frames": []
            }),
            _result("fisheye", 1.2, 1.2, recommended=False, selected=rejected),
        ],
    }
    # double_sphere has no per-frame data, so the best cv2 model places the points.
    assert _discarded_points(summary) == []
    summary["results"][2]["recommended"] = True
    summary["results"][0]["recommended"] = False
    points = _discarded_points(summary)
    assert [(point["x"], point["kind"]) for point in points] == [(0.1, "error")]


# --- OpenICC runner ---


def test_stale_result_is_not_reused(tmp_path, monkeypatch):
    (tmp_path / "out.json").write_text('{"stale": true}', encoding="utf-8")

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(OpenICCError, match="no result JSON"):
        run_calibrate_camera(tmp_path, OpenICCSettings())


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (
            "Unable to find image 'openicc:latest' locally\n"
            "docker: Error response from daemon: pull access denied for openicc",
            "setup-openicc",
        ),
        (
            "permission denied while trying to connect to the Docker daemon socket",
            "docker group",
        ),
    ],
)
def test_docker_setup_errors_say_what_to_do(tmp_path, monkeypatch, stderr, expected):
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 125, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(OpenICCError, match=expected):
        run_calibrate_camera(tmp_path, OpenICCSettings())


def test_build_image_commands_pin_the_commit(tmp_path):
    fresh = build_image_commands(tmp_path / "src", "openicc")
    assert fresh[0] == ["git", "init", "-q", str(tmp_path / "src")]
    assert fresh[1][-1] == OPENICC_REPO
    assert fresh[2][-1] == OPENICC_COMMIT
    assert fresh[-1] == ["docker", "build", "-t", "openicc", str(tmp_path / "src")]
    (tmp_path / "src" / ".git").mkdir(parents=True)
    existing = build_image_commands(tmp_path / "src", "custom")
    assert existing[0][3] == "fetch" and existing[-1][3] == "custom"


# --- end to end through the real OpenICC image ---


def _openicc_image_present() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", "openicc"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _rodrigues(rvec: np.ndarray) -> np.ndarray:
    theta = float(np.linalg.norm(rvec))
    if theta < 1e-12:
        return np.eye(3)
    k = rvec / theta
    kx = np.asarray([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(theta) * kx + (1 - np.cos(theta)) * kx @ kx


def _project_double_sphere(points, fx, fy, cx, cy, xi, alpha):
    """Double Sphere projection (Usenko et al. 2018, eq. 40-45)."""
    x, y, z = points[:, 0], points[:, 1], points[:, 2]
    d1 = np.sqrt(x * x + y * y + z * z)
    zeta = xi * d1 + z
    d2 = np.sqrt(x * x + y * y + zeta * zeta)
    denom = alpha * d2 + (1 - alpha) * zeta
    w1 = alpha / (1 - alpha) if alpha <= 0.5 else (1 - alpha) / alpha
    w2 = (w1 + xi) / np.sqrt(2 * w1 * xi + xi * xi + 1)
    valid = z > -w2 * d1
    return np.stack([fx * x / denom + cx, fy * y / denom + cy], axis=1), valid


@pytest.mark.skipif(not _openicc_image_present(), reason="needs the openicc docker image")
def test_double_sphere_recovers_measured_hero13_intrinsics(tmp_path):
    # Ground truth = the HERO13 + Max Lens Mod 2.0 webcam-Wide solve measured on
    # 2026-06-12 (0.617 px, alpha 0.708), rendered for a wide spread of poses.
    fx, cx, cy, xi, alpha = 629.19, 949.64, 538.89, 0.00166, 0.708
    image_size = (1920, 1080)
    board_config = BoardConfig()
    _board, obj_by_id = make_caib_board(board_config)
    rng = np.random.default_rng(7)
    center = np.asarray([board_config.pattern_width_m / 2, board_config.pattern_height_m / 2, 0])

    records = []
    for index in range(60):
        rvec = rng.uniform([-0.7, -0.7, -0.5], [0.7, 0.7, 0.5])
        tvec = rng.uniform([-0.45, -0.25, 0.28], [0.45, 0.25, 0.65])
        rotation = _rodrigues(rvec)
        corners, ids, points = [], [], []
        for marker_id, obj in obj_by_id.items():
            camera_points = (rotation @ (obj - center).T).T + tvec
            pixels, valid = _project_double_sphere(camera_points, fx, fx, cx, cy, xi, alpha)
            inside = (
                valid.all()
                and (pixels[:, 0] > 2).all()
                and (pixels[:, 0] < image_size[0] - 2).all()
                and (pixels[:, 1] > 2).all()
                and (pixels[:, 1] < image_size[1] - 2).all()
            )
            if not inside:
                continue
            noisy = pixels + rng.normal(0.0, 0.15, pixels.shape)
            corners.append(noisy.reshape(1, 4, 2).astype(np.float32))
            ids.append([marker_id])
            points.append(noisy)
        if len(corners) < 12:
            continue
        records.append(
            DetectionRecord(
                name=f"capture_{index:03d}.jpg",
                corners=corners,
                ids=np.asarray(ids, dtype=np.int32),
                marker_count=len(corners),
                pose=coverage_params(np.concatenate(points), image_size),
            )
        )
    assert len(records) >= 25

    result = run_double_sphere_model(
        output_dir=tmp_path,
        camera=CameraConfig(camera_name="synthetic_hero13", width=1920, height=1080),
        board_config=board_config,
        image_size=image_size,
        records=records,
        obj_by_id=obj_by_id,
    )

    assert result["ok"] is True, result
    assert result["rms"] < 1.0
    matrix = result["camera_matrix"]
    assert matrix[0][0] == pytest.approx(fx, rel=0.02)
    assert matrix[0][2] == pytest.approx(cx, abs=5.0)
    assert matrix[1][2] == pytest.approx(cy, abs=5.0)
    assert result["distortion"][1] == pytest.approx(alpha, abs=0.05)
    artifact = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
    assert artifact["intrinsic_type"] == "DOUBLE_SPHERE"
