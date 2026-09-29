"""Render the README screenshots of the real UI with a simulated camera.

    uv run --with playwright python scripts/screenshot.py

Only the camera is simulated. The script renders the calib.io 5X5 board as a
HERO13 + Max Lens Mod 2.0 would see it (a Double Sphere camera with the
intrinsics measured on 2026-06-12, black outside the lens circle), runs the
app's own detector and solver on those frames with the gripper preset's models
(double_sphere and kannala_brandt need the OpenICC image), and serves the
resulting session states to the real UI through Playwright routes. The app
itself runs unmodified in this process.

It also checks layout facts a person would otherwise have to eyeball: no
horizontal page scroll, key panels visible, no text spilling out of buttons,
chips or figures, exactly one enabled amber (primary) button where a state has a
next action, the step bar agreeing with it, Next camera only after a solve,
nothing drawn over the idle preview, no discarded or surplus boxes in the
coverage legend, and no browser console errors. It exits non-zero if any fail.

Output: docs/screenshot.png (solved, desktop). The other shots (idle, idle with
custom settings, preview, capturing, paused with enough views, every model
failed, OpenICC models failed, solved after the route completed, a retake after
Stop, connecting, a stream error with views saved, after Next camera, and 390 px
mobile) are for review only and go to --review-dir.
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
        "max_samples": config.capture.max_samples,
        "preview_open": state != "idle",
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

    # Preview: the camera streams and the board is in view, no run started yet.
    first = views[0]
    preview = base_status(config, "preview", f"{first[1].marker_count} markers", gopro)
    preview.update(
        run_id=None,
        guide=guide_status([], first[1].pose, targets),
        markers=first[1].marker_count,
        pose=first[1].pose.as_dict(),
    )
    preview_frame = draw_detection(
        first[0], first[1], f"{first[1].marker_count} markers", selected=True
    )

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
    # The picture shows the last view, so the stats describe that view too.
    solved.update(markers=last[1].marker_count, pose=last[1].pose.as_dict())
    solved_frame = draw_detection(
        last[0], last[1], f"{last[1].marker_count} markers", selected=True
    )

    # Review-only states the page must not get wrong (hand-made from the above).
    # Paused with enough views: Solve is next, and the Solve step says so.
    paused = dict(capturing, state="paused", message="capture paused", captures=len(poses))
    paused.update(
        coverage=coverage_summary(poses, targets), guide=guide_status(poses[:16], None, targets)
    )
    # Every model failed: the page asks to Solve again, never offers Next camera.
    failed = dict(solved)
    failed["results"] = [
        {"model": r["model"], "ok": False, "error": f"{r['model']}: solver did not converge"}
        for r in results
    ]
    failed["message"] = "calibration solve finished; failed models: " + ", ".join(
        r["model"] for r in results
    )
    # A likely first run: the OpenICC image was never built, so Double Sphere and
    # Kannala-Brandt fail and only OpenCV fisheye solves. No UMI file was written,
    # so the page must ask for another Solve, not offer Next camera.
    no_image = (
        "Docker image 'gopro-charuco-openicc:d75dda5-p1' is not built on this machine. "
        "Run `uv run gopro-charuco setup-openicc` once (about 10 minutes), then solve again."
    )
    openicc = {"double_sphere", "kannala_brandt"}
    partial = dict(solved)
    partial["results"] = [
        {"model": r["model"], "ok": False, "error_type": "OpenICCError", "error": no_image}
        if r["model"] in openicc
        else r
        for r in results
    ]
    partial["message"] = "calibration solve finished; failed models: double_sphere, kannala_brandt"
    # The route finished before the solve: the legend still explains the dots.
    guide = solved["guide"]
    route_done = dict(solved)
    route_done["guide"] = dict(
        guide,
        checkpoints=[dict(c, complete=True, current=False) for c in guide["checkpoints"]],
        complete_count=guide["total_count"],
        complete=True,
    )
    # Solved, then Stop, with too little spread: a stopped run cannot be resumed.
    retake = dict(
        solved,
        state="idle",
        message="preview closed",
        preview_open=False,
        coverage=coverage_summary(poses[:8], targets),
        markers=0,
        pose=None,
    )
    # The first seconds after Open preview: no picture yet, so no guide marks.
    connecting = dict(preview, message="waiting for GoPro stream (4s)", markers=0, pose=None)
    # The stream died mid-run with enough views saved: Solve them.
    error = dict(
        paused,
        state="error",
        message="ffmpeg exited early",
        preview_open=False,
        markers=0,
        pose=None,
    )
    # After Next camera: the run is reset and the page asks for the next GoPro.
    next_camera = base_status(config, "idle", "stopped; ready for next camera", None)
    next_camera.update(run_id=None, video_bridge=None)
    return {
        "preview": (preview, preview_frame),
        "capturing": (capturing, capturing_frame),
        "paused": (paused, capturing_frame),
        "solved": (solved, solved_frame),
        "failed": (failed, solved_frame),
        "partial": (partial, solved_frame),
        "route_done": (route_done, solved_frame),
        "retake": (retake, b""),
        "connecting": (connecting, b""),
        "error": (error, b""),
        "next_camera": (next_camera, b""),
    }


# ---- browser ------------------------------------------------------------

LAYOUT_CHECK = """
({expect, primary, overlayEmpty, mustSay, mustNotSay, absent, steps, setupHighlighted}) => {
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
  // A closed dropdown cuts its choice short without scrolling, so measure the
  // text. A cut is allowed only when the full choice is written out next to it.
  const measure = document.createElement("canvas").getContext("2d");
  const ARROW_PX = 20;
  for (const sel of document.querySelectorAll("select")) {
    if (!sel.offsetParent || !sel.clientWidth || sel.closest("details:not([open])")) continue;
    const cs = getComputedStyle(sel);
    measure.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    const text = sel.selectedOptions[0]?.text || "";
    const pad = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
    const room = sel.clientWidth - pad - ARROW_PX;
    if (measure.measureText(text).width <= room) continue;
    const full = document.querySelector(`[data-full-choice="${sel.name || sel.id}"]`);
    if (!full || !full.offsetParent || !full.textContent.includes(text)) {
      problems.push(`dropdown ${sel.name || sel.id} cuts its choice short: "${text}"`);
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
  // One obvious next action: the enabled amber buttons are exactly the expected one.
  const shown = (el) => el.offsetParent && el.getBoundingClientRect().width > 0;
  const primaries = [...document.querySelectorAll(".btn-primary")]
    .filter((el) => shown(el) && !el.disabled)
    .map((el) => `#${el.id}`);
  const want = primary ? [primary] : [];
  if (JSON.stringify(primaries) !== JSON.stringify(want)) {
    problems.push(`primary buttons ${JSON.stringify(primaries)}, expected ${JSON.stringify(want)}`);
  }
  for (const el of document.querySelectorAll(".btn:disabled")) {
    if (shown(el) && !el.title) problems.push(`disabled #${el.id} does not say why`);
  }
  if (overlayEmpty) {
    const canvas = document.getElementById("guideOverlay");
    const data = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
    let drawn = 0;
    for (let i = 3; i < data.length; i += 4) drawn += data[i] > 0;
    if (drawn) problems.push(`guide overlay draws ${drawn} pixels with no live picture`);
  }
  const legend = document.querySelector(".coverage .legend").textContent;
  const oldSwatch = document.querySelector(".swatch-error, .swatch-surplus");
  if (oldSwatch || /discard|left out/i.test(legend)) {
    problems.push("coverage legend still lists discarded or surplus views");
  }
  for (const selector of absent) {
    const el = document.querySelector(selector);
    if (el && el.offsetParent) problems.push(`should not be shown: ${selector}`);
  }
  for (const [id, want] of Object.entries(steps)) {
    const got = document.getElementById(id).dataset.stepState;
    if (got !== want) problems.push(`step ${id} is ${got}, expected ${want}`);
  }
  if (document.querySelectorAll("[aria-current=step]").length > 1) {
    problems.push("more than one step is marked current");
  }
  const setupAmber = document.getElementById("presetSelect").classList.contains("select-primary");
  if (setupAmber !== setupHighlighted) {
    problems.push(`step 1 dropdown highlight is ${setupAmber}, expected ${setupHighlighted}`);
  }
  const body = document.body.innerText;
  for (const text of mustSay) {
    if (!body.includes(text)) problems.push(`missing text: "${text}"`);
  }
  for (const text of mustNotSay) {
    if (body.includes(text)) problems.push(`should not say: "${text}"`);
  }
  return problems;
}
"""


def custom_defaults(route):
    """Serve the start-up settings changed so they match no camera setup, as a
    plain `gopro-charuco serve` without --config does."""
    response = route.fetch()
    data = response.json()
    data["config"]["capture"]["target_samples"] += 7
    route.fulfill(response=response, json=data)


def shoot(browser, base_url, status, jpeg, path, viewport, checks, text_path=None, custom=False):
    errors = []
    page = browser.new_page(viewport=viewport, device_scale_factor=1)
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    if custom:
        page.route("**/api/defaults", custom_defaults)
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
    # Not "networkidle": a live session keeps polling the status every 150 ms.
    page.goto(base_url, wait_until="load")
    page.wait_for_function("() => lastStatus !== null")
    page.wait_for_timeout(600)
    if status is not None:
        page.evaluate("() => updateStatus(lastStatus)")  # redraw once the image has decoded
        page.wait_for_timeout(200)
    problems = page.evaluate(LAYOUT_CHECK, checks) + [f"console: {e}" for e in errors]
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
    desktop = {"width": 1440, "height": 900}
    solved_texts = [
        "Kannala–Brandt for UMI: matches Double Sphere",
        "UMI intrinsics JSON",
        "Blue: not reached",
    ]

    def checks(
        expect,
        primary,
        overlay_empty=False,
        texts=(),
        absent=(),
        steps=None,
        setup=False,
        not_texts=(),
    ):
        return {
            "expect": common + expect,
            "primary": primary,
            "overlayEmpty": overlay_empty,
            "mustSay": list(texts),
            "mustNotSay": list(not_texts),
            "absent": list(absent),
            "steps": steps or {},
            "setupHighlighted": setup,
        }

    def step_row(setup, connect, capture, solve):
        names = ("stepSetup", "stepConnect", "stepCapture", "stepSolve")
        return dict(zip(names, (setup, connect, capture, solve), strict=True))

    review = args.review_dir
    live = ["#preview", "#cameraReadout .chip"]
    # (name, status, frame, path, viewport, checks, custom start-up settings)
    shots = [
        (
            "idle",
            None,
            b"",
            review / "idle.png",
            desktop,
            checks(
                ["#previewEmpty"],
                "#previewBtn",
                overlay_empty=True,
                absent=["#nextCameraBtn", "#guideLegend"],
                steps=step_row("done", "current", "pending", "pending"),
            ),
            False,
        ),
        (
            # A plain `serve` with no --config: step 1 is next, so no button is amber.
            "custom-idle",
            None,
            b"",
            review / "custom-idle.png",
            desktop,
            checks(
                ["#previewEmpty"],
                None,
                overlay_empty=True,
                texts=["These settings match no camera setup"],
                steps=step_row("current", "pending", "pending", "pending"),
                setup=True,
            ),
            True,
        ),
        (
            "preview",
            *states["preview"],
            review / "preview.png",
            desktop,
            checks(
                [*live, "#guideLegend"],
                "#startRunBtn",
                absent=["#nextCameraBtn"],
                steps=step_row("done", "done", "current", "pending"),
            ),
            False,
        ),
        (
            "capturing",
            *states["capturing"],
            review / "capturing.png",
            desktop,
            checks(
                [*live, "#guideLegend", "[data-legend-next]"],
                None,
                absent=["#nextCameraBtn"],
                steps=step_row("done", "done", "current", "pending"),
            ),
            False,
        ),
        (
            "paused-enough",
            *states["paused"],
            review / "paused-enough.png",
            desktop,
            checks(
                live,
                "#solveBtn",
                texts=["Solve now, or Resume"],
                steps=step_row("done", "done", "done", "current"),
            ),
            False,
        ),
        (
            "solved",
            *states["solved"],
            args.out_dir / "screenshot.png",
            desktop,
            checks(
                [*live, "#resultsPanel", ".figure"],
                "#nextCameraBtn",
                texts=solved_texts,
                absent=["[data-legend-next]"],
                steps=step_row("done", "done", "done", "done"),
            ),
            False,
        ),
        (
            "all-failed",
            *states["failed"],
            review / "all-failed.png",
            desktop,
            checks(
                [*live, "#resultsPanel"],
                "#solveBtn",
                texts=["FAILED", "The last solve failed", "The solve failed: no model solved."],
                not_texts=["calibration solve complete"],
                absent=["#nextCameraBtn"],
                steps=step_row("done", "done", "done", "current"),
            ),
            False,
        ),
        (
            "partial-openicc-missing",
            *states["partial"],
            review / "partial-openicc-missing.png",
            desktop,
            checks(
                [*live, "#resultsPanel", "#nextCameraBtn"],
                "#solveBtn",
                texts=[
                    "INCOMPLETE",
                    "Calibration passed for OpenCV fisheye, but",
                    "_kannala_brandt.json) were not written",
                    "Calibration incomplete: Double Sphere and Kannala–Brandt for UMI did not",
                    "setup-openicc",
                ],
                not_texts=["PASS", "Calibration done."],
                steps=step_row("done", "done", "done", "current"),
            ),
            False,
        ),
        (
            "route-complete-solved",
            *states["route_done"],
            review / "route-complete-solved.png",
            desktop,
            checks(
                [*live, "#resultsPanel", "#guideLegend"],
                "#nextCameraBtn",
                texts=["Green: done"],
                absent=["[data-legend-next]", "#legendTodo"],
                steps=step_row("done", "done", "done", "done"),
            ),
            False,
        ),
        (
            "retake-stopped",
            *states["retake"],
            review / "retake-stopped.png",
            desktop,
            checks(
                ["#resultsPanel", "#previewEmpty"],
                "#previewBtn",
                overlay_empty=True,
                texts=["RETAKE", "Retake: Open preview, start a new run"],
                not_texts=["Solve again"],
                absent=["#guideLegend"],
                steps=step_row("done", "current", "done", "done"),
            ),
            False,
        ),
        (
            "connecting",
            *states["connecting"],
            review / "connecting.png",
            desktop,
            checks(
                [],
                None,
                overlay_empty=True,
                texts=["Connecting to the camera"],
                absent=["#guideLegend"],
                steps=step_row("done", "current", "pending", "pending"),
            ),
            False,
        ),
        (
            "error-with-views",
            *states["error"],
            review / "error-with-views.png",
            desktop,
            checks(
                ["#previewEmpty"],
                "#solveBtn",
                overlay_empty=True,
                texts=["Problem: ffmpeg exited early.", "Solve them now"],
                steps=step_row("done", "pending", "done", "current"),
            ),
            False,
        ),
        (
            "after-next-camera",
            *states["next_camera"],
            review / "after-next-camera.png",
            desktop,
            checks(
                ["#previewEmpty", "#guidePrompt"],
                "#previewBtn",
                overlay_empty=True,
                texts=["give it its own Camera name"],
                absent=["#nextCameraBtn", "#guideLegend", "#resultsPanel"],
                steps=step_row("done", "current", "pending", "pending"),
            ),
            False,
        ),
        (
            # Layout check only: the page must still work at phone width.
            "solved-mobile",
            *states["solved"],
            review / "solved-mobile.png",
            {"width": 390, "height": 844},
            checks(["#preview", "#resultsPanel"], "#nextCameraBtn", texts=solved_texts),
            False,
        ),
    ]
    failures = 0
    launch = {"executable_path": shutil.which("chromium")} if shutil.which("chromium") else {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        for name, status, frame, path, viewport, shot_checks, custom in shots:
            jpeg = (
                cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
                if len(frame)
                else b""
            )
            text_path = args.review_dir / f"{name}.txt"
            problems = shoot(
                browser, base_url, status, jpeg, path, viewport, shot_checks, text_path, custom
            )
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
