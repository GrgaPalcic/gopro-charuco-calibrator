"""Kannala-Brandt output UMI can load: OpenICC FISHEYE parsing, UMI's own loader,
the projection helpers against cv2.fisheye, the ORB-SLAM3 block, and the solver's
routing, recommendation order and UMI check."""

import json
from pathlib import Path
from typing import Dict  # noqa: UP035 (kept as UMI wrote it)

import cv2
import numpy as np
import pytest
import yaml

from gopro_charuco_calibrator.boards import make_caib_board
from gopro_charuco_calibrator.coverage import PoseParams
from gopro_charuco_calibrator.models import BoardConfig, CameraConfig, SolverConfig
from gopro_charuco_calibrator.openicc import (
    OpenICCError,
    OpenICCSettings,
    orbslam3_kb8_yaml,
    run_openicc_model,
)
from gopro_charuco_calibrator.projection import (
    project_double_sphere,
    project_kannala_brandt,
    rays_to_angle,
    unproject_double_sphere,
)
from gopro_charuco_calibrator.solver import (
    DetectionRecord,
    recommended_model,
    solve_from_frames,
    umi_check,
)

# UMI's example/calibration/gopro_intrinsics_2_7k.json (real-stanford/
# universal_manipulation_interface), key for key; OpenICC writes FISHEYE results so.
UMI_GOPRO_INTRINSICS_2_7K = {
    "final_reproj_error": 0.2916398582648,
    "fps": 59.94005994005994,
    "image_height": 2028,
    "image_width": 2704,
    "intrinsic_type": "FISHEYE",
    "intrinsics": {
        "aspect_ratio": 1.0029788958491257,
        "focal_length": 796.8544625226342,
        "principal_pt_x": 1354.4265245977356,
        "principal_pt_y": 1011.4847310011687,
        "radial_distortion_1": -0.02196117964405394,
        "radial_distortion_2": -0.018959717016668237,
        "radial_distortion_3": 0.001693880829392453,
        "radial_distortion_4": -0.00016807228608000285,
        "skew": 0.0,
    },
    "nr_calib_images": 59,
    "stabelized": False,
}
UMI_KEYS = set(UMI_GOPRO_INTRINSICS_2_7K)
UMI_K = [UMI_GOPRO_INTRINSICS_2_7K["intrinsics"][f"radial_distortion_{i}"] for i in range(1, 5)]

# The HERO13 + Max Lens Mod 2.0 webcam-Wide Double Sphere solve measured on 2026-06-12.
HERO13_DS = (629.19, 629.19, 949.64, 538.89, 0.00166, 0.708)


# Verbatim from UMI umi/common/cv_util.py (commit c34ba9c, 2024-02-16), docstring
# example trimmed. This is how UMI loads the file we write.
def parse_fisheye_intrinsics(json_data: dict) -> Dict[str, np.ndarray]:  # noqa: UP006
    """
    Reads camera intrinsics from OpenCameraImuCalibration to opencv format.
    """
    assert json_data['intrinsic_type'] == 'FISHEYE'
    intr_data = json_data['intrinsics']

    # img size
    h = json_data['image_height']
    w = json_data['image_width']

    # pinhole parameters
    f = intr_data['focal_length']
    px = intr_data['principal_pt_x']
    py = intr_data['principal_pt_y']

    # Kannala-Brandt non-linear parameters for distortion
    kb8 = [
        intr_data['radial_distortion_1'],
        intr_data['radial_distortion_2'],
        intr_data['radial_distortion_3'],
        intr_data['radial_distortion_4']
    ]

    opencv_intr_dict = {
        'DIM': np.array([w, h], dtype=np.int64),
        'K': np.array([
            [f, 0, px],
            [0, f, py],
            [0, 0, 1]
        ], dtype=np.float64),
        'D': np.array([kb8]).T
    }
    return opencv_intr_dict


# --- OpenICC FISHEYE result -> our files ---


def _records(count: int = 12):
    board_config = BoardConfig(
        cols=5, rows=4, square_m=0.04, marker_m=0.028, start_id=20, marker_count=10
    )
    _board, obj_by_id = make_caib_board(board_config)
    records = []
    for index in range(count):
        corners, ids = [], []
        for marker_id, obj in obj_by_id.items():
            corners.append(obj[:, :2].reshape(1, 4, 2).astype(np.float32) * 1000 + index)
            ids.append([marker_id])
        records.append(
            DetectionRecord(
                name=f"capture_{index:03d}.jpg",
                corners=corners,
                ids=np.asarray(ids, dtype=np.int32),
                marker_count=len(corners),
                pose=PoseParams(0.5, 0.5, 0.3, 0.1),
            )
        )
    return board_config, obj_by_id, records


def _fake_runner(out_json, calls=None):
    def fake(work_dir: Path, _settings, camera_model="DOUBLE_SPHERE"):
        if calls is not None:
            calls.append((work_dir.name, camera_model))
        (work_dir / "out.json").write_text(json.dumps(out_json), encoding="utf-8")
        return json.loads(json.dumps(out_json))

    return fake


def _run_kb(tmp_path, monkeypatch, out_json, image_size, calls=None):
    board_config, obj_by_id, records = _records()
    monkeypatch.setattr(
        "gopro_charuco_calibrator.openicc.run_calibrate_camera", _fake_runner(out_json, calls)
    )
    return run_openicc_model(
        model="kannala_brandt",
        output_dir=tmp_path,
        camera=CameraConfig(camera_name="gripper"),
        board_config=board_config,
        image_size=image_size,
        records=records,
        obj_by_id=obj_by_id,
        settings=OpenICCSettings(),
    )


def test_fisheye_result_is_parsed_into_kannala_brandt(tmp_path, monkeypatch):
    calls = []
    size = (2704, 2028)  # UMI's 2.7K fixture; above 1080 rows, so solved downscaled
    result = _run_kb(tmp_path, monkeypatch, UMI_GOPRO_INTRINSICS_2_7K, size, calls)
    assert calls == [("openicc_kannala_brandt", "FISHEYE")]
    assert result["model"] == "kannala_brandt" and result["ok"] is True
    assert result["distortion_model"] == "kannala_brandt"
    assert result["distortion"] == pytest.approx(UMI_K)
    assert Path(result["json"]).name == "gripper_kannala_brandt.json"
    assert Path(result["orbslam3_yaml"]).name == "gripper_kannala_brandt_orbslam3.yaml"
    json.dumps(result)


def test_fisheye_4k_downscale_scales_back_pixels_not_k(tmp_path, monkeypatch):
    # OpenICC solves 4K at 1080p scale: focal, principal point and error come back x2,
    # k1..k4 are angles-to-angles and stay as they are.
    solved = json.loads(json.dumps(UMI_GOPRO_INTRINSICS_2_7K))
    solved.update(image_width=1920, image_height=1080)
    result = _run_kb(tmp_path, monkeypatch, solved, (3840, 2160))
    intr = UMI_GOPRO_INTRINSICS_2_7K["intrinsics"]
    matrix = result["camera_matrix"]
    assert matrix[0][0] == pytest.approx(intr["focal_length"] * 2)
    assert matrix[1][1] == pytest.approx(intr["focal_length"] * intr["aspect_ratio"] * 2)
    assert matrix[0][2] == pytest.approx(intr["principal_pt_x"] * 2)
    assert matrix[1][2] == pytest.approx(intr["principal_pt_y"] * 2)
    assert result["rms"] == pytest.approx(UMI_GOPRO_INTRINSICS_2_7K["final_reproj_error"] * 2)
    assert result["distortion"] == pytest.approx(UMI_K)

    written = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
    assert UMI_KEYS <= set(written)
    assert set(written) - UMI_KEYS == {"solve_downsample_factor"}
    assert set(written["intrinsics"]) == set(UMI_GOPRO_INTRINSICS_2_7K["intrinsics"])
    assert written["intrinsic_type"] == "FISHEYE"
    assert (written["image_width"], written["image_height"]) == (3840, 2160)
    assert written["solve_downsample_factor"] == pytest.approx(2.0)
    assert written["intrinsics"]["focal_length"] == pytest.approx(intr["focal_length"] * 2)
    assert written["intrinsics"]["aspect_ratio"] == intr["aspect_ratio"]
    for i in range(1, 5):
        key = f"radial_distortion_{i}"
        assert written["intrinsics"][key] == intr[key]


def test_fisheye_nonfinite_k_is_rejected(tmp_path, monkeypatch):
    bad = json.loads(json.dumps(UMI_GOPRO_INTRINSICS_2_7K))
    bad["intrinsics"]["radial_distortion_3"] = float("nan")
    with pytest.raises(OpenICCError, match="did not converge"):
        _run_kb(tmp_path, monkeypatch, bad, (1920, 1080))


def test_umi_loader_reads_our_file(tmp_path, monkeypatch):
    result = _run_kb(tmp_path, monkeypatch, UMI_GOPRO_INTRINSICS_2_7K, (2704, 2028))
    written = json.loads(Path(result["json"]).read_text())
    loaded = parse_fisheye_intrinsics(written)
    intr = written["intrinsics"]
    assert loaded["DIM"].tolist() == [2704, 2028]
    f, px, py = intr["focal_length"], intr["principal_pt_x"], intr["principal_pt_y"]
    np.testing.assert_allclose(loaded["K"], [[f, 0, px], [0, f, py], [0, 0, 1]])
    assert loaded["D"].shape == (4, 1)
    np.testing.assert_allclose(loaded["D"].ravel(), UMI_K)
    # UMI evaluates it with cv2.fisheye; the loaded arrays are what that takes.
    projected, _ = cv2.fisheye.projectPoints(
        np.asarray([[[0.0, 0.0, 1.0]]]), np.zeros(3), np.zeros(3), loaded["K"], loaded["D"]
    )
    np.testing.assert_allclose(projected.ravel(), [px, py])


# --- projection helpers ---


def test_cv2_fisheye_agrees_with_project_kannala_brandt_to_85_deg():
    fx, cx, cy = 796.85, 1354.43, 1011.48
    rays = rays_to_angle(85.0)
    ours = project_kannala_brandt(rays, fx, fx, cx, cy, UMI_K)
    matrix = np.asarray([[fx, 0.0, cx], [0.0, fx, cy], [0.0, 0.0, 1.0]])
    theirs, _ = cv2.fisheye.projectPoints(
        rays.reshape(-1, 1, 3), np.zeros(3), np.zeros(3), matrix, np.asarray(UMI_K)
    )
    assert np.abs(theirs.reshape(-1, 2) - ours).max() < 1e-6


def test_unproject_double_sphere_inverts_project():
    rays = rays_to_angle(95.0)  # past the 83.5 deg rim of the Max Lens Mod circle
    pixels, valid = project_double_sphere(rays, *HERO13_DS)
    assert valid.all()
    back, back_valid = unproject_double_sphere(pixels, *HERO13_DS)
    assert back_valid.all()
    np.testing.assert_allclose(back, rays, atol=1e-9)
    again, _ = project_double_sphere(back, *HERO13_DS)
    np.testing.assert_allclose(again, pixels, atol=1e-6)


def _fit_kb_to_hero13(max_angle_deg: float = 83.5):
    """Kannala-Brandt f, k1..k4 least-squares fitted to the HERO13 Double Sphere curve."""
    fx, _fy, _cx, _cy, xi, alpha = HERO13_DS
    theta = np.radians(np.linspace(0.01, max_angle_deg, 400))
    rays = np.stack([np.sin(theta), np.zeros_like(theta), np.cos(theta)], axis=1)
    radius = project_double_sphere(rays, fx, fx, 0.0, 0.0, xi, alpha)[0][:, 0]
    basis = np.stack([theta ** (2 * i + 1) for i in range(5)], axis=1)  # f*theta*(1+k.theta^2i)
    coeffs = np.linalg.lstsq(basis, radius, rcond=None)[0]
    worst = float(np.abs(basis @ coeffs - radius).max())
    return float(coeffs[0]), [float(c / coeffs[0]) for c in coeffs[1:]], worst


def test_kannala_brandt_can_represent_hero13_lens():
    # The four k's are not what limits KB on the 167 deg image: fitted straight to
    # the measured Double Sphere curve, they match it to well under a hundredth of
    # a pixel out to the 83.5 deg rim.
    _f, _k, worst = _fit_kb_to_hero13()
    assert worst < 0.01


# --- ORB-SLAM3 camera block ---


def test_orbslam3_yaml_fields(tmp_path, monkeypatch):
    result = _run_kb(tmp_path, monkeypatch, UMI_GOPRO_INTRINSICS_2_7K, (2704, 2028))
    text = Path(result["orbslam3_yaml"]).read_text(encoding="utf-8")
    assert text == orbslam3_kb8_yaml(result["openicc"])
    assert text.startswith("#") and "IMU" in text.splitlines()[1]
    fields = yaml.safe_load(text)
    intr = json.loads(Path(result["json"]).read_text())["intrinsics"]
    assert fields["Camera.type"] == "KannalaBrandt8"
    # fx = fy = focal_length, as UMI's loader does (aspect_ratio is dropped).
    assert fields["Camera1.fx"] == fields["Camera1.fy"] == intr["focal_length"]
    assert fields["Camera1.cx"] == intr["principal_pt_x"]
    assert fields["Camera1.cy"] == intr["principal_pt_y"]
    assert [fields[f"Camera1.k{i}"] for i in range(1, 5)] == UMI_K
    assert (fields["Camera.width"], fields["Camera.height"]) == (2704, 2028)
    assert fields["Camera.fps"] == 60
    assert not any(key.startswith("IMU") for key in fields)


# --- solver: routing, recommendation, UMI check ---


def _result(model, median=None, rms=None, ok=True):
    return {"model": model, "ok": ok, "median_view_error_px": median, "rms": rms}


def test_recommended_order_double_sphere_then_kannala_brandt_then_lowest():
    ds, kb = _result("double_sphere", 1.1, 1.1), _result("kannala_brandt", 1.0, 1.0)
    fisheye = _result("fisheye", 0.5, 0.6)
    assert recommended_model([fisheye, kb, ds]) == "double_sphere"
    assert recommended_model([fisheye, kb, {**ds, "ok": False}]) == "kannala_brandt"
    assert recommended_model([fisheye, {**kb, "ok": False}]) == "fisheye"


def _ds_result():
    fx, fy, cx, cy, xi, alpha = HERO13_DS
    return {
        "model": "double_sphere",
        "ok": True,
        "rms": 0.6,
        "median_view_error_px": 0.6,
        "camera_matrix": [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
        "distortion": [xi, alpha],
    }


def _kb_result(k=None, aspect=1.0):
    _fx, _fy, cx, cy, _xi, _alpha = HERO13_DS
    focal, fitted_k, _worst = _fit_kb_to_hero13()
    return {
        "model": "kannala_brandt",
        "ok": True,
        "rms": 0.6,
        "median_view_error_px": 0.6,
        "camera_matrix": [[focal, 0.0, cx], [0.0, focal * aspect, cy], [0.0, 0.0, 1.0]],
        "distortion": fitted_k if k is None else list(k),
        "openicc": {"intrinsics": {"aspect_ratio": aspect}},
    }


def _corner_records(max_angle_deg: float) -> list[DetectionRecord]:
    pixels, _ = project_double_sphere(rays_to_angle(max_angle_deg, 8, 16), *HERO13_DS)
    quads = pixels[: len(pixels) // 4 * 4].reshape(-1, 1, 4, 2).astype(np.float32)
    return [
        DetectionRecord(
            name="capture_001.jpg",
            corners=list(quads),
            ids=np.arange(len(quads), dtype=np.int32).reshape(-1, 1),
            marker_count=len(quads),
            pose=PoseParams(0.5, 0.5, 0.3, 0.1),
        )
    ]


def test_umi_check_matching_models_has_no_warnings():
    check = umi_check(_kb_result(), _ds_result(), _corner_records(80.0))
    assert check["board_reach_deg"] == pytest.approx(80.0, abs=1e-3)
    assert check["max_diff_vs_double_sphere_px"] < 0.05
    assert check["aspect_ratio"] == 1.0
    assert check["warnings"] == []
    json.dumps(check)


def test_umi_check_warns_on_aspect_and_mismatch():
    check = umi_check(_kb_result((0.0, 0.0, 0.0, 0.0), aspect=1.01), _ds_result(),
                      _corner_records(70.0))
    assert check["max_diff_vs_double_sphere_px"] > 1.0
    assert len(check["warnings"]) == 2
    assert "square" in check["warnings"][0] and "1.0100" in check["warnings"][0]
    assert "70°" in check["warnings"][1]
    # Just inside the aspect tolerance: no aspect warning.
    close = umi_check(_kb_result(aspect=1.004), _ds_result(), _corner_records(70.0))
    assert not any("square" in warning for warning in close["warnings"])


def test_umi_check_without_double_sphere():
    check = umi_check(_kb_result(), None, _corner_records(70.0))
    assert check == {
        "board_reach_deg": None,
        "max_diff_vs_double_sphere_px": None,
        "aspect_ratio": 1.0,
        "warnings": [],
    }


def _solve(monkeypatch, tmp_path, models, openicc_runner, records=None):
    records = records or _corner_records(75.0) * 25
    monkeypatch.setattr(
        "gopro_charuco_calibrator.solver.detect_frames",
        lambda **_kwargs: ((1920, 1080), records),
    )
    monkeypatch.setattr(
        "gopro_charuco_calibrator.solver.make_caib_board",
        lambda *_args, **_kwargs: (object(), {}),
    )
    monkeypatch.setattr("gopro_charuco_calibrator.solver.run_openicc_model", openicc_runner)
    monkeypatch.setattr(
        "gopro_charuco_calibrator.solver.run_model",
        lambda **kwargs: _result(kwargs["model"].name, 0.4, 0.5),
    )
    return solve_from_frames(
        frames_dir=tmp_path / "frames",
        output_dir=tmp_path / "out",
        camera=CameraConfig(),
        board_config=BoardConfig(),
        solver=SolverConfig(models=models),
    )


def test_solver_routes_openicc_models_and_attaches_umi_check(monkeypatch, tmp_path):
    calls = []

    def runner(*, model, **_kwargs):
        calls.append(model)
        return _ds_result() if model == "double_sphere" else _kb_result()

    summary = _solve(
        monkeypatch, tmp_path, ["double_sphere", "kannala_brandt", "fisheye"], runner
    )
    assert calls == ["double_sphere", "kannala_brandt"]
    by_model = {result["model"]: result for result in summary["results"]}
    assert list(by_model) == ["double_sphere", "kannala_brandt", "fisheye"]
    assert summary["recommended_model"] == "double_sphere"
    assert [r["recommended"] for r in summary["results"]] == [True, False, False]
    check = by_model["kannala_brandt"]["umi_check"]
    assert set(check) == {
        "board_reach_deg", "max_diff_vs_double_sphere_px", "aspect_ratio", "warnings"
    }
    assert check["board_reach_deg"] == pytest.approx(75.0, abs=1e-3)
    assert check["max_diff_vs_double_sphere_px"] < 0.05
    assert "umi_check" not in by_model["double_sphere"]
    summary_path = tmp_path / "out" / "caib_marker_board_calibration_summary.json"
    saved = json.loads(summary_path.read_text(encoding="utf-8"))
    assert saved["results"][1]["umi_check"] == check


def test_solver_kannala_brandt_alone_and_failures(monkeypatch, tmp_path):
    def runner(*, model, **_kwargs):
        if model == "double_sphere":
            raise OpenICCError("OpenICC double_sphere needs at least 10 views")
        return _kb_result()

    summary = _solve(monkeypatch, tmp_path, ["double_sphere", "kannala_brandt"], runner)
    ds, kb = summary["results"]
    assert ds["ok"] is False and ds["error_type"] == "OpenICCError"
    assert summary["recommended_model"] == "kannala_brandt"
    assert kb["umi_check"]["board_reach_deg"] is None
    assert kb["umi_check"]["max_diff_vs_double_sphere_px"] is None

    def kb_fails(*, model, **_kwargs):
        if model == "kannala_brandt":
            raise OpenICCError("OpenICC did not converge")
        return _ds_result()

    summary = _solve(monkeypatch, tmp_path, ["double_sphere", "kannala_brandt"], kb_fails)
    ds, kb = summary["results"]
    assert kb["ok"] is False and kb["model"] == "kannala_brandt" and "umi_check" not in kb
    assert summary["recommended_model"] == "double_sphere"


def test_gripper_preset_solves_all_three_models():
    from gopro_charuco_calibrator import presets

    _title, config = presets.get_preset("gopro13_umi_gripper_fisheye_1080p")
    assert config.solver.models == ["double_sphere", "kannala_brandt", "fisheye"]
