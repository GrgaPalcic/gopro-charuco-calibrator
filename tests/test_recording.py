"""The "From a recording" route: clip check, frame selection on a synthetic clip, the
Labs codes, the ORB-SLAM3 block size, and the job end to end through the HTTP API."""

import base64
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import cv2
import numpy as np
import pytest
import yaml
from conftest import POSES, build_clip, needs_ffmpeg, with_contrast
from fastapi.testclient import TestClient

from gopro_charuco_calibrator import app as app_module
from gopro_charuco_calibrator import presets, recording
from gopro_charuco_calibrator.clipcheck import (
    check_clip,
    compare,
    ffmpeg_error_line,
    probe_video,
)
from gopro_charuco_calibrator.coverage import PoseParams
from gopro_charuco_calibrator.detection import detect_markers
from gopro_charuco_calibrator.gopro import camera_state_warnings
from gopro_charuco_calibrator.guide import default_checkpoints
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
    TOOLS_MISSING,
    RecordingError,
    RecordingJob,
    _frame_size,
    _Sample,
    _Selector,
    _Staged,
    board_sharpness,
    camera_name_from_serial,
    extract_views,
    incomplete_row,
    join_names,
    motion_limit_px,
    recording_guide,
    sanitise_clip_name,
)
from gopro_charuco_calibrator.synthetic import recording_camera

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


def test_recording_config_lens_is_the_one_with_a_labs_code():
    # fX (the only lens code) is Max SuperView / Ultra Wide: another lens would put a
    # QR code for one lens next to a checklist line for another.
    with pytest.raises(ValueError):
        RecordingConfig(lens="Linear")


def test_recording_config_rejects_a_shutter_that_does_not_match_the_angle():
    with pytest.raises(ValueError, match="1/480"):
        RecordingConfig(shutter_angle_deg=90)
    with pytest.raises(ValueError, match="iso_max"):
        RecordingConfig(iso_max=1000)


def test_lens_mod_code_follows_lens_mod_after_a_json_round_trip():
    _, preset = presets.get_preset("gopro13_mlm2_adwal002")
    dumped = preset.model_dump()
    assert dumped["recording"]["labs_lens_mod_code"] == "oX2"
    dumped["recording"]["lens_mod"] = "AEWAL-001"  # the UI changes only the mod
    edited = AppConfig.model_validate(dumped)
    assert edited.recording.labs_lens_mod_code == "oX10"
    assert labs_command(edited.recording, calibration=True) == "mVr4Tp60e0!NoX10fXti16S45"
    assert "oX10" in [u["code"] for u in unverified_codes(edited.recording, calibration=True)]
    # A code that is not one of the two mods' codes is kept as written.
    assert RecordingConfig(labs_lens_mod_code="oX3").labs_lens_mod_code == "oX3"


def test_labs_rejects_values_the_creator_does_not_offer():
    with pytest.raises(ValueError, match="frame rate of 59.94"):
        labs_command(RecordingConfig(fps=59.94, shutter_angle_deg=45), calibration=True)
    rec = RecordingConfig(fps=60, shutter_angle_deg=22.5, calibration_shutter="1/960")
    with pytest.raises(ValueError, match="shutter angle of 22.5"):
        labs_command(rec, calibration=True)
    rec = RecordingConfig(fps=30, shutter_angle_deg=22.5 * 2, calibration_shutter="1/240")
    assert labs_command(rec, calibration=True) == "mVr4Tp30e0!NoX2fXti16S45"
    client = TestClient(app_module.app)
    config = AppConfig(recording=RecordingConfig(fps=59.94, shutter_angle_deg=45))
    response = client.post("/api/recording/labs", json={"config": config.model_dump()})
    assert response.status_code == 400 and "59.94" in response.json()["detail"]


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
    assert [u["code"] for u in unverified_codes(uwlm, calibration=True)] == [
        "fX", "oX10", "oX3", "S45"
    ]
    lens_note = unverified_codes(mlm2, calibration=True)[0]["note"]
    assert "Max SuperView (listed for HERO12-13)" in lens_note
    assert "Check the lens on the camera screen." in lens_note
    alternative = unverified_codes(uwlm, calibration=False)[2]["note"]
    assert "oX3fX" in alternative and "oX2fX" in alternative and "uses oX10" in alternative
    rows = {row["setting"]: row for row in checklist(mlm2)}
    assert rows["Lens mod"]["value"] == "Max Lens Mod 2.0"
    assert "not detected automatically" in rows["Lens mod"]["how"]
    assert "Detected automatically" in checklist(uwlm)[0]["how"]
    assert rows["HyperSmooth"]["value"] == "Off" and rows["Lens"]["value"] == "Ultra Wide"
    assert "1/480" in rows["Shutter"]["value"] and "Auto" in rows["Shutter"]["value"]
    assert rows["Frame rate"] == {
        "setting": "Frame rate", "value": "60 fps", "how": "Set it in the video preset."
    }
    with pytest.raises(ValueError):
        labs_command(RecordingConfig(width=1920, height=1080), calibration=True)


def test_labs_endpoint():
    client = TestClient(app_module.app)
    config = AppConfig(recording=RecordingConfig(lens_mod="AEWAL-001"))
    body = client.post("/api/recording/labs", json={"config": config.model_dump()}).json()
    assert body["calibration"]["code"] == "mVr4Tp60e0!NoX10fXti16S45"
    assert body["calibration"]["png"].startswith("data:image/png;base64,")
    assert body["checklist"][0]["setting"] == "Lens mod"


def test_labs_and_start_refuse_a_preset_without_recording_settings(tmp_path, fresh_job):
    client = TestClient(app_module.app)
    _, live_only = presets.get_preset("gopro11_wide_1080p")
    assert live_only.recording is None
    body = {"config": live_only.model_dump(), "runs_dir": str(tmp_path / "runs")}
    for endpoint in ("/api/recording/labs", "/api/recording/start"):
        response = client.post(endpoint, json=body)
        assert response.status_code == 400, endpoint
        assert "no settings for recording" in response.json()["detail"]
    assert not (tmp_path / "runs").exists() and fresh_job.run_id is None


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
    assert rows["resolution"]["advice"] == (
        "Set the resolution to 4K with the aspect ratio 4:3 (4000x3000), then record again."
    )
    # The advice names the preset's own size, whatever it is.
    other = RecordingConfig(width=1600, height=1200, fps=24, calibration_shutter="1/192")
    rows = _rows(probe, meta, other)
    assert rows["resolution"]["advice"] == "Set the resolution to 1600x1200, then record again."


def test_clip_check_without_gopro_metadata_or_ffprobe():
    rows = _rows({"error": "ffprobe is not installed"}, {})
    for field in ("model", "resolution", "fps", "hypersmooth", "lens", "shutter"):
        assert rows[field]["status"] == "unknown", field
    assert "original file" in rows["model"]["advice"]
    # A GoPro clip that only lacks the stabilisation tag: EISA alone decides.
    rows = _rows(GOOD_PROBE, {**GOOD_META, "eise": None, "eisa": "HS EIS"})
    assert rows["hypersmooth"]["status"] == "mismatch"


def test_clip_check_flags_a_clip_from_another_camera():
    rows = _rows(GOOD_PROBE, GOOD_META)
    assert rows["serial"]["status"] == "ok"
    rows = {r["field"]: r for r in compare(GOOD_PROBE, GOOD_META, RecordingConfig(), "C350999")}
    assert rows["serial"]["status"] == "mismatch" and "another camera" in rows["serial"]["advice"]
    assert rows["serial"]["expected"] == "C350999"


def test_clip_check_flags_a_rotation_flag():
    assert _rows({**GOOD_PROBE, "rotation": 0}, GOOD_META)["rotation"]["status"] == "ok"
    row = _rows({**GOOD_PROBE, "rotation": -180}, GOOD_META)["rotation"]
    assert row["status"] == "mismatch" and row["found"] == "turn by 180°"
    assert "original file" in row["advice"]
    # Frames are read as stored, so the size is never swapped for a 90 degree flag.
    assert _frame_size({"width": 1600, "height": 1200, "rotation": 90}) == (1600, 1200)


def test_ffmpeg_error_lines_leave_the_folder_out(tmp_path):
    path = tmp_path / "runs" / "cam_1" / "clips" / "junk.mp4"
    line = ffmpeg_error_line(f"noise\n{path}: Invalid data found when processing input\n", path)
    assert line == "Invalid data found when processing input"
    assert ffmpeg_error_line(f"[mov] moov atom not found in {path}", path) == (
        "[mov] moov atom not found in junk.mp4"
    )
    assert ffmpeg_error_line("", path) is None


def test_clip_check_model_from_a_generic_device_name():
    # gpmf gives DVNM "Camera" no model; a missing model is unknown, never a mismatch.
    rows = _rows(GOOD_PROBE, {**GOOD_META, "model": None})
    assert rows["model"]["status"] == "unknown"


def test_a_metadata_failure_gives_unknown_rows(tmp_path, monkeypatch):
    job = RecordingJob(tmp_path)

    def broken(*args, **kwargs):
        raise TypeError("odd tag shape")

    monkeypatch.setattr(recording, "check_clip", broken)
    monkeypatch.setattr(
        "gopro_charuco_calibrator.clipcheck.probe_video", lambda path: dict(GOOD_PROBE)
    )
    check = job._check(tmp_path / "x.mp4", None)
    rows = {row["field"]: row for row in check["check"]}
    assert rows["resolution"]["status"] == "ok" and rows["model"]["status"] == "unknown"
    assert "odd tag shape" in check["metadata"]["error"]
    assert check["mismatch_count"] == 0


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


def test_recording_guide_for_a_config_before_a_run(fresh_job):
    client = TestClient(app_module.app)
    config = AppConfig(recording=RecordingConfig())
    config.coverage_targets.size_max = 0.45  # the near checkpoints follow it
    guide = client.post("/api/recording/guide", json={"config": config.model_dump()}).json()
    assert guide["total"] == 23
    assert guide == recording_guide(config) != recording_guide(AppConfig())


def test_names_and_limits():
    assert join_names(["A"]) == "A" and join_names(["A", "B"]) == "A and B"
    assert join_names(["A", "B", "C"]) == "A, B and C"
    assert sanitise_clip_name("../../etc/GX010001.MP4") == "GX010001.MP4"
    assert sanitise_clip_name("my clip (1).mp4") == "my_clip__1_.mp4"
    assert sanitise_clip_name("") == "clip.mp4"
    # Capped in bytes, not characters: 130 CJK characters are 390 bytes.
    long = sanitise_clip_name("录" * 130 + ".MP4")
    assert long.endswith(".MP4") and len(long.encode("utf-8")) <= 210
    assert len(sanitise_clip_name("a" * 300 + ".mp4").encode()) == 204
    assert camera_name_from_serial("C3501234567890") == "gopro13_7890"
    assert camera_name_from_serial("ab") is None
    assert motion_limit_px(1.5, 1920, 30.0) == pytest.approx(1.5)
    assert motion_limit_px(1.5, 4000, 12.0) == pytest.approx(1.5 * 4000 / 1920 * 2.5)


# --- extraction on a synthetic clip -------------------------------------------


def _extract(clip, run_dir, prior=None, prior_scores=None, **rec_overrides):
    probe = probe_video(clip.path)
    return extract_views(
        clip.path,
        config=AppConfig(),
        rec=RecordingConfig(**rec_overrides),
        probe=probe,
        frames_dir=run_dir / "frames",
        overlays_dir=run_dir / "overlays",
        prior_poses=prior,
        prior_scores=prior_scores,
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


# Two positions the synthetic clip does not have (tilted about two axes).
NEW_POSITIONS = [
    (0.30, 0.50, 0.40, (0.0, 0.5, 0.3)),
    (0.70, 0.50, 0.40, (0.0, -0.5, -0.3)),
]


@needs_ffmpeg
def test_a_short_blurred_retake_keeps_nothing(synthetic_clip, tmp_path):
    first = _extract(synthetic_clip, tmp_path / "run")
    poses = [(*entry, "blurred") for entry in NEW_POSITIONS]
    retake = build_clip(tmp_path / "retake.mp4", poses=poses)
    prior_scores = [view["weighted"] for view in first.kept]
    again = _extract(retake, tmp_path / "run", prior=first.poses, prior_scores=prior_scores)
    assert again.counts["kept"] == 0, again.kept
    assert again.counts["blurred"] >= 2
    # The same two positions held sharp are new views, so the rule is not just
    # dropping everything in a retake.
    sharp = build_clip(tmp_path / "sharp.mp4", poses=[(*e, "sharp") for e in NEW_POSITIONS])
    kept = _extract(sharp, tmp_path / "run", prior=first.poses, prior_scores=prior_scores)
    assert kept.counts["kept"] == 2
    assert kept.kept[0]["name"] == f"capture_{len(first.kept) + 1:03d}.jpg"


@needs_ffmpeg
def test_most_holds_blurred_still_keeps_only_the_sharp_ones(tmp_path):
    # A clip recorded before the fast-shutter QR code: 3 sharp far holds, 4 near holds
    # blurred throughout. A median reference would be a blurred score and keep them all.
    far = [(0.35, 0.35, 0.65), (0.65, 0.35, 0.65), (0.65, 0.65, 0.65)]
    near = [(0.3, 0.5, 0.28), (0.7, 0.5, 0.28), (0.5, 0.35, 0.28), (0.5, 0.65, 0.28)]
    poses = [(*p, (0.0, 0.0, 0.0), "sharp") for p in far]
    poses += [(*p, (0.0, 0.0, 0.0), "blurred") for p in near]
    clip = build_clip(tmp_path / "mostly_blurred.mp4", poses=poses)
    result = _extract(clip, tmp_path / "run")
    kinds = [clip.label_at(view["time_s"]) for view in result.kept]
    assert sorted(kinds) == [("sharp", 0), ("sharp", 1), ("sharp", 2)], kinds
    assert result.counts["blurred"] >= 4


def test_sharpness_does_not_depend_on_contrast():
    board = AppConfig().board
    camera = recording_camera(board, (1600, 1200))
    gray = camera.render_gray(*camera.pose_towards(800, 600, 0.45, (0.0, 0.0, 0.0)))
    half = cv2.resize(gray, (800, 600), interpolation=cv2.INTER_AREA)
    detection = detect_markers(half, (800, 600), board)
    full = board_sharpness(half, detection)
    for contrast in (0.7, 0.45):
        assert board_sharpness(with_contrast(half, contrast), detection) == pytest.approx(
            full, rel=0.05
        )
    blurred = cv2.GaussianBlur(half, (0, 0), 0.65)  # sigma 1.3 at full resolution
    assert board_sharpness(with_contrast(blurred, 0.45), detection) < 0.5 * full


@needs_ffmpeg
def test_sharp_holds_in_low_contrast_are_kept(tmp_path):
    # A first clip: sharp holds at full, 70 % and 45 % contrast, and a blurred hold at
    # 45 %. Only the blurred one may go.
    poses = [
        (0.35, 0.35, 0.65, (0.0, 0.0, 0.0), "sharp"),
        (0.65, 0.65, 0.65, (0.0, 0.0, 0.0), "sharp"),
        (0.30, 0.50, 0.40, (0.0, 0.0, 0.0), "sharp", 0.7),
        (0.70, 0.50, 0.40, (0.0, 0.0, 0.0), "sharp", 0.45),
        (0.50, 0.30, 0.28, (0.0, 0.0, 0.0), "sharp", 0.45),
        (0.50, 0.70, 0.40, (0.0, 0.0, 0.0), "blurred", 0.45),
    ]
    clip = build_clip(tmp_path / "shade.mp4", poses=poses)
    result = _extract(clip, tmp_path / "run")
    kinds = sorted(clip.label_at(view["time_s"]) for view in result.kept)
    assert kinds == [("sharp", i) for i in range(5)], kinds
    assert result.counts["blurred"] >= 1


@needs_ffmpeg
def test_a_retake_in_other_light_keeps_its_sharp_views(synthetic_clip, tmp_path):
    # The run's reference is the full-contrast first clip; the retake is darker.
    first = _extract(synthetic_clip, tmp_path / "run")
    prior_scores = [view["weighted"] for view in first.kept]
    for contrast in (0.7, 0.45):
        sharp = build_clip(
            tmp_path / f"sharp_{contrast}.mp4",
            poses=[(*e, "sharp", contrast) for e in NEW_POSITIONS],
        )
        kept = _extract(
            sharp, tmp_path / f"run_{contrast}", prior=first.poses, prior_scores=prior_scores
        )
        assert kept.counts["kept"] == 2, (contrast, kept.counts)
    blurred = build_clip(
        tmp_path / "blurred_dark.mp4", poses=[(*e, "blurred", 0.7) for e in NEW_POSITIONS]
    )
    again = _extract(blurred, tmp_path / "run_b", prior=first.poses, prior_scores=prior_scores)
    assert again.counts["kept"] == 0 and again.counts["blurred"] >= 2


def _cut_short(clip_path: Path, out: Path, fraction: float) -> Path:
    """A copy with the index first (as a camera writes it) and the rest cut off, like a
    copy from the card that stopped early."""
    whole = out.with_name(f"whole_{out.name}")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(clip_path), "-c", "copy",
         "-movflags", "+faststart", str(whole)],
        check=True,
    )
    data = whole.read_bytes()
    out.write_bytes(data[: int(len(data) * fraction)])
    return out


@needs_ffmpeg
def test_a_clip_cut_short_is_flagged_and_still_used(synthetic_clip, tmp_path):
    cut = _cut_short(synthetic_clip.path, tmp_path / "cut.mp4", 0.5)
    probe = probe_video(cut)
    whole = probe_video(synthetic_clip.path)["duration_s"]
    assert probe["duration_s"] == pytest.approx(whole, abs=0.1)  # the index says all of it
    result = _extract(SimpleNamespace(path=cut), tmp_path / "run")
    assert result.incomplete and 0 < result.read_s < 0.9 * result.duration_s
    assert 0 < result.counts["kept"] < 15
    kinds = [synthetic_clip.label_at(view["time_s"]) for view in result.kept]
    assert all(kind == "sharp" for kind, _pose in kinds), kinds
    row = incomplete_row(result)
    assert row["status"] == "mismatch" and row["field"] == "complete"
    assert row["advice"] == (
        f"Only the first {result.read_s:.0f} s of {result.duration_s:.0f} s could be read. "
        "Copy the file from the card again."
    )
    # The whole clip is not flagged.
    assert not _extract(synthetic_clip, tmp_path / "whole").incomplete


@needs_ffmpeg
def test_a_rotation_flag_does_not_turn_the_frames(synthetic_clip, tmp_path):
    turned = tmp_path / "turned.mp4"
    remux = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i",
         str(synthetic_clip.path), "-c", "copy", str(turned)],
        capture_output=True,
    )
    if remux.returncode != 0:
        pytest.skip("this ffmpeg cannot write a rotation flag (-display_rotation)")
    probe = probe_video(turned)
    assert abs(probe["rotation"]) == 90
    rows = {row["field"]: row for row in check_clip(turned, RecordingConfig())["check"]}
    assert rows["rotation"]["status"] == "mismatch"
    frames = tmp_path / "run"
    result = extract_views(
        turned, config=AppConfig(), rec=RecordingConfig(), probe=probe,
        frames_dir=frames / "frames", overlays_dir=frames / "overlays",
    )
    assert result.image_size == (1600, 1200) and result.counts["kept"] == 15
    assert cv2.imread(str(frames / "frames" / "capture_001.jpg")).shape[:2] == (1200, 1600)


@needs_ffmpeg
def test_a_failure_while_saving_views_leaves_no_files(synthetic_clip, tmp_path, monkeypatch):
    calls = []

    def draw_then_fail(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("disk full")
        return args[0]

    monkeypatch.setattr(recording, "draw_detection", draw_then_fail)
    with pytest.raises(RuntimeError, match="disk full"):
        _extract(synthetic_clip, tmp_path)
    assert list((tmp_path / "frames").iterdir()) == []
    assert list((tmp_path / "overlays").iterdir()) == []
    assert not list(tmp_path.glob("_staging_*"))


@needs_ffmpeg
def test_extraction_cap(synthetic_clip, tmp_path):
    prior = [PoseParams(0.5, 0.5, 0.23, 0.0)]
    result = _extract(synthetic_clip, tmp_path, prior=prior, max_views=6)
    assert result.counts["kept"] >= 5  # 6 in the run (one already kept), + checkpoints
    assert result.counts["over_cap"] >= 8
    # 15 sharp positions, less the centre one: it repeats the view already kept.
    assert result.counts["kept"] + result.counts["over_cap"] == 14


def _selector(tmp_path, prior, max_views):
    rec = RecordingConfig(max_views=max_views)
    return _Selector(config=AppConfig(), rec=rec, staging=tmp_path, prior_poses=prior)


def _staged(tmp_path, index, pose):
    path = tmp_path / f"s{index}.jpg"
    path.write_bytes(b"x")
    detection = SimpleNamespace(pose=pose)
    return _Staged(_Sample(index, index / 12, detection, 100.0, 0.0), path)


def _all_checkpoint_poses():
    targets = AppConfig().coverage_targets
    return [PoseParams(c.x, c.y, c.size, c.skew) for c in default_checkpoints(targets)]


def test_cap_keeps_the_most_varied_when_no_checkpoint_is_missing(tmp_path):
    prior = _all_checkpoint_poses()
    selector = _selector(tmp_path, prior, max_views=len(prior) + 2)
    near = PoseParams(0.52, 0.5, 0.35, 0.0)  # next to the centre checkpoint
    far = PoseParams(0.05, 0.95, 0.9, 0.0)
    farther = PoseParams(0.95, 0.05, 0.05, 0.9)
    selector.staged = [_staged(tmp_path, i, p) for i, p in enumerate([near, far, farther])]
    kept = selector.finish()
    assert [s.sample.pose for s in kept] == [far, farther]
    assert selector.counts["over_cap"] == 1


def test_cap_always_admits_a_view_that_completes_a_missing_checkpoint(tmp_path):
    # A full run (at the cap) still missing its first checkpoint, "center medium".
    checkpoints = _all_checkpoint_poses()
    prior = checkpoints[1:] + [PoseParams(0.05, 0.05, 0.05, 0.0)] * 97
    selector = _selector(tmp_path, prior, max_views=len(prior))
    centre = checkpoints[0]
    other = PoseParams(0.95, 0.95, 0.9, 0.0)
    selector.staged = [_staged(tmp_path, 0, other), _staged(tmp_path, 1, centre)]
    kept = selector.finish()
    assert [s.sample.pose for s in kept] == [centre]
    assert selector.counts["over_cap"] == 1


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

    def check_with_serial(path, rec, run_serial=None):
        # The synthetic clip has no GPMF; pretend the camera wrote its serial.
        result = real_check(path, rec, run_serial)
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
    # The same 2 settings differ in both clips: 2 settings, named once, in 2 clips.
    assert status["captures"] == 15 and status["mismatch_count"] == 2
    assert status["mismatch_fields"] == ["Resolution", "Frame rate"]
    assert status["mismatch_clips"] == ["GX010001.MP4", "GX010001_2.MP4"]
    assert "2 settings differ from the preset in GX010001.MP4 and GX010001_2.MP4" in (
        status["message"]
    )
    assert Path(status["output_dir"]) == run_dir
    saved = json.loads((run_dir / "config.json").read_text())
    camera = saved["config"]["camera"]
    assert (camera["width"], camera["height"], camera["fps"]) == (1600, 1200, 24.0)

    # Next camera: back to idle, with nothing of this run left in the status.
    reset = client.post("/api/recording/new").json()
    assert reset["state"] == "idle" and reset["run_id"] is None and reset["results"] is None
    assert reset["clips"] == [] and reset["captures"] == 0
    assert reset["orbslam3"] is None and reset["refused_clip"] is None


def _tree(folder: Path) -> dict[str, tuple[int, int]]:
    """Every file under ``folder``: size and modification time, to show it is untouched."""
    return {
        str(path.relative_to(folder)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


@needs_ffmpeg
def test_retake_adds_views_and_another_camera_gets_its_own_run(
    synthetic_clip, fresh_job, monkeypatch, tmp_path
):
    real_check = recording.check_clip
    serials = iter(["C3501234567890", "C3501234567890", "C3509999999999"])
    inodes = {}

    def check_with_serial(path, rec, run_serial=None):
        result = real_check(path, rec, run_serial)
        result["metadata"] = {**result["metadata"], "serial": next(serials)}
        inodes[path.name] = path.stat().st_ino
        return result

    monkeypatch.setattr(recording, "check_clip", check_with_serial)
    client = TestClient(app_module.app)
    runs = tmp_path / "runs"
    client.post(
        "/api/recording/start",
        json={"config": _config().model_dump(), "runs_dir": str(runs)},
    )
    assert _upload(client, synthetic_clip.path, "GX010001.MP4").status_code == 200
    first = _poll(client)
    assert first["state"] == "solved" and first["captures"] == 15
    assert first["previous_run_id"] is None and first["camera_switch"] is None

    retake = build_clip(tmp_path / "GX010002.MP4", poses=[(*e, "sharp") for e in NEW_POSITIONS])
    assert _upload(client, retake.path, "GX010002.MP4").status_code == 200
    status = _poll(client)
    assert status["state"] == "solved", status["message"]
    assert status["clips"][1]["kept"] == ["capture_016.jpg", "capture_017.jpg"]
    assert status["captures"] == 17 == status["coverage"]["count"]
    run_dir = Path(status["output_dir"])
    frames = sorted(p.name for p in (run_dir / "frames").glob("capture_*.jpg"))
    assert frames == [f"capture_{i:03d}.jpg" for i in range(1, 18)]
    summary = json.loads(Path(status["summary_path"]).read_text())
    assert len(summary["frames"]) == 17  # the solve read both clips' views
    assert {v["clip"] for v in summary["recording"]["views"]} == {"GX010001.MP4", "GX010002.MP4"}

    # A clip from another camera: the first run stays exactly as it is, and the clip
    # starts a run of its own, named from its serial, moved there (not copied).
    before = _tree(run_dir)
    assert _upload(client, synthetic_clip.path, "GX010003.MP4").status_code == 200
    other = _poll(client)
    assert other["state"] == "solved", other["message"]
    assert _tree(run_dir) == before
    note = (
        "This clip is from another camera (serial C3509999999999): started a separate "
        f"calibration for it. The previous camera's calibration is in {status['run_id']}."
    )
    assert other["message"].startswith(note)
    assert other["camera_switch"]["message"] == note
    assert other["camera_switch"]["previous_serial"] == "C3501234567890"
    assert other["previous_run_id"] == status["run_id"]
    assert other["previous_output_dir"] == str(run_dir)
    assert other["run_id"] != status["run_id"] and other["run_id"].startswith("gopro13_9999_")
    assert other["camera_name"] == "gopro13_9999" and other["serial"] == "C3509999999999"
    assert other["refused_clip"] is None
    new_dir = Path(other["output_dir"])
    assert new_dir.parent == runs
    moved = new_dir / "clips" / "GX010003.MP4"
    assert moved.stat().st_ino == inodes["GX010003.MP4"]  # the same file, moved
    assert moved.stat().st_size == synthetic_clip.path.stat().st_size
    assert len(list(runs.rglob("GX010003.MP4"))) == 1
    # Its own run: one clip, its own views, and its serial is not a mismatch.
    assert [c["name"] for c in other["clips"]] == ["GX010003.MP4"]
    assert other["captures"] == 15 and other["mismatch_count"] == 2
    rows = {row["field"]: row for row in other["clips"][0]["check"]}
    assert rows["serial"]["status"] == "ok"
    new_summary = json.loads(Path(other["summary_path"]).read_text())
    assert new_summary["recording"]["previous_run_id"] == status["run_id"]
    assert new_summary["recording"]["serial"] == "C3509999999999"
    saved = json.loads((new_dir / "config.json").read_text())
    assert saved["clips"] == ["GX010003.MP4"]
    assert saved["acquisition_mode"]["camera_name"] == "gopro13_9999"


def test_a_camera_switch_clears_the_results_and_can_be_undone(tmp_path, fresh_job):
    """No ffmpeg needed: the switch itself, before any view is read."""
    fresh_job.start(_config(), tmp_path / "runs")
    first_id, first_dir = fresh_job.run_id, fresh_job.output_dir
    first_generation = fresh_job._generation
    # Pretend the first camera's run solved.
    fresh_job._set(results=[{"model": "fisheye", "ok": True}], summary_path="x/summary.json",
                   acquisition_mode={"camera_name": "a"}, orbslam3={"path": "x"},
                   rejected_points=[[1, 2]])
    clip = first_dir / "clips" / "GX010003.MP4"
    clip.write_bytes(b"x" * 100)

    moved, generation = fresh_job._switch_camera(clip, "C3509999999999", "C3501234567890")
    status = fresh_job.status()
    assert generation != first_generation and status["run_id"].startswith("gopro13_9999_")
    assert status["results"] is None and status["summary_path"] is None
    assert status["acquisition_mode"] is None and status["orbslam3"] is None
    assert status["rejected_points"] == [] and status["needs_more_views"] is False
    # The new run knows its camera before any clip has joined it.
    assert status["clips"] == [] and status["serial"] == "C3509999999999"
    assert moved.is_file() and not clip.exists()

    # Undone: the new run's folder goes and the first run is current again.
    assert fresh_job._undo_switch() == first_generation
    status = fresh_job.status()
    assert not moved.parent.parent.exists() and first_dir.is_dir()
    assert (fresh_job.run_id, fresh_job.output_dir) == (first_id, first_dir)
    assert status["run_id"] == first_id and status["results"][0]["model"] == "fisheye"
    assert status["summary_path"] == "x/summary.json" and status["serial"] is None


@needs_ffmpeg
def test_another_camera_never_shows_the_previous_calibration(
    synthetic_clip, fresh_job, monkeypatch, tmp_path
):
    """Camera A solves. Camera B's clip then fails three ways: its folder cannot be
    made, its views cannot be read, and it has too few views. The first two leave A's
    run current and nothing of B on disk; the third is B's own run, with no results."""
    real_check, real_extract = recording.check_clip, recording.extract_views
    camera = {"serial": "C3501234567890", "extract_fails": False}
    seen_while_extracting = []

    def check_with_serial(path, rec, run_serial=None):
        result = real_check(path, rec, run_serial)
        result["metadata"] = {**result["metadata"], "serial": camera["serial"]}
        return result

    def extract(clip, **kwargs):
        seen_while_extracting.append(fresh_job.status())
        if camera["extract_fails"]:
            raise RuntimeError("the decoder stopped")
        return real_extract(clip, **kwargs)

    monkeypatch.setattr(recording, "check_clip", check_with_serial)
    monkeypatch.setattr(recording, "extract_views", extract)
    client = TestClient(app_module.app)
    runs = tmp_path / "runs"
    client.post(
        "/api/recording/start",
        json={"config": _config().model_dump(), "runs_dir": str(runs)},
    )
    assert _upload(client, synthetic_clip.path, "GX010001.MP4").status_code == 200
    first = _poll(client)
    assert first["state"] == "solved" and first["results"]
    first_dir = Path(first["output_dir"])
    before = _tree(first_dir)

    def only_the_first_run(status):
        assert status["run_id"] == first["run_id"] and status["results"] == first["results"]
        assert status["summary_path"] == first["summary_path"]
        assert status["acquisition_mode"] == first["acquisition_mode"]
        assert status["camera_name"] == "gopro13_7890" and status["serial"] == "C3501234567890"
        assert status["captures"] == 15 and status["previous_run_id"] is None
        assert [p.name for p in runs.iterdir()] == [first_dir.name]
        assert _tree(first_dir) == before
        assert fresh_job.busy is False

    # 1. The other camera's folder cannot be made (a full disk): refused, not stuck.
    camera["serial"] = "C3509999999999"
    real_make = fresh_job._make_run_dir

    def disk_full():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fresh_job, "_make_run_dir", disk_full)
    assert _upload(client, synthetic_clip.path, "GX010002.MP4").status_code == 200
    status = _poll(client)
    assert status["state"] == "error" and status["refused_clip"]["reason"] == "no_run_folder"
    assert status["refused_clip"]["serial"] == "C3509999999999"
    assert "(No space left on device)" in status["message"]
    assert f"This camera's run {first['run_id']} is still open." in status["message"]
    only_the_first_run(status)
    monkeypatch.setattr(fresh_job, "_make_run_dir", real_make)

    # 2. No views can be read from it: its new run is removed again.
    camera["extract_fails"] = True
    assert _upload(client, synthetic_clip.path, "GX010002.MP4").status_code == 200
    status = _poll(client)
    assert seen_while_extracting[-1]["run_id"].startswith("gopro13_9999_")
    assert seen_while_extracting[-1]["results"] is None
    assert status["state"] == "error" and status["refused_clip"]["reason"] == "failed"
    assert "the decoder stopped" in status["message"]
    assert "the folder made for that camera was removed" in status["message"]
    only_the_first_run(status)
    camera["extract_fails"] = False

    # The first camera's clip still joins the first camera's run.
    camera["serial"] = "C3501234567890"
    assert _upload(client, synthetic_clip.path, "GX010003.MP4").status_code == 200
    status = _poll(client)
    assert status["state"] == "solved" and status["run_id"] == first["run_id"]
    assert [c["name"] for c in status["clips"]] == ["GX010001.MP4", "GX010003.MP4"]
    first = status

    # 3. Too few views: B's own run, with none of A's calibration in it.
    camera["serial"] = "C3509999999999"
    short = build_clip(tmp_path / "short.mp4", poses=[POSES[0], POSES[2], POSES[4]])
    assert _upload(client, short.path, "GX010004.MP4").status_code == 200
    other = _poll(client)
    while_analysing = seen_while_extracting[-1]
    assert while_analysing["run_id"] == other["run_id"] and while_analysing["results"] is None
    assert other["state"] == "error" and other["needs_more_views"] is True
    assert other["run_id"].startswith("gopro13_9999_") and other["captures"] == 3
    for key in ("results", "summary_path", "acquisition_mode", "orbslam3"):
        assert other[key] is None, key
    assert other["rejected_points"] == []
    assert other["previous_run_id"] == first["run_id"]
    assert sorted(p.name for p in runs.iterdir()) == sorted([first_dir.name, other["run_id"]])
    # A's calibration is left exactly as it was.
    assert json.loads(Path(first["summary_path"]).read_text())["recording"]["serial"] == (
        "C3501234567890"
    )


@needs_ffmpeg
def test_a_clip_cut_short_warns_in_the_job(synthetic_clip, fresh_job, tmp_path):
    cut = _cut_short(synthetic_clip.path, tmp_path / "GX010001.MP4", 0.75)
    client = TestClient(app_module.app)
    client.post(
        "/api/recording/start",
        json={"config": _config().model_dump(), "runs_dir": str(tmp_path / "runs")},
    )
    assert _upload(client, cut, "GX010001.MP4").status_code == 200
    status = _poll(client)
    assert status["state"] == "solved", status["message"]
    (clip,) = status["clips"]
    rows = {row["field"]: row for row in clip["check"]}
    assert rows["complete"]["status"] == "mismatch"
    assert rows["complete"]["advice"].startswith("Only the first ")
    assert clip["mismatch_count"] == 3  # resolution, frame rate, cut short
    assert "Whole clip read" in status["mismatch_fields"]
    assert status["message"].endswith(
        "Only part of GX010001.MP4 could be read: copy it from the card again."
    )
    assert "2 settings differ" in status["message"]
    assert 8 <= status["captures"] < 15


def test_missing_ffmpeg_never_blames_or_removes_the_clip(tmp_path, fresh_job, monkeypatch):
    client = TestClient(app_module.app)
    body = {"config": _config().model_dump(), "runs_dir": str(tmp_path / "runs")}
    monkeypatch.setattr(recording, "missing_tools", lambda: ["ffmpeg", "ffprobe"])
    response = client.post("/api/recording/start", json=body)
    assert response.status_code == 503 and response.json()["detail"] == TOOLS_MISSING
    assert TOOLS_MISSING.endswith(
        "Install ffmpeg (it includes ffprobe), then drop the clip again."
    )
    monkeypatch.setattr(recording, "missing_tools", lambda: [])
    run_dir = Path(client.post("/api/recording/start", json=body).json()["run_dir"])

    # Refused before anything is written.
    monkeypatch.setattr(recording, "missing_tools", lambda: ["ffprobe"])
    response = client.put("/api/recording/clips?name=GX010001.MP4", content=b"x" * 1000)
    assert response.status_code == 503 and response.json()["detail"] == TOOLS_MISSING
    assert list((run_dir / "clips").iterdir()) == []
    assert client.get("/api/recording/status").json()["message"] == TOOLS_MISSING

    # Gone between the upload and the check: the clip stays where it is.
    monkeypatch.setattr(recording, "missing_tools", lambda: [])
    path = fresh_job.begin_upload("GX010001.MP4")
    path.write_bytes(b"x" * 1000)
    monkeypatch.setattr(recording, "missing_tools", lambda: ["ffmpeg"])
    fresh_job.start_processing(path)
    status = fresh_job.wait(30)
    assert status["state"] == "error" and status["message"] == TOOLS_MISSING
    assert path.is_file() and status["clips"] == [] and status["refused_clip"] is None


@needs_ffmpeg
def test_clips_the_run_cannot_use_leave_no_trace(synthetic_clip, fresh_job, tmp_path):
    """A wrong-size retake and a file that is not a video are refused like a clip from
    another camera: removed, and never counted in the mismatch warnings."""
    client = TestClient(app_module.app)
    # A preset whose recording section matches the synthetic clip: no mismatch at all.
    rec = RecordingConfig(width=1600, height=1200, fps=24, calibration_shutter="1/192")
    config = _config().model_copy(update={"recording": rec})
    started = client.post(
        "/api/recording/start",
        json={"config": config.model_dump(), "runs_dir": str(tmp_path / "runs")},
    ).json()
    run_dir = Path(started["run_dir"])
    assert _upload(client, synthetic_clip.path, "GX010001.MP4").status_code == 200
    first = _poll(client)
    assert first["state"] == "solved" and first["mismatch_count"] == 0, first["message"]

    small = build_clip(tmp_path / "small.mp4", poses=POSES[:3], size=(1200, 900))
    assert _upload(client, small.path, "GX010002.MP4").status_code == 200
    refused = _poll(client)
    assert refused["state"] == "error" and refused["refused_clip"]["reason"] == "different_size"
    assert "1200x900" in refused["message"] and "not used" in refused["message"]
    assert not (run_dir / "clips" / "GX010002.MP4").exists()

    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video" * 1000)
    assert _upload(client, junk, "junk.mp4").status_code == 200
    refused = _poll(client)
    assert refused["refused_clip"]["reason"] == "unreadable"
    assert "junk.mp4 is not a video ffmpeg can read" in refused["message"]
    assert str(tmp_path) not in refused["message"]  # no folders in what the UI shows
    assert not (run_dir / "clips" / "junk.mp4").exists()
    for status in (refused, client.get("/api/recording/status").json()):
        assert [c["name"] for c in status["clips"]] == ["GX010001.MP4"]
        assert status["mismatch_count"] == 0 and status["mismatch_clips"] == []
        assert status["results"] == first["results"] and status["captures"] == 15

    assert _upload(client, synthetic_clip.path, "GX010003.MP4").status_code == 200
    status = _poll(client)
    assert status["state"] == "solved" and status["refused_clip"] is None
    assert status["message"] == "Calibration solved."
    assert status["mismatch_fields"] == [] and status["mismatch_clips"] == []
    assert [c["name"] for c in status["clips"]] == ["GX010001.MP4", "GX010003.MP4"]
    summary = json.loads(Path(status["summary_path"]).read_text())
    assert [c["name"] for c in summary["recording"]["clips"]] == ["GX010001.MP4", "GX010003.MP4"]
    assert summary["recording"]["mismatch_fields"] == []
    saved = json.loads((run_dir / "config.json").read_text())
    assert saved["clips"] == ["GX010001.MP4", "GX010003.MP4"]
    assert sorted(p.name for p in (run_dir / "clips").iterdir()) == [
        "GX010001.MP4", "GX010003.MP4"
    ]


def test_an_upload_the_browser_drops_ends_in_a_plain_error(tmp_path, fresh_job):
    fresh_job.start(_config(), tmp_path / "runs")
    messages = iter(
        [
            {"type": "http.request", "body": b"x" * 1000, "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/api/recording/clips",
        "query_string": b"name=GX010001.MP4",
        "headers": [(b"content-length", b"10000000")],
    }
    request = app_module.Request(scope, receive)
    response = anyio.run(app_module.recording_upload, request, "GX010001.MP4")
    assert response.status_code == 400
    message = "The upload stopped before the end. Drop the clip again."
    assert json.loads(response.body) == {"detail": message}
    status = fresh_job.status()
    assert status["state"] == "error" and status["message"] == message
    assert list((Path(status["output_dir"]) / "clips").iterdir()) == []


@needs_ffmpeg
def test_a_clip_too_short_to_solve_asks_for_another(tmp_path, fresh_job):
    clip = build_clip(tmp_path / "short.mp4", poses=[POSES[0], POSES[2], POSES[4]])
    client = TestClient(app_module.app)
    client.post(
        "/api/recording/start",
        json={"config": _config().model_dump(), "runs_dir": str(tmp_path / "runs")},
    )
    assert _upload(client, clip.path, "short.mp4").status_code == 200
    status = _poll(client)
    assert status["state"] == "error" and status["needs_more_views"] is True
    assert status["captures"] == 3 == status["clips"][0]["counts"]["kept"]
    assert "Only 3 usable views so far; solving needs 8" in status["message"]


def test_busy_job_refuses_uploads_and_resets(tmp_path, fresh_job):
    client = TestClient(app_module.app)
    client.post(
        "/api/recording/start",
        json={"config": _config().model_dump(), "runs_dir": str(tmp_path / "runs")},
    )
    fresh_job._set(state="analysing")
    assert client.put("/api/recording/clips?name=a.mp4", content=b"x").status_code == 409
    assert client.post("/api/recording/new").status_code == 409
    # A refused start changes nothing, not even the runs folder.
    runs_before = fresh_job.runs_dir
    response = client.post(
        "/api/recording/start",
        json={"config": _config().model_dump(), "runs_dir": str(tmp_path / "elsewhere")},
    )
    assert response.status_code == 409 and fresh_job.runs_dir == runs_before
    fresh_job._set(state="ready")
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
