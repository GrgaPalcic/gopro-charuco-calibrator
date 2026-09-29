import json
import struct
import subprocess
from pathlib import Path

import numpy as np
import pytest

from gopro_charuco_calibrator.boards import make_caib_board
from gopro_charuco_calibrator.coverage import PoseParams
from gopro_charuco_calibrator.models import BoardConfig, CameraConfig, SolverConfig
from gopro_charuco_calibrator.openicc import (
    OPENICC_MIN_VIEWS,
    OpenICCError,
    OpenICCSettings,
    build_command,
    build_corners_payload,
    intrinsics_to_camera_matrix,
    run_calibrate_camera,
    run_double_sphere_model,
    scene_point_id,
    settings_from_env,
    ubjson_dumps,
)
from gopro_charuco_calibrator.solver import DetectionRecord, solve_from_frames

# --- minimal UBJSON decoder for round-trip testing (test-only) ---


def _ubjson_load(buf: bytes, pos: int = 0):
    marker = buf[pos : pos + 1]
    pos += 1
    if marker == b"Z":
        return None, pos
    if marker == b"T":
        return True, pos
    if marker == b"F":
        return False, pos
    if marker in (b"i", b"U", b"I", b"l", b"L"):
        widths = {
            b"i": (">b", 1), b"U": (">B", 1), b"I": (">h", 2), b"l": (">i", 4), b"L": (">q", 8),
        }
        fmt, size = widths[marker]
        return struct.unpack(fmt, buf[pos : pos + size])[0], pos + size
    if marker == b"D":
        return struct.unpack(">d", buf[pos : pos + 8])[0], pos + 8
    if marker == b"S":
        length, pos = _ubjson_load(buf, pos)
        return buf[pos : pos + length].decode("utf-8"), pos + length
    if marker == b"[":
        items = []
        while buf[pos : pos + 1] != b"]":
            value, pos = _ubjson_load(buf, pos)
            items.append(value)
        return items, pos + 1
    if marker == b"{":
        obj = {}
        while buf[pos : pos + 1] != b"}":
            length, pos = _ubjson_load(buf, pos)
            key = buf[pos : pos + length].decode("utf-8")
            pos += length
            obj[key], pos = _ubjson_load(buf, pos)
        return obj, pos + 1
    raise AssertionError(f"unknown marker {marker!r} at {pos - 1}")


def ubjson_loads(buf: bytes):
    value, pos = _ubjson_load(buf, 0)
    assert pos == len(buf)
    return value


def test_ubjson_golden_bytes():
    encoded = ubjson_dumps({"a": 1, "b": [True, None, "hi"]})
    assert encoded == (
        b"{"
        + b"i\x01a" + b"i\x01"
        + b"i\x01b" + b"[" + b"T" + b"Z" + b"Si\x02hi" + b"]"
        + b"}"
    )
    assert ubjson_dumps(1.5) == b"D" + struct.pack(">d", 1.5)


def test_ubjson_int_widths_and_types():
    assert ubjson_dumps(127)[0:1] == b"i"
    assert ubjson_dumps(-128)[0:1] == b"i"
    assert ubjson_dumps(255)[0:1] == b"U"
    assert ubjson_dumps(32767)[0:1] == b"I"
    assert ubjson_dumps(2**31 - 1)[0:1] == b"l"
    assert ubjson_dumps(2**40)[0:1] == b"L"
    assert ubjson_dumps(True) == b"T"  # bool before int
    assert ubjson_dumps(np.float64(2.0)) == ubjson_dumps(2.0)
    assert ubjson_dumps(np.int32(7)) == ubjson_dumps(7)
    with pytest.raises(TypeError):
        ubjson_dumps(object())


def test_ubjson_roundtrip():
    fixture = {
        "calibration_board_type": 0,
        "square_size_meter": 0.021,
        "views": {"33333": {"image_points": {"0": [12.5, -3.0], "141": [0.0, 720.0]}}},
        "flags": [True, False, None, "x"],
        "big": 2**40,
    }
    assert ubjson_loads(ubjson_dumps(fixture)) == fixture


# --- corners payload ---


def _small_board() -> BoardConfig:
    return BoardConfig(
        cols=5, rows=4, square_m=0.04, marker_m=0.028, start_id=20, marker_count=10
    )


def _records_from_board(board_config: BoardConfig, count: int) -> tuple[list, dict]:
    _board, obj_by_id = make_caib_board(board_config)
    records = []
    for index in range(count):
        corners = []
        ids = []
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
    return records, obj_by_id


def test_corners_payload_ids_and_geometry():
    board_config = _small_board()
    records, obj_by_id = _records_from_board(board_config, 12)
    payload = build_corners_payload(
        records=records,
        obj_by_id=obj_by_id,
        image_size=(1920, 1080),
        square_size_m=board_config.square_m,
        fps=30.0,
    )
    assert payload["image_width"] == 1920 and payload["image_height"] == 1080
    assert payload["camera_fps"] == 30.0
    assert payload["square_size_meter"] == board_config.square_m
    assert payload["calibration_board_type"] == 0
    # every image-point id must exist in scene_pts (OpenICC hard-crashes otherwise)
    for view in payload["views"].values():
        for point_id in view["image_points"]:
            assert point_id in payload["scene_pts"]
    # scene point geometry matches obj_by_id under the marker_id*4+k scheme
    for marker_id, obj in obj_by_id.items():
        for k in range(4):
            assert payload["scene_pts"][str(scene_point_id(marker_id, k))] == pytest.approx(
                list(map(float, obj[k]))
            )
    # synthetic timestamps unique + monotonic
    stamps = [int(ts) for ts in payload["views"]]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert ubjson_dumps(payload)  # encodable


def test_corners_payload_too_few_views():
    board_config = _small_board()
    records, obj_by_id = _records_from_board(board_config, OPENICC_MIN_VIEWS - 1)
    with pytest.raises(OpenICCError, match=str(OPENICC_MIN_VIEWS)):
        build_corners_payload(
            records=records,
            obj_by_id=obj_by_id,
            image_size=(1920, 1080),
            square_size_m=board_config.square_m,
            fps=30.0,
        )


# --- settings + command ---


def test_settings_from_env():
    settings = settings_from_env(
        {
            "OPENICC_BINARY": "/opt/calibrate_camera",
            "OPENICC_GRID_SIZE": "0.05",
            "OPENICC_TIMEOUT_S": "120",
        }
    )
    assert settings.binary_path == "/opt/calibrate_camera"
    assert settings.grid_size == 0.05
    assert settings.timeout_s == 120
    assert settings_from_env({}).docker_image == "openicc"
    assert settings_from_env({"OPENICC_DOCKER_ROOT": "1"}).docker_run_as_user is False


def test_build_command_docker_and_native(tmp_path):
    docker_cmd = build_command(tmp_path, OpenICCSettings(), "openicc-test")
    assert docker_cmd[:3] == ["docker", "run", "--rm"]
    assert "--name" in docker_cmd and "openicc-test" in docker_cmd
    assert any(arg.startswith("--user") or ":" in arg for arg in docker_cmd)
    assert f"{tmp_path.resolve()}:/data" in docker_cmd
    assert "--camera_model_to_calibrate=DOUBLE_SPHERE" in docker_cmd
    native_cmd = build_command(tmp_path, OpenICCSettings(binary_path="/opt/cc"), None)
    assert native_cmd[0] == "/opt/cc"
    assert f"--input_corners={tmp_path / 'corners.uson'}" in native_cmd


# --- runner failure paths (mocked subprocess) ---


def test_run_docker_missing(tmp_path, monkeypatch):
    def raise_missing(*_args, **_kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", raise_missing)
    with pytest.raises(OpenICCError, match="OPENICC_BINARY"):
        run_calibrate_camera(tmp_path, OpenICCSettings())


def test_run_nonzero_exit_surfaces_stderr(tmp_path, monkeypatch):
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd, 125, stdout="", stderr="Cannot connect to the Docker daemon"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(OpenICCError, match="Docker daemon"):
        run_calibrate_camera(tmp_path, OpenICCSettings())
    assert "Docker daemon" in (tmp_path / "calibrate_camera.log").read_text()


def test_run_timeout_cleans_container(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "docker" and cmd[1] == "run":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(OpenICCError, match="timed out"):
        run_calibrate_camera(tmp_path, OpenICCSettings(timeout_s=1))
    assert any(cmd[:3] == ["docker", "rm", "-f"] for cmd in calls)


# --- orchestrator ---

OUT_JSON = {
    "final_reproj_error": 0.617,
    "fps": 30.0,
    "image_height": 1080,
    "image_width": 1920,
    "intrinsic_type": "DOUBLE_SPHERE",
    "intrinsics": {
        "alpha": 0.708,
        "aspect_ratio": 0.9998,
        "focal_length": 629.19,
        "principal_pt_x": 949.64,
        "principal_pt_y": 538.89,
        "skew": 0.0,
        "xi": 0.00166,
    },
    "nr_calib_images": 37,
    "stabelized": False,
}


def _patched_runner(out_json):
    def fake_runner(work_dir: Path, _settings, _camera_model="DOUBLE_SPHERE"):
        (work_dir / "out.json").write_text(json.dumps(out_json), encoding="utf-8")
        return out_json

    return fake_runner


def test_run_double_sphere_model_success(tmp_path, monkeypatch):
    board_config = _small_board()
    records, obj_by_id = _records_from_board(board_config, 12)
    monkeypatch.setattr(
        "gopro_charuco_calibrator.openicc.run_calibrate_camera", _patched_runner(OUT_JSON)
    )
    result = run_double_sphere_model(
        output_dir=tmp_path,
        camera=CameraConfig(camera_name="testcam"),
        board_config=board_config,
        image_size=(1920, 1080),
        records=records,
        obj_by_id=obj_by_id,
        settings=OpenICCSettings(),
    )
    assert result["model"] == "double_sphere" and result["ok"] is True
    assert result["rms"] == pytest.approx(0.617)
    assert result["median_view_error_px"] == pytest.approx(0.617)
    assert result["worst_view_error_px"] is None
    assert result["distortion"] == [pytest.approx(0.00166), pytest.approx(0.708)]
    matrix = result["camera_matrix"]
    assert matrix[0][0] == pytest.approx(629.19)
    assert matrix[1][1] == pytest.approx(629.19 * 0.9998)
    assert matrix[0][2] == pytest.approx(949.64) and matrix[1][2] == pytest.approx(538.89)
    assert result["yaml"] is None
    assert result["selected"]["frame_count"] == 37
    assert result["all_frames"]["frame_count"] == 12
    artifact = Path(result["json"])
    assert artifact.name == "testcam_double_sphere.json"
    assert json.loads(artifact.read_text())["final_reproj_error"] == pytest.approx(0.617)
    json.dumps(result)  # whole dict must be JSON-safe


def test_run_double_sphere_model_nonfinite(tmp_path, monkeypatch):
    bad = json.loads(json.dumps(OUT_JSON))
    bad["final_reproj_error"] = float("nan")
    monkeypatch.setattr(
        "gopro_charuco_calibrator.openicc.run_calibrate_camera", _patched_runner(bad)
    )
    board_config = _small_board()
    records, obj_by_id = _records_from_board(board_config, 12)
    with pytest.raises(OpenICCError, match="did not converge"):
        run_double_sphere_model(
            output_dir=tmp_path,
            camera=CameraConfig(),
            board_config=board_config,
            image_size=(1920, 1080),
            records=records,
            obj_by_id=obj_by_id,
            settings=OpenICCSettings(),
        )


def test_intrinsics_to_camera_matrix_defaults():
    matrix = intrinsics_to_camera_matrix(
        {"focal_length": 100.0, "principal_pt_x": 10.0, "principal_pt_y": 20.0}
    )
    assert matrix == [[100.0, 0.0, 10.0], [0.0, 100.0, 20.0], [0.0, 0.0, 1.0]]


# --- solve_from_frames wiring ---


def _solve_with_patched(monkeypatch, tmp_path, ds_behavior):
    records = [
        DetectionRecord(
            name=f"capture_{index:03d}.jpg",
            corners=[],
            ids=np.empty((0, 1), dtype=np.int32),
            marker_count=12,
            pose=PoseParams(0.4, 0.5, 0.3, 0.1),
        )
        for index in range(1, 26)
    ]
    monkeypatch.setattr(
        "gopro_charuco_calibrator.solver.detect_frames",
        lambda **_kwargs: ((1920, 1080), records),
    )
    monkeypatch.setattr(
        "gopro_charuco_calibrator.solver.make_caib_board",
        lambda *_args, **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(
        "gopro_charuco_calibrator.solver.run_openicc_model", ds_behavior
    )
    return solve_from_frames(
        frames_dir=tmp_path / "frames",
        output_dir=tmp_path / "out",
        camera=CameraConfig(),
        board_config=BoardConfig(),
        solver=SolverConfig(models=["double_sphere"]),
    )


def test_solve_from_frames_double_sphere_failure(monkeypatch, tmp_path):
    def fail(**_kwargs):
        raise OpenICCError("'docker' not found — set OPENICC_BINARY")

    summary = _solve_with_patched(monkeypatch, tmp_path, fail)
    (result,) = summary["results"]
    assert result["ok"] is False
    assert result["model"] == "double_sphere"
    assert result["error_type"] == "OpenICCError"
    assert "OPENICC_BINARY" in result["error"]


def test_solve_from_frames_double_sphere_success(monkeypatch, tmp_path):
    canned = {"model": "double_sphere", "ok": True, "rms": 0.6, "median_view_error_px": 0.6}

    summary = _solve_with_patched(monkeypatch, tmp_path, lambda **_kwargs: canned)
    assert summary["results"] == [canned]


# --- caib layout parity auto-detection ---


def test_detect_board_layout_picks_flipped():
    import cv2

    from gopro_charuco_calibrator.boards import make_caib_board
    from gopro_charuco_calibrator.solver import detect_board_layout

    board_config = BoardConfig(
        cols=10, rows=7, square_m=0.021, marker_m=0.015,
        aruco_dict="DICT_4X4_50", start_id=0, marker_count=35,
    )
    _board, flipped_obj = make_caib_board(board_config, flipped=True)
    k_true = np.asarray([[900.0, 0.0, 960.0], [0.0, 900.0, 540.0], [0.0, 0.0, 1.0]])
    records = []
    for index in range(6):
        rvec = np.asarray([0.1 * index, -0.05 * index, 0.02], dtype=np.float64)
        tvec = np.asarray([-0.1, -0.07, 0.5 + 0.05 * index], dtype=np.float64)
        corners = []
        ids = []
        for marker_id, obj in flipped_obj.items():
            projected, _ = cv2.projectPoints(
                obj.astype(np.float64), rvec, tvec, k_true, np.zeros(5)
            )
            corners.append(projected.reshape(1, 4, 2).astype(np.float32))
            ids.append([marker_id])
        records.append(
            DetectionRecord(
                name=f"v{index}.jpg",
                corners=corners,
                ids=np.asarray(ids, dtype=np.int32),
                marker_count=len(corners),
                pose=PoseParams(0.5, 0.5, 0.3, 0.1),
            )
        )
    _board, obj_by_id, layout = detect_board_layout(records, board_config)
    assert layout == "flipped"
    assert obj_by_id.keys() == flipped_obj.keys()
    assert np.allclose(obj_by_id[0], flipped_obj[0])


def test_run_double_sphere_model_4k_downscale(tmp_path, monkeypatch):
    board_config = _small_board()
    records, obj_by_id = _records_from_board(board_config, 12)
    monkeypatch.setattr(
        "gopro_charuco_calibrator.openicc.run_calibrate_camera", _patched_runner(OUT_JSON)
    )
    result = run_double_sphere_model(
        output_dir=tmp_path,
        camera=CameraConfig(camera_name="fourk"),
        board_config=board_config,
        image_size=(3840, 2160),
        records=records,
        obj_by_id=obj_by_id,
        settings=OpenICCSettings(),
    )
    # intrinsics + error scaled back to native 4K (factor 2)
    assert result["camera_matrix"][0][0] == pytest.approx(629.19 * 2)
    assert result["camera_matrix"][0][2] == pytest.approx(949.64 * 2)
    assert result["rms"] == pytest.approx(0.617 * 2)
    artifact = json.loads(Path(result["json"]).read_text())
    assert artifact["image_width"] == 3840 and artifact["image_height"] == 2160
    assert artifact["solve_downsample_factor"] == pytest.approx(2.0)
    # the corners file written for the solver must be at <=1080p scale
    corners = ubjson_loads((tmp_path / "openicc" / "corners.uson").read_bytes())
    assert corners["image_height"] == 1080
