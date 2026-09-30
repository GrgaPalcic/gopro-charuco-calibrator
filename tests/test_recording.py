"""The "From a recording" route: clip check, frame selection on a synthetic clip, the
Labs codes, the ORB-SLAM3 block size, and the job end to end through the HTTP API."""

import base64
import json
import os
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml
from conftest import POSES, build_clip, needs_ffmpeg
from fastapi.testclient import TestClient

from gopro_charuco_calibrator import app as app_module
from gopro_charuco_calibrator import presets, recording
from gopro_charuco_calibrator.clipcheck import check_clip, compare, probe_video
from gopro_charuco_calibrator.coverage import PoseParams, pose_distance
from gopro_charuco_calibrator.gopro import camera_state_warnings
from gopro_charuco_calibrator.labs import checklist, labs_command, labs_payload, unverified_codes
from gopro_charuco_calibrator.models import (
    AppConfig,
    CaptureConfig,
    GoProSettingsConfig,
    RecordingConfig,
    SolverConfig,
)
from gopro_charuco_calibrator.openicc import DEFAULT_DOCKER_IMAGE, orbslam3_kb8_yaml
from gopro_charuco_calibrator.recording import (
    RecordingError,
    RecordingJob,
    camera_name_from_serial,
    extract_views,
    motion_limit_px,
    recording_guide,
    sanitise_clip_name,
)

# --- config and presets ------------------------------------------------------


def test_recording_config_defaults_are_the_dataset_mode():
    rec = RecordingConfig()
    assert (rec.width, rec.height, rec.fps, rec.lens, rec.hypersmooth) == (
        4000, 3000, 60.0, "Ultra Wide", "off"
    )
    assert rec.shutter_s == pytest.approx(1 / 480)
    assert rec.labs_lens_mod_code == "oX2"
    assert RecordingConfig(lens_mod="AEWAL-001").labs_lens_mod_code == "oX10"
    assert (rec.orbslam3_width, rec.orbslam3_height) == (960, 720)


def test_recording_config_rejects_a_shutter_that_does_not_match_the_angle():
    with pytest.raises(ValueError, match="1/480"):
        RecordingConfig(shutter_angle_deg=90)
    with pytest.raises(ValueError, match="iso_max"):
        RecordingConfig(iso_max=1000)


def test_lens_mod_presets(monkeypatch, tmp_path):
    monkeypatch.setattr(presets, "user_presets_dir", lambda: tmp_path)
    names = {entry["name"]: entry["title"] for entry in presets.list_presets()}
    assert names["gopro13_mlm2_adwal002"] == "HERO13 + Max Lens Mod 2.0 (ADWAL-002)"
    assert names["gopro13_uwlm_aewal001"] == "HERO13 + Ultra Wide Lens Mod (AEWAL-001)"
    assert "gopro13_umi_gripper_fisheye_1080p" not in names
    _, mlm2 = presets.get_preset("gopro13_mlm2_adwal002")
    _, uwlm = presets.get_preset("gopro13_uwlm_aewal001")
    assert mlm2.gopro.max_lens_mod == 2 and mlm2.recording.lens_mod == "ADWAL-002"
    assert uwlm.gopro.max_lens_mod == 100 and uwlm.recording.lens_mod == "AEWAL-001"
    for config in (mlm2, uwlm):
        # Same webcam settings as the old gripper preset, for the live route.
        assert (config.gopro.webcam_fov, config.gopro.webcam_resolution) == (0, 12)
        assert config.solver.models == ["double_sphere", "kannala_brandt", "fisheye"]


def test_auto_detect_lens_mod_is_not_a_mismatch():
    config = GoProSettingsConfig(max_lens_mod=100, webcam_fov=0)
    state = {"settings": {"webcam_digital_lens": 0, "max_lens_mod": 3}}
    assert camera_state_warnings(config, state) == []
    config = GoProSettingsConfig(max_lens_mod=2, webcam_fov=0)
    assert camera_state_warnings(config, state)


# --- Labs --------------------------------------------------------------------


def _decode_qr(data_uri: str) -> str:
    png = base64.b64decode(data_uri.split(",", 1)[1])
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    return cv2.QRCodeDetector().detectAndDecode(image)[0]


@pytest.mark.parametrize(
    "lens_mod, calibration, dataset",
    [
        ("ADWAL-002", "mVr4Tp60e0!NoX2fXti16S45", "mVr4Tp60e0!NoX2fXti16S0"),
        ("AEWAL-001", "mVr4Tp60e0!NoX10fXti16S45", "mVr4Tp60e0!NoX10fXti16S0"),
    ],
)
def test_labs_codes_and_qr_round_trip(lens_mod, calibration, dataset):
    rec = RecordingConfig(lens_mod=lens_mod)
    assert labs_command(rec, calibration=True) == calibration
    assert labs_command(rec, calibration=False) == dataset
    payload = labs_payload(rec)
    assert payload["calibration"]["code"] == calibration
    assert payload["dataset"]["code"] == dataset
    assert _decode_qr(payload["calibration"]["png"]) == calibration
    assert _decode_qr(payload["dataset"]["png"]) == dataset


def test_labs_unverified_codes_and_checklist():
    mlm2, uwlm = RecordingConfig(), RecordingConfig(lens_mod="AEWAL-001")
    assert [u["code"] for u in unverified_codes(mlm2, calibration=True)] == ["fX", "S45"]
    assert [u["code"] for u in unverified_codes(mlm2, calibration=False)] == ["fX"]
    assert [u["code"] for u in unverified_codes(uwlm, calibration=True)] == ["fX", "oX10", "S45"]
    rows = {row["setting"]: row for row in checklist(mlm2)}
    assert rows["Lens mod"]["value"] == "Max Lens Mod 2.0"
    assert "not detected automatically" in rows["Lens mod"]["how"]
    assert "Detected automatically" in checklist(uwlm)[0]["how"]
    assert rows["HyperSmooth"]["value"] == "Off" and rows["Lens"]["value"] == "Ultra Wide"
    assert "1/480" in rows["Shutter"]["value"] and "Auto" in rows["Shutter"]["value"]
    with pytest.raises(ValueError):
        labs_command(RecordingConfig(width=1920, height=1080), calibration=True)


def test_labs_endpoint():
    client = TestClient(app_module.app)
    config = AppConfig(recording=RecordingConfig(lens_mod="AEWAL-001"))
    body = client.post("/api/recording/labs", json={"config": config.model_dump()}).json()
    assert body["calibration"]["code"] == "mVr4Tp60e0!NoX10fXti16S45"
    assert body["calibration"]["png"].startswith("data:image/png;base64,")
    assert body["checklist"][0]["setting"] == "Lens mod"


# --- ORB-SLAM3 block size -----------------------------------------------------

KB_RAW = {
    "image_width": 4000,
    "image_height": 3000,
    "fps": 59.94,
    "intrinsics": {
        "focal_length": 1200.0,
        "principal_pt_x": 2001.0,
        "principal_pt_y": 1499.0,
        "radial_distortion_1": 0.01,
        "radial_distortion_2": -0.002,
        "radial_distortion_3": 0.0003,
        "radial_distortion_4": -0.00004,
    },
}


def test_orbslam3_block_scales_to_the_slam_size():
    native = yaml.safe_load(orbslam3_kb8_yaml(KB_RAW))
    text = orbslam3_kb8_yaml(KB_RAW, (960, 720))
    scaled = yaml.safe_load(text)
    assert (scaled["Camera.width"], scaled["Camera.height"]) == (960, 720)
    assert scaled["Camera1.fx"] == pytest.approx(1200.0 * 0.24)
    assert scaled["Camera1.fy"] == scaled["Camera1.fx"]
    assert scaled["Camera1.cx"] == pytest.approx(2001.0 * 0.24)
    assert scaled["Camera1.cy"] == pytest.approx(1499.0 * 0.24)
    for i in range(1, 5):
        assert scaled[f"Camera1.k{i}"] == native[f"Camera1.k{i}"]
    assert "Scaled from the 4000x3000 calibration to 960x720" in text
    assert "Scaled" not in orbslam3_kb8_yaml(KB_RAW)
    with pytest.raises(ValueError, match="aspect"):
        orbslam3_kb8_yaml(KB_RAW, (960, 540))


def _kb_summary(tmp_path, width, height):
    raw = {**KB_RAW, "image_width": width, "image_height": height}
    path = tmp_path / "cam_kannala_brandt_orbslam3.yaml"
    path.write_text(orbslam3_kb8_yaml(raw), encoding="utf-8")
    kb = {"model": "kannala_brandt", "ok": True, "orbslam3_yaml": str(path), "openicc": raw}
    return {"image_size": [width, height], "results": [kb]}, path


def test_job_writes_the_slam_block_at_960x720_for_4_3_only(tmp_path):
    job = RecordingJob(tmp_path)
    summary, path = _kb_summary(tmp_path, 4000, 3000)
    info = job._write_orbslam3(summary)
    assert info["size"] == [960, 720] and "960x720" in info["note"]
    assert yaml.safe_load(path.read_text())["Camera.width"] == 960
    assert summary["results"][0]["orbslam3_size"] == [960, 720]
    summary, path = _kb_summary(tmp_path, 3840, 2160)
    info = job._write_orbslam3(summary)
    assert info["size"] == [3840, 2160] and "own size" in info["note"]
    assert yaml.safe_load(path.read_text())["Camera.width"] == 3840
    assert job._write_orbslam3({"image_size": [4000, 3000], "results": []}) is None


# --- clip check (no ffmpeg needed) -------------------------------------------


GOOD_PROBE = {
    "width": 4000, "height": 3000, "avg_frame_rate": 60000 / 1001, "r_frame_rate": 60000 / 1001,
    "codec": "hevc", "duration_s": 75.0, "rotation": 0,
}
GOOD_META = {
    "model": "HERO13 Black", "serial": "C3501234567890", "eise": "N", "eisa": "N/A",
    "vfov": "W", "zfov": 150.0, "shutter_s": 1 / 480, "has_imu": True,
}


def _rows(probe, meta, rec=None):
    return {row["field"]: row for row in compare(probe, meta, rec or RecordingConfig())}


def test_clip_check_passes_a_matching_clip():
    rows = _rows(GOOD_PROBE, GOOD_META)
    for field in ("model", "resolution", "fps", "hypersmooth", "shutter"):
        assert rows[field]["status"] == "ok", rows[field]
    assert rows["fps"]["found"] == "59.94 fps"  # 59.94 counts as 60
    # The file never confirms the lens: always "unknown", with what it did say.
    assert rows["lens"]["status"] == "unknown"
    assert "W" in rows["lens"]["found"] and "150.0°" in rows["lens"]["found"]
    assert "camera screen" in rows["lens"]["advice"]
    assert rows["codec"]["found"] == "hevc" and rows["imu"]["found"] == "present"
    assert not any(row["status"] == "mismatch" for row in rows.values())


def test_clip_check_flags_each_wrong_setting():
    probe = {**GOOD_PROBE, "width": 3840, "height": 2160, "avg_frame_rate": 30.0}
    meta = {**GOOD_META, "model": "HERO12 Black", "eise": "Y", "eisa": "HS Boost",
            "shutter_s": 1 / 60}
    rows = _rows(probe, meta)
    for field in ("model", "resolution", "fps", "hypersmooth", "shutter"):
        assert rows[field]["status"] == "mismatch", field
        assert rows[field]["advice"], field
    assert rows["hypersmooth"]["found"] == "On (HS Boost)"
    assert rows["shutter"]["found"] == "1/60 s"
    assert rows["lens"]["status"] == "unknown"  # never a mismatch


def test_clip_check_without_gopro_metadata_or_ffprobe():
    rows = _rows({"error": "ffprobe is not installed"}, {})
    for field in ("model", "resolution", "fps", "hypersmooth", "lens", "shutter"):
        assert rows[field]["status"] == "unknown", field
    assert "original file" in rows["model"]["advice"]
    # A GoPro clip that only lacks the stabilisation tag: EISA alone decides.
    rows = _rows(GOOD_PROBE, {**GOOD_META, "eise": None, "eisa": "HS EIS"})
    assert rows["hypersmooth"]["status"] == "mismatch"


# --- guide and helpers --------------------------------------------------------


def test_recording_guide_has_the_23_checkpoints_with_words():
    guide = recording_guide(AppConfig())
    assert guide["total"] == 23 == len(guide["checkpoints"])
    first, second = guide["checkpoints"][:2]
    assert first["caption"] == "middle distance · centre"
    assert second["label"] == "left small" and second["caption"] == "far · left edge"
    near = [c for c in guide["checkpoints"] if c["distance"] == "near"]
    tilted = [c for c in guide["checkpoints"] if c["tilted"]]
    assert len(near) == 9 and len(tilted) == 4
    assert tilted[0]["caption"] == "tilted · upper left" and tilted[0]["skew"] > 0
    client = TestClient(app_module.app)
    assert client.get("/api/recording/guide").json()["total"] == 23


def test_names_and_limits():
    assert sanitise_clip_name("../../etc/GX010001.MP4") == "GX010001.MP4"
    assert sanitise_clip_name("my clip (1).mp4") == "my_clip__1_.mp4"
    assert sanitise_clip_name("") == "clip.mp4"
    assert camera_name_from_serial("C3501234567890") == "gopro13_7890"
    assert camera_name_from_serial("ab") is None
    assert motion_limit_px(1.5, 1920, 30.0) == pytest.approx(1.5)
    assert motion_limit_px(1.5, 4000, 12.0) == pytest.approx(1.5 * 4000 / 1920 * 2.5)


# --- extraction on a synthetic clip -------------------------------------------


def _extract(clip, run_dir, prior=None, **rec_overrides):
    probe = probe_video(clip.path)
    return extract_views(
        clip.path,
        config=AppConfig(),
        rec=RecordingConfig(**rec_overrides),
        probe=probe,
        frames_dir=run_dir / "frames",
        overlays_dir=run_dir / "overlays",
        prior_poses=prior,
    )


@needs_ffmpeg
def test_probe_and_check_of_the_synthetic_clip(synthetic_clip):
    probe = probe_video(synthetic_clip.path)
    assert (probe["width"], probe["height"], probe["avg_frame_rate"]) == (1600, 1200, 24.0)
    result = check_clip(synthetic_clip.path, RecordingConfig())
    rows = {row["field"]: row for row in result["check"]}
    assert rows["resolution"]["status"] == "mismatch" and rows["fps"]["status"] == "mismatch"
    assert rows["model"]["status"] == "unknown"  # no GPMF in an ffmpeg-written file
    assert result["metadata"]["gpmf_found"] is False
    assert result["mismatch_count"] == 2


@needs_ffmpeg
def test_extraction_keeps_one_sharp_still_view_per_position(synthetic_clip, tmp_path):
    result = _extract(synthetic_clip, tmp_path)
    counts = result.counts
    kinds = [synthetic_clip.label_at(view["time_s"]) for view in result.kept]
    # Nothing blurred, moving or empty is ever kept.
    assert all(kind == "sharp" for kind, _pose in kinds), kinds
    held = {
        index for index, entry in enumerate(POSES) if entry[4] != "blurred"
    }
    # One view per sharp held position, and the all-blurred position dropped.
    assert sorted(pose for _kind, pose in kinds) == sorted(held)
    assert counts["kept"] == len(held) == len(result.kept)
    assert counts["no_board"] >= 10  # the empty start
    assert counts["moving"] >= 14  # at least one per move between positions
    assert counts["blurred"] >= 6  # the blurred halves and the blurred position
    assert counts["duplicate"] > counts["kept"]  # the rest of every hold
    assert counts["over_cap"] == 0
    total = sum(counts[k] for k in ("kept", *recording.DROP_REASONS))
    assert total == counts["samples"]
    # Files the solver reads, full resolution, and their overlays.
    names = [view["name"] for view in result.kept]
    assert names[0] == "capture_001.jpg" and names[-1] == f"capture_{len(names):03d}.jpg"
    image = cv2.imread(str(tmp_path / "frames" / names[0]))
    assert image.shape[:2] == (1200, 1600)
    assert (tmp_path / "overlays" / names[0]).is_file()
    assert not list(tmp_path.glob("_staging_*"))

    # A retake of the same positions adds nothing new; numbering would continue.
    again = _extract(synthetic_clip, tmp_path, prior=result.poses)
    assert again.counts["kept"] == 0 and again.first_index == len(names) + 1


@needs_ffmpeg
def test_extraction_cap_keeps_the_most_varied(synthetic_clip, tmp_path):
    prior = [PoseParams(0.5, 0.5, 0.23, 0.0)]
    result = _extract(synthetic_clip, tmp_path, prior=prior, max_views=6)
    assert result.counts["kept"] == 5  # 6 in the run, one already kept
    assert result.counts["over_cap"] >= 8
    # Farthest-first from the kept view: the far ring, not the ones next to the centre.
    assert all(pose_distance(pose, prior[0]) > 0.3 for pose in result.poses)


@needs_ffmpeg
def test_extraction_reports_an_undecodable_clip(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video" * 100)
    with pytest.raises(RecordingError, match="ffmpeg could not decode"):
        extract_views(
            bad,
            config=AppConfig(),
            rec=RecordingConfig(),
            probe={"width": 64, "height": 48, "duration_s": 1.0},
            frames_dir=tmp_path / "frames",
            overlays_dir=tmp_path / "overlays",
        )


# --- the job through the HTTP API -----------------------------------------------


def _config(models=("fisheye",)) -> AppConfig:
    return AppConfig(
        capture=CaptureConfig(),
        solver=SolverConfig(models=list(models), min_frames=8),
        recording=RecordingConfig(),
    )


def _poll(client, timeout=240.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/api/recording/status").json()
        if status["state"] in ("solved", "error", "ready", "idle"):
            return status
        time.sleep(0.2)
    raise AssertionError(f"job did not finish: {status}")


def _upload(client, clip_path: Path, name: str):
    with clip_path.open("rb") as stream:
        return client.put(f"/api/recording/clips?name={name}", content=stream.read())


@pytest.fixture
def fresh_job(monkeypatch, tmp_path):
    job = RecordingJob(tmp_path / "runs")
    monkeypatch.setattr(app_module, "_recording", job)
    return job


@needs_ffmpeg
def test_end_to_end_upload_check_extract_solve_and_retake(
    synthetic_clip, fresh_job, monkeypatch, tmp_path
):
    real_check = recording.check_clip

    def check_with_serial(path, rec):
        # The synthetic clip has no GPMF; pretend the camera wrote its serial.
        result = real_check(path, rec)
        result["metadata"] = {**result["metadata"], "serial": "C3501234567890"}
        return result

    monkeypatch.setattr(recording, "check_clip", check_with_serial)
    client = TestClient(app_module.app)
    runs = tmp_path / "runs"
    config = _config()

    # No run yet: an upload is refused.
    assert _upload(client, synthetic_clip.path, "x.mp4").status_code == 409
    started = client.post(
        "/api/recording/start", json={"config": config.model_dump(), "runs_dir": str(runs)}
    ).json()
    assert started["state"] == "ready" and started["route"] == "recording"
    run_dir = Path(started["run_dir"])
    assert {p.name for p in run_dir.iterdir()} >= {"clips", "frames", "overlays", "config.json"}
    saved = json.loads((run_dir / "config.json").read_text())
    assert saved["acquisition_mode"]["route"] == "recording"

    response = _upload(client, synthetic_clip.path, "GX010001.MP4")
    assert response.status_code == 200, response.text
    status = _poll(client)
    assert status["state"] == "solved", status["message"]

    # Named after the serial, since the name was a preset default.
    assert status["camera_name"] == "gopro13_7890" and status["camera_named_from_serial"]
    assert "gopro13_7890" in status["camera_name_note"]
    run_dir = Path(status["output_dir"])
    assert run_dir.name.startswith("gopro13_7890_") and run_dir.is_dir()
    assert (run_dir / "clips" / "GX010001.MP4").stat().st_size == synthetic_clip.path.stat(
    ).st_size

    # The same keys the live session status uses for results.
    for key in ("results", "coverage", "guide", "image_size", "acquisition_mode", "run_id",
                "summary_path", "rejected_points", "captures"):
        assert key in status, key
    assert status["image_size"] == [1600, 1200]
    assert status["results"][0]["model"] == "fisheye" and status["results"][0]["ok"]
    assert status["coverage"]["count"] == status["captures"] == 15
    assert status["guide"]["total_count"] == 23
    assert status["acquisition_mode"]["lens_fov"] == "Ultra Wide"
    # The clip check warns (not 4000x3000, not 60 fps) but never blocks the solve.
    (clip,) = status["clips"]
    assert clip["name"] == "GX010001.MP4" and clip["mismatch_count"] == 2
    assert status["mismatch_count"] == 2
    assert clip["counts"]["kept"] == 15 and clip["counts"]["blurred"] > 0

    summary = json.loads(Path(status["summary_path"]).read_text())
    assert summary["route"] == "recording"
    assert summary["recording"]["serial"] == "C3501234567890"
    assert summary["recording"]["counts"]["kept"] == 15
    assert summary["recording"]["clips"][0]["check"]
    assert summary["acquisition_mode"]["route"] == "recording"
    assert summary["image_size"] == [1600, 1200]
    assert summary["coverage"]["count"] == 15

    # A retake adds its clip to the same run and re-solves; nothing new here.
    assert _upload(client, synthetic_clip.path, "GX010001.MP4").status_code == 200
    status = _poll(client)
    assert status["state"] == "solved"
    assert [c["name"] for c in status["clips"]] == ["GX010001.MP4", "GX010001_2.MP4"]
    assert status["clips"][1]["counts"]["kept"] == 0
    assert status["captures"] == 15 and status["mismatch_count"] == 4
    assert Path(status["output_dir"]) == run_dir

    # Next camera: back to idle, with nothing of this run left in the status.
    reset = client.post("/api/recording/new").json()
    assert reset["state"] == "idle" and reset["run_id"] is None and reset["results"] is None
    assert reset["clips"] == [] and reset["captures"] == 0


@needs_ffmpeg
def test_a_clip_too_short_to_solve_asks_for_another(tmp_path, fresh_job):
    clip = build_clip(tmp_path / "short.mp4", poses=POSES[:3], size=(800, 600))
    client = TestClient(app_module.app)
    client.post(
        "/api/recording/start",
        json={"config": _config().model_dump(), "runs_dir": str(tmp_path / "runs")},
    )
    assert _upload(client, clip.path, "short.mp4").status_code == 200
    status = _poll(client)
    assert status["state"] == "error" and status["needs_more_views"] is True
    assert "solving needs 8" in status["message"]


def test_busy_job_refuses_uploads_and_resets(tmp_path, fresh_job):
    client = TestClient(app_module.app)
    client.post(
        "/api/recording/start",
        json={"config": _config().model_dump(), "runs_dir": str(tmp_path / "runs")},
    )
    fresh_job._state = "analysing"
    assert client.put("/api/recording/clips?name=a.mp4", content=b"x").status_code == 409
    assert client.post("/api/recording/new").status_code == 409
    fresh_job._state = "ready"
    response = client.put("/api/recording/clips?name=a.mp4", content=b"")
    assert response.status_code == 400
    assert client.get("/api/recording/status").json()["state"] == "error"


def test_live_session_is_untouched_by_the_recording_route():
    client = TestClient(app_module.app)
    before = client.get("/api/session/status").json()
    client.get("/api/recording/status")
    assert client.get("/api/session/status").json()["state"] == before["state"]


# --- the OpenICC Kannala-Brandt block, for real (needs the Docker image) ---------


def _docker_image_present() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect",
                 os.environ.get("OPENICC_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE)],
                capture_output=True,
                timeout=15,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@needs_ffmpeg
@pytest.mark.skipif(not _docker_image_present(), reason="needs the OpenICC Docker image")
def test_end_to_end_kannala_brandt_block_is_960x720(varied_clip, fresh_job, tmp_path):
    client = TestClient(app_module.app)
    config = _config(models=("kannala_brandt", "fisheye"))
    client.post(
        "/api/recording/start",
        json={"config": config.model_dump(), "runs_dir": str(tmp_path / "runs")},
    )
    assert _upload(client, varied_clip.path, "GX010002.MP4").status_code == 200
    status = _poll(client, timeout=600)
    assert status["state"] == "solved", status["message"]
    kb = next(r for r in status["results"] if r["model"] == "kannala_brandt")
    assert kb["ok"], kb.get("error")
    native = json.loads(Path(kb["json"]).read_text())
    assert (native["image_width"], native["image_height"]) == (1600, 1200)
    block = yaml.safe_load(Path(kb["orbslam3_yaml"]).read_text())
    assert (block["Camera.width"], block["Camera.height"]) == (960, 720)
    scale = 960 / 1600
    assert block["Camera1.fx"] == pytest.approx(native["intrinsics"]["focal_length"] * scale)
    assert block["Camera1.cx"] == pytest.approx(native["intrinsics"]["principal_pt_x"] * scale)
    assert block["Camera1.k1"] == pytest.approx(native["intrinsics"]["radial_distortion_1"])
    assert status["orbslam3"]["size"] == [960, 720]
