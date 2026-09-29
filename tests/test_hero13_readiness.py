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
    DEFAULT_SOURCE_DIR,
    OPENICC_COMMIT,
    OPENICC_PATCHES,
    OPENICC_REPO,
    OpenICCError,
    OpenICCSettings,
    apply_source_patches,
    build_image_commands,
    run_calibrate_camera,
    run_double_sphere_model,
    run_openicc_model,
    settings_from_env,
)
from gopro_charuco_calibrator.projection import (
    board_reach_deg,
    kb_vs_double_sphere_px,
    project_double_sphere,
    rays_to_angle,
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
                ["docker", "image", "inspect", settings_from_env().docker_image],
                capture_output=True,
                timeout=15,
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


# Ground truth = the HERO13 + Max Lens Mod 2.0 webcam-Wide solve measured on
# 2026-06-12 (0.617 px, alpha 0.708): fx = fy, cx, cy, xi, alpha.
HERO13_DS = (629.19, 949.64, 538.89, 0.00166, 0.708)
HERO13_DS_RESULT = {
    "camera_matrix": [[629.19, 0.0, 949.64], [0.0, 629.19, 538.89], [0.0, 0.0, 1.0]],
    "distortion": [0.00166, 0.708],
}


# The Max Lens Mod image is a ~167 deg circle: nothing is seen beyond 83.5 deg off-axis.
HERO13_CIRCLE_DEG = 83.5


def _hero13_scene(edge_views: int = 0):
    """The measured HERO13 Double Sphere lens rendered for a wide spread of board poses.

    The 60 base poses rarely reach past ~64 deg off-axis. ``edge_views`` adds views
    with the board out towards the edge of the 167 deg circle, turned partly to
    face the camera, as someone sweeping the board round the rim would hold it.
    """
    fx, cx, cy, xi, alpha = HERO13_DS
    image_size = (1920, 1080)
    board_config = BoardConfig()
    _board, obj_by_id = make_caib_board(board_config)
    center = np.asarray([board_config.pattern_width_m / 2, board_config.pattern_height_m / 2, 0])
    circle_px = project_double_sphere(
        np.asarray([[np.sin(np.radians(HERO13_CIRCLE_DEG)), 0.0,
                     np.cos(np.radians(HERO13_CIRCLE_DEG))]]),
        fx, fx, 0.0, 0.0, xi, alpha,
    )[0][0, 0]

    def render(name, rotation, tvec, rng):
        corners, ids, points = [], [], []
        for marker_id, obj in obj_by_id.items():
            camera_points = (rotation @ (obj - center).T).T + tvec
            pixels, valid = project_double_sphere(camera_points, fx, fx, cx, cy, xi, alpha)
            inside = (
                valid.all()
                and (pixels[:, 0] > 2).all()
                and (pixels[:, 0] < image_size[0] - 2).all()
                and (pixels[:, 1] > 2).all()
                and (pixels[:, 1] < image_size[1] - 2).all()
                and (np.hypot(pixels[:, 0] - cx, pixels[:, 1] - cy) < circle_px).all()
            )
            if not inside:
                continue
            noisy = pixels + rng.normal(0.0, 0.15, pixels.shape)
            corners.append(noisy.reshape(1, 4, 2).astype(np.float32))
            ids.append([marker_id])
            points.append(noisy)
        if len(corners) < 12:
            return None
        return DetectionRecord(
            name=name,
            corners=corners,
            ids=np.asarray(ids, dtype=np.int32),
            marker_count=len(corners),
            pose=coverage_params(np.concatenate(points), image_size),
        )

    records = []
    rng = np.random.default_rng(7)
    for index in range(60):
        rvec = rng.uniform([-0.7, -0.7, -0.5], [0.7, 0.7, 0.5])
        tvec = rng.uniform([-0.45, -0.25, 0.28], [0.45, 0.25, 0.65])
        records.append(render(f"capture_{index:03d}.jpg", _rodrigues(rvec), tvec, rng))

    edge_rng = np.random.default_rng(11)
    for index in range(edge_views):
        off_axis = np.radians(edge_rng.uniform(55.0, 80.0))
        # Mostly left and right: the 1080 px height cuts the circle at ~46 deg.
        azimuth = np.radians(edge_rng.choice([0.0, 180.0]) + edge_rng.uniform(-30.0, 30.0))
        direction = np.asarray(
            [np.sin(off_axis) * np.cos(azimuth), np.sin(off_axis) * np.sin(azimuth),
             np.cos(off_axis)]
        )
        axis = np.cross([0.0, 0.0, 1.0], direction)
        facing = axis / np.linalg.norm(axis) * off_axis * edge_rng.uniform(0.4, 0.9)
        rotation = _rodrigues(facing) @ _rodrigues(edge_rng.uniform(-0.3, 0.3, 3))
        tvec = direction * edge_rng.uniform(0.3, 0.55)
        records.append(render(f"capture_{60 + index:03d}.jpg", rotation, tvec, edge_rng))

    records = [record for record in records if record is not None]
    assert len(records) >= 25
    return image_size, board_config, obj_by_id, records


@pytest.mark.skipif(not _openicc_image_present(), reason="needs the openicc docker image")
def test_double_sphere_recovers_measured_hero13_intrinsics(tmp_path):
    fx, cx, cy, xi, alpha = HERO13_DS
    image_size, board_config, obj_by_id, records = _hero13_scene()
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
    assert matrix[0][2] == pytest.approx(cx, abs=5.0)
    assert matrix[1][2] == pytest.approx(cy, abs=5.0)
    # Compare the models, not the numbers: in Double Sphere f, xi and alpha trade
    # off against each other, and OpenICC is not deterministic, so on this exact
    # data f landed anywhere in 573-626 px over ten runs (2026-09-29) at the same RMS.
    # What must hold is that the same 3D rays land on the same pixels. Out to 70 deg:
    # 0.17-0.24 px over 5 runs with the patched image (OPENICC_PATCHES), against
    # 0.57-2.68 px unpatched, where the distortion stayed fitted around the image centre
    # (2026-09-29).
    rays = rays_to_angle(70.0)
    expected, _ = project_double_sphere(rays, fx, fx, cx, cy, xi, alpha)
    recovered, valid = project_double_sphere(
        rays, matrix[0][0], matrix[1][1], matrix[0][2], matrix[1][2], *result["distortion"]
    )
    assert valid.all()
    assert np.abs(recovered - expected).max() < 1.0
    artifact = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
    assert artifact["intrinsic_type"] == "DOUBLE_SPHERE"


def _solve_kannala_brandt_against_truth(tmp_path, edge_views: int = 0):
    image_size, board_config, obj_by_id, records = _hero13_scene(edge_views)
    result = run_openicc_model(
        model="kannala_brandt",
        output_dir=tmp_path,
        camera=CameraConfig(camera_name="synthetic_hero13", width=1920, height=1080),
        board_config=board_config,
        image_size=image_size,
        records=records,
        obj_by_id=obj_by_id,
    )
    assert result["ok"] is True, result
    corners = np.concatenate(
        [np.asarray(c, dtype=np.float64).reshape(-1, 2) for r in records for c in r.corners]
    )
    reach = board_reach_deg(HERO13_DS_RESULT, corners)
    # Worst pixel distance to the TRUE lens over rays out to each angle, with the KB
    # side evaluated as UMI loads it (fy = fx).
    diffs = {
        angle: kb_vs_double_sphere_px(result, HERO13_DS_RESULT, angle)
        for angle in (60.0, 70.0, 80.0, reach)
    }
    print(
        f"\nKB vs true DS ({len(records)} views): rms {result['rms']:.3f} px, board reach "
        f"{reach:.1f} deg, aspect {result['openicc']['intrinsics']['aspect_ratio']:.5f}, "
        + ", ".join(f"<= {angle:.1f} deg {diff:.2f} px" for angle, diff in diffs.items())
    )
    assert result["rms"] < 1.0
    artifact = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
    assert artifact["intrinsic_type"] == "FISHEYE"
    assert Path(result["orbslam3_yaml"]).is_file()
    return reach, diffs


@pytest.mark.skipif(not _openicc_image_present(), reason="needs the openicc docker image")
def test_kannala_brandt_matches_true_double_sphere_on_hero13(tmp_path):
    # Does OpenICC's Kannala-Brandt (FISHEYE, the file UMI loads) hold on the ~167 deg
    # Max Lens Mod image? Solved on the same Double Sphere rendered scene as above,
    # with the patched image (OPENICC_PATCHES), 5 runs on 2026-09-29: board reach
    # 64.4 deg; worst distance to the true lens out to that reach 0.23-0.44 px, out to
    # 70 deg 0.54-0.99 px (past the board, so extrapolated). Unpatched it was 0.49-0.84
    # and 0.58-1.63 px over 19 runs.
    reach, diffs = _solve_kannala_brandt_against_truth(tmp_path)
    assert reach == pytest.approx(64.4, abs=0.5)
    assert diffs[reach] < 1.0
    assert diffs[70.0] < 2.0


@pytest.mark.skipif(not _openicc_image_present(), reason="needs the openicc docker image")
def test_kannala_brandt_solves_with_views_at_the_rim(tmp_path):
    # With 30 more views out to the 83.5 deg rim, the patched image (OPENICC_PATCHES)
    # put KB 0.11-0.32 px from the true lens out to the rim over 5 runs (2026-09-29).
    # Unpatched, the distortion stayed fitted around the image centre and KB landed
    # 0.9-4.8 px off (18 runs) at the same noise-level rms. The four KB coefficients are
    # not the limit: they fit this lens's curve to 0.0002 px out to 83.5 deg
    # (test_kannala_brandt_can_represent_hero13_lens).
    reach, diffs = _solve_kannala_brandt_against_truth(tmp_path, edge_views=30)
    assert reach > 83.0
    assert diffs[reach] < 1.0


@pytest.mark.skipif(not _openicc_image_present(), reason="needs the openicc docker image")
def test_double_sphere_solves_with_views_at_the_rim(tmp_path):
    # The same rim scene through Double Sphere: 0.09-0.32 px from the true lens out to
    # the 83.5 deg rim over 5 runs with the patched image (2026-09-29); unpatched it was
    # 0.2-3.9 px (10 runs).
    fx, cx, cy, xi, alpha = HERO13_DS
    image_size, board_config, obj_by_id, records = _hero13_scene(edge_views=30)
    result = run_double_sphere_model(
        output_dir=tmp_path,
        camera=CameraConfig(camera_name="synthetic_hero13", width=1920, height=1080),
        board_config=board_config,
        image_size=image_size,
        records=records,
        obj_by_id=obj_by_id,
    )
    assert result["ok"] is True, result
    rays = rays_to_angle(HERO13_CIRCLE_DEG)
    expected, _ = project_double_sphere(rays, fx, fx, cx, cy, xi, alpha)
    matrix = result["camera_matrix"]
    recovered, valid = project_double_sphere(
        rays, matrix[0][0], matrix[1][1], matrix[0][2], matrix[1][2], *result["distortion"]
    )
    assert valid.all()
    assert np.linalg.norm(recovered - expected, axis=1).max() < 1.0


def test_source_patch_frees_the_distortion_in_the_final_adjustment(tmp_path):
    relative, old, new = OPENICC_PATCHES[0]
    source = tmp_path / relative
    source.parent.mkdir(parents=True)
    source.write_text(f"// stage 3\n{old}\n", encoding="utf-8")
    apply_source_patches(tmp_path)
    patched = source.read_text(encoding="utf-8")
    assert new in patched and old not in patched
    apply_source_patches(tmp_path)  # a rebuild re-applies it without complaint
    assert source.read_text(encoding="utf-8") == patched
    source.write_text("// some other OpenICC version\n", encoding="utf-8")
    with pytest.raises(OpenICCError, match="setup-openicc"):
        apply_source_patches(tmp_path)


def test_source_patch_matches_the_pinned_checkout():
    # When the pinned source has been fetched (setup-openicc ran), the patch must apply to it.
    checkout = DEFAULT_SOURCE_DIR.expanduser()
    relative, old, new = OPENICC_PATCHES[0]
    if not (checkout / relative).is_file():
        pytest.skip("OpenICC source not fetched on this machine")
    text = (checkout / relative).read_text(encoding="utf-8")
    assert text.count(old) == 1 or new in text
