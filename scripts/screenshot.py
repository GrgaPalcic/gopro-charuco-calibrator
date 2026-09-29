"""Render the README screenshots of the real UI with a simulated camera.

    uv run --with playwright python scripts/screenshot.py

Only the camera is simulated. The script renders the calib.io 5X5 board as a
HERO13 + Max Lens Mod 2.0 would see it (a Double Sphere camera with the
intrinsics measured on 2026-06-12, black outside the lens circle), runs the
app's own detector and solver on those frames (double_sphere needs the openicc
image), and serves the resulting session states to the real UI through
Playwright routes. The app itself runs unmodified in this process.

It also checks layout facts a person would otherwise have to eyeball: no
horizontal page scroll, key panels visible, no text spilling out of buttons,
chips or figures, and no browser console errors. It exits non-zero if any fail.

Output: docs/screenshot.png (solved, desktop) and docs/screenshot-mobile.png
(solved, 390 px wide), plus idle/capturing review shots in --review-dir.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import cv2
import cv2.aruco as aruco
import numpy as np
import uvicorn

from gopro_charuco_calibrator import presets
from gopro_charuco_calibrator.app import app, set_default_config
from gopro_charuco_calibrator.boards import make_caib_board, resolve_dictionary
from gopro_charuco_calibrator.capture import _discarded_points
from gopro_charuco_calibrator.coverage import coverage_summary
from gopro_charuco_calibrator.detection import detect_markers, draw_detection
from gopro_charuco_calibrator.gopro import (
    REPORTED_SETTING_FIELDS,
    SETTING_DEFS,
    _setting_label,
    describe_acquisition_mode,
)
from gopro_charuco_calibrator.guide import guide_status
from gopro_charuco_calibrator.solver import solve_from_frames

REPO = Path(__file__).resolve().parent.parent
PRESET = "gopro13_umi_gripper_fisheye_1080p"
SIZE = (1920, 1080)
# HERO13 + Max Lens Mod 2.0, USB webcam Wide 1080p, OpenICC solve of 2026-06-12.
DS = {"f": 629.19, "cx": 949.64, "cy": 538.89, "xi": 0.00166, "alpha": 0.708}
LENS_HALF_ANGLE = np.radians(83.5)  # 167 deg clean fisheye
TEXTURE_PX_PER_M = 4000.0


# ---- synthetic camera ----------------------------------------------------


def ds_unproject(u, v):
    """Double Sphere pixel -> unit ray (Usenko et al. 2018, eq. 47-51)."""
    f, cx, cy, xi, alpha = DS["f"], DS["cx"], DS["cy"], DS["xi"], DS["alpha"]
    mx, my = (u - cx) / f, (v - cy) / f
    r2 = mx * mx + my * my
    valid = r2 <= 1.0 / (2.0 * alpha - 1.0) if alpha > 0.5 else np.ones_like(r2, bool)
    r2c = np.where(valid, r2, 0.0)
    mz = (1 - alpha * alpha * r2c) / (
        alpha * np.sqrt(np.maximum(1 - (2 * alpha - 1) * r2c, 0)) + 1 - alpha
    )
    k = (mz * xi + np.sqrt(mz * mz + (1 - xi * xi) * r2c)) / (mz * mz + r2c)
    ray = np.stack([k * mx, k * my, k * mz - xi], axis=-1)
    ray /= np.linalg.norm(ray, axis=-1, keepdims=True)
    return ray, valid


def board_texture(board_config):
    """The printed board: markers on alternating cells, black squares between."""
    sq = board_config.square_m * TEXTURE_PX_PER_M
    width = int(round(board_config.cols * sq))
    height = int(round(board_config.rows * sq))
    tex = np.full((height, width), 255, np.uint8)
    _board, obj_by_id = make_caib_board(board_config)
    marker_cells = set()
    dictionary = resolve_dictionary(board_config.aruco_dict)
    side = int(round(board_config.marker_m * TEXTURE_PX_PER_M))
    for marker_id, corners in obj_by_id.items():
        x0, y0 = corners[0][0] * TEXTURE_PX_PER_M, corners[0][1] * TEXTURE_PX_PER_M
        marker_cells.add((int(y0 // sq), int(x0 // sq)))
        image = aruco.generateImageMarker(dictionary, int(marker_id), side)
        tex[int(round(y0)) : int(round(y0)) + side, int(round(x0)) : int(round(x0)) + side] = image
    parity = next(iter(marker_cells))
    for row in range(board_config.rows):
        for col in range(board_config.cols):
            if (row, col) in marker_cells or (row + col) % 2 == sum(parity) % 2:
                continue
            tex[int(row * sq) : int((row + 1) * sq), int(col * sq) : int((col + 1) * sq)] = 0
    return tex


class Camera:
    def __init__(self, board_config):
        self.board_config = board_config
        self.texture = board_texture(board_config)
        u, v = np.meshgrid(
            np.arange(SIZE[0], dtype=np.float64), np.arange(SIZE[1], dtype=np.float64)
        )
        self.rays, valid = ds_unproject(u, v)
        self.inside = valid & (np.arccos(np.clip(self.rays[..., 2], -1, 1)) < LENS_HALF_ANGLE)
        yy, xx = np.mgrid[0 : SIZE[1], 0 : SIZE[0]]
        self.room = (38 + 22 * (yy / SIZE[1]) + 6 * np.sin(xx / 97.0)).astype(np.uint8)
        self.center = np.asarray(
            [board_config.pattern_width_m / 2, board_config.pattern_height_m / 2, 0.0]
        )

    def render(self, rotation, tvec):
        origin_b = rotation.T @ (-tvec) + self.center
        dirs_b = self.rays @ rotation  # (R^T d) for every ray
        with np.errstate(divide="ignore", invalid="ignore"):
            s = -origin_b[2] / dirs_b[..., 2]
        hit = origin_b[:2] + s[..., None] * dirs_b[..., :2]
        map_x = (hit[..., 0] * TEXTURE_PX_PER_M).astype(np.float32)
        map_y = (hit[..., 1] * TEXTURE_PX_PER_M).astype(np.float32)
        map_x[~(s > 0)] = -1
        board = cv2.remap(
            self.texture,
            map_x,
            map_y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        on_board = (
            cv2.remap(
                np.full_like(self.texture, 255),
                map_x,
                map_y,
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            > 127
        )
        gray = np.where(on_board, board, self.room)
        gray = np.where(self.inside, gray, 0).astype(np.uint8)
        return cv2.cvtColor(cv2.GaussianBlur(gray, (3, 3), 0.6), cv2.COLOR_GRAY2BGR)


def rotation_between(a, b):
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3)
    vx = np.asarray([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1 / (1 + c))


def pose_towards(u, v, distance, tilt):
    ray, _ = ds_unproject(np.asarray([u], float), np.asarray([v], float))
    ray = ray[0]
    base = rotation_between(np.asarray([0.0, 0.0, 1.0]), ray)
    tilt_r, _ = cv2.Rodrigues(np.asarray(tilt, dtype=np.float64))
    return base @ tilt_r, distance * ray


def synthetic_views(camera, count, rng):
    """Board poses spread over the whole image, near and far, some tilted."""
    views = []
    dictionary = resolve_dictionary(camera.board_config.aruco_dict)
    while len(views) < count:
        u = rng.uniform(0.12, 0.88) * SIZE[0]
        v = rng.uniform(0.12, 0.88) * SIZE[1]
        rotation, tvec = pose_towards(
            u, v, rng.uniform(0.2, 1.0), rng.uniform([-0.55, -0.55, -0.4], [0.55, 0.55, 0.4])
        )
        frame = camera.render(rotation, tvec)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        detection = detect_markers(gray, SIZE, camera.board_config, dictionary)
        if detection is not None and detection.marker_count >= 12:
            views.append((frame, detection))
    return views


# ---- session states -----------------------------------------------------


def fake_gopro(config):
    settings = {"webcam_digital_lens": config.gopro.webcam_fov, "max_lens_mod": 2, "hypersmooth": 0}
    assert set(settings) == set(REPORTED_SETTING_FIELDS)
    assert all("setting_id" in SETTING_DEFS[field] for field in settings)
    labels = {field: str(_setting_label(field, value)) for field, value in settings.items()}
    return {
        "enabled": True,
        "ok": True,
        "camera_state": {"ok": True, "settings": settings, "labels": labels},
        "warnings": [],
    }


def base_status(config, state, message, gopro):
    return {
        "state": state,
        "run_id": f"{config.camera.camera_name}_20260929_101500",
        "message": message,
        "captures": 0,
        "target_samples": config.capture.target_samples,
        "coverage": coverage_summary([], config.coverage_targets),
        "guide": guide_status([], None, config.coverage_targets),
        "gopro": gopro,
        "video_bridge": {"enabled": True, "ok": True},
        "rejected_points": [],
        "image_size": list(SIZE),
        "markers": 0,
        "pose": None,
    }


def build_states(config, workdir, views):
    gopro = fake_gopro(config)
    targets = config.coverage_targets
    poses = [detection.pose for _frame, detection in views]

    # Capturing: part of the route done, the live board sitting on the next target.
    done, live = poses[:16], views[16]
    capturing = base_status(config, "capturing", "saved capture_016.jpg", gopro)
    capturing.update(
        captures=len(done),
        coverage=coverage_summary(done, targets),
        guide=guide_status(done, live[1].pose, targets),
        markers=live[1].marker_count,
        pose=live[1].pose.as_dict(),
    )
    capturing_frame = draw_detection(
        live[0], live[1], f"{live[1].marker_count} markers", selected=True
    )

    # Solved: the real solver on all rendered frames.
    frames_dir = workdir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for index, (frame, _detection) in enumerate(views, start=1):
        cv2.imwrite(
            str(frames_dir / f"capture_{index:03d}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 95]
        )
    summary = solve_from_frames(
        frames_dir=frames_dir,
        output_dir=workdir,
        camera=config.camera,
        board_config=config.board,
        solver=config.solver,
        coverage_targets=targets,
    )
    # Show output paths where a real run writes them (runs/<run id>/...).
    shown = f"runs/{config.camera.camera_name}_20260929_101500"
    results = json.loads(json.dumps(summary["results"]).replace(str(workdir), shown))
    solved = base_status(config, "solved", "calibration solve complete", gopro)
    solved.update(
        captures=len(views),
        coverage=summary["coverage"],
        guide=guide_status(poses, None, targets),
        results=results,
        rejected_points=_discarded_points(summary),
        acquisition_mode=describe_acquisition_mode(config, gopro),
        summary_path=f"{shown}/caib_marker_board_calibration_summary.json",
    )
    last = views[-1]
    solved_frame = draw_detection(
        last[0], last[1], f"{last[1].marker_count} markers", selected=True
    )
    return {"capturing": (capturing, capturing_frame), "solved": (solved, solved_frame)}


# ---- browser ------------------------------------------------------------

LAYOUT_CHECK = """
(expect) => {
  const problems = [];
  const vw = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth > vw + 1) {
    problems.push(`page scrolls horizontally: ${document.documentElement.scrollWidth} > ${vw}`);
  }
  for (const el of document.querySelectorAll(".app *")) {
    const r = el.getBoundingClientRect();
    if (r.width && r.right > vw + 1 && !el.closest("[hidden]")) {
      const name = `${el.tagName.toLowerCase()}#${el.id || ""}.${el.className}`;
      problems.push(`${name} ends at ${Math.round(r.right)} > ${vw}`);
    }
  }
  const texts = ".btn, .chip, .figure-value, .stats dd, .step-title, summary";
  for (const el of document.querySelectorAll(texts)) {
    if (el.offsetParent && el.scrollWidth > el.clientWidth + 1) {
      const text = el.textContent.trim().slice(0, 40);
      problems.push(`text spills out of ${el.className || el.tagName}: "${text}"`);
    }
  }
  for (const selector of expect) {
    const el = document.querySelector(selector);
    const r = el && el.getBoundingClientRect();
    if (!el || !r.width || !r.height || el.closest("[hidden]")) {
      problems.push(`not visible: ${selector}`);
    }
  }
  const img = document.getElementById("preview");
  if (expect.includes("#preview") && !(img.complete && img.naturalWidth > 0)) {
    problems.push("preview image not loaded");
  }
  return problems;
}
"""


def shoot(browser, base_url, status, jpeg, path, viewport, expect, text_path=None):
    errors = []
    page = browser.new_page(viewport=viewport, device_scale_factor=1)
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    if status is not None:
        body = json.dumps(status)
        page.route(
            "**/api/session/status",
            lambda route: route.fulfill(status=200, content_type="application/json", body=body),
        )
        for pattern in ("**/api/session/stream.mjpg", "**/api/session/latest.jpg*"):
            page.route(
                pattern,
                lambda route: route.fulfill(status=200, content_type="image/jpeg", body=jpeg),
            )
    page.goto(base_url, wait_until="networkidle")
    page.wait_for_timeout(600)
    if status is not None:
        page.evaluate("() => updateStatus(lastStatus)")  # redraw once the image has decoded
        page.wait_for_timeout(200)
    problems = page.evaluate(LAYOUT_CHECK, expect) + [f"console: {e}" for e in errors]
    page.screenshot(path=str(path), full_page=True)
    if text_path is not None:
        steps = page.evaluate(
            "() => [...document.querySelectorAll('.step')]"
            ".map(s => `${s.id}=${s.dataset.stepState}`)"
        )
        text_path.write_text(
            f"steps: {' '.join(steps)}\n\n{page.inner_text('body')}\n", encoding="utf-8"
        )
    page.close()
    return problems


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, default=REPO / "docs")
    parser.add_argument(
        "--review-dir", type=Path, default=Path(tempfile.gettempdir()) / "gopro-ui-review"
    )
    parser.add_argument("--views", type=int, default=40)
    args = parser.parse_args()
    args.review_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    _title, config = presets.get_preset(PRESET)
    config = config.model_copy(deep=True)
    camera = Camera(config.board)
    print(f"rendering {args.views} synthetic Max Lens Mod views…", flush=True)
    views = synthetic_views(camera, args.views, np.random.default_rng(29))
    with tempfile.TemporaryDirectory() as tmp:
        print("solving them with the app's solver…", flush=True)
        states = build_states(config, Path(tmp), views)
    for result in states["solved"][0]["results"]:
        state = "ok" if result.get("ok") else f"FAILED: {result.get('error', '')[:120]}"
        print(f"  {result['model']}: rms {result.get('rms')} ({state})")

    set_default_config(config)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    base_url = f"http://127.0.0.1:{port}/"

    common = [".steps", "#scatter", ".setup", ".previewWrap"]
    shots = [
        (
            "idle",
            None,
            b"",
            args.review_dir / "idle.png",
            {"width": 1440, "height": 900},
            common + ["#previewEmpty"],
        ),
        (
            "capturing",
            *states["capturing"],
            args.review_dir / "capturing.png",
            {"width": 1440, "height": 900},
            common + ["#preview", "#cameraReadout .chip"],
        ),
        (
            "solved",
            *states["solved"],
            args.out_dir / "screenshot.png",
            {"width": 1440, "height": 900},
            common + ["#preview", "#cameraReadout .chip", "#resultsPanel", ".figure"],
        ),
        (
            "solved-mobile",
            *states["solved"],
            args.out_dir / "screenshot-mobile.png",
            {"width": 390, "height": 844},
            common + ["#preview", "#resultsPanel"],
        ),
    ]
    failures = 0
    launch = {"executable_path": shutil.which("chromium")} if shutil.which("chromium") else {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        for name, status, frame, path, viewport, expect in shots:
            jpeg = (
                cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
                if len(frame)
                else b""
            )
            text_path = args.review_dir / f"{name}.txt"
            problems = shoot(browser, base_url, status, jpeg, path, viewport, expect, text_path)
            print(f"{name}: {path}  {'ok' if not problems else 'PROBLEMS'}")
            for problem in problems:
                print(f"  - {problem}")
            failures += bool(problems)
        browser.close()
    server.should_exit = True
    thread.join(timeout=5)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
