"""End-to-end Double Sphere solve through the real OpenICC Docker image.

Skipped unless the OpenICC image exists AND FRAMES_DIR points at a folder of
capture_*.jpg frames of the configured board, e.g.:

    FRAMES_DIR=~/Downloads/gopro13_umi_gripper_20260610_140106/frames \
        pytest tests/test_openicc_integration.py -v
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from gopro_charuco_calibrator.boards import make_caib_board
from gopro_charuco_calibrator.models import BoardConfig, CameraConfig
from gopro_charuco_calibrator.openicc import DEFAULT_DOCKER_IMAGE, run_double_sphere_model
from gopro_charuco_calibrator.solver import detect_frames


def _docker_image_present() -> bool:
    try:
        return (
            subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    os.environ.get("OPENICC_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE),
                ],
                capture_output=True,
                timeout=15,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


FRAMES_DIR = os.environ.get("FRAMES_DIR", "")

pytestmark = pytest.mark.skipif(
    not (FRAMES_DIR and Path(FRAMES_DIR).is_dir() and _docker_image_present()),
    reason="needs FRAMES_DIR env var and the openicc docker image",
)


def test_double_sphere_on_real_frames(tmp_path):
    board_config = BoardConfig(
        cols=int(os.environ.get("BOARD_COLS", 10)),
        rows=int(os.environ.get("BOARD_ROWS", 7)),
        square_m=float(os.environ.get("BOARD_SQUARE_M", 0.021)),
        marker_m=float(os.environ.get("BOARD_MARKER_M", 0.015)),
        aruco_dict=os.environ.get("BOARD_DICT", "DICT_4X4_50"),
        start_id=int(os.environ.get("BOARD_START_ID", 0)),
        marker_count=int(os.environ.get("BOARD_MARKER_COUNT", 35)),
    )
    _board, obj_by_id = make_caib_board(board_config)
    image_size, records = detect_frames(
        frames_dir=Path(FRAMES_DIR), board_config=board_config, min_markers=8
    )
    result = run_double_sphere_model(
        output_dir=tmp_path,
        camera=CameraConfig(camera_name="integration"),
        board_config=board_config,
        image_size=image_size,
        records=records,
        obj_by_id=obj_by_id,
    )
    assert result["ok"] is True
    assert result["rms"] < 3.0
    artifact = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
    assert artifact["intrinsic_type"] == "DOUBLE_SPHERE"
