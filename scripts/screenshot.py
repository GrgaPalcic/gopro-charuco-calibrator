"""Render the README screenshots of the real UI with a simulated camera.

    uv run --with playwright python scripts/screenshot.py

Only the camera is simulated. The script renders the calib.io 5X5 board as a
HERO13 + Max Lens Mod 2.0 would see it (gopro_charuco_calibrator/synthetic.py: a
Double Sphere camera with the intrinsics measured on 2026-06-12, black outside the
lens circle), runs the app's own detector and solver on those frames with the
gripper preset's models (double_sphere and kannala_brandt need the OpenICC image), and serves the
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

The From a recording route's shots (rec-*.png, review only) are served the same
way: the route's Labs QR codes and guide come from the real app, and only
/api/recording/status is faked. Its results are the app's own solve of 4:3
synthetic frames (gopro_charuco_calibrator/synthetic.recording_camera, 1600x1200,
made-up intrinsics) shown as if they came from a 4000x3000 HERO13 clip, so the
figures (focal, centre, errors) are those of the synthetic camera. The clip's
file facts and metadata are made up for the page (no real HERO13 clip has been
read yet); the clip-check rows are built from them by the app's own
clipcheck.compare. The QR codes on the settings shot are decoded back from the
screenshot.
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
import numpy as np
import uvicorn

from gopro_charuco_calibrator import presets
from gopro_charuco_calibrator.app import app, set_default_config
from gopro_charuco_calibrator.capture import _discarded_points
from gopro_charuco_calibrator.clipcheck import compare
from gopro_charuco_calibrator.coverage import PoseParams, coverage_summary
from gopro_charuco_calibrator.detection import draw_detection
from gopro_charuco_calibrator.gopro import (
    REPORTED_SETTING_FIELDS,
    SETTING_DEFS,
    _setting_label,
    describe_acquisition_mode,
)
from gopro_charuco_calibrator.guide import default_checkpoints, guide_status
from gopro_charuco_calibrator.labs import labs_command
from gopro_charuco_calibrator.recording import (
    RecordingJob,
    camera_name_from_serial,
    describe_recording_mode,
    empty_counts,
    no_results,
)
from gopro_charuco_calibrator.solver import solve_from_frames
from gopro_charuco_calibrator.synthetic import SIZE, Camera, recording_camera, synthetic_views

REPO = Path(__file__).resolve().parent.parent
PRESET = "gopro13_mlm2_adwal002"
# A shipped camera setup with no recording section (live route only).
LIVE_ONLY_PRESET = "gopro13_wide_1080p"
UWLM_PRESET = "gopro13_uwlm_aewal001"
ROUTE_KEY = "gopro-charuco.route"


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


# ---- recording-route states -----------------------------------------------

# Made up for the page: a placeholder serial (it names the camera gopro13_1234) and
# clip facts in the shape clipcheck reads. None of these were read from a camera.
DEMO_SERIAL = "DEMO00001234"
CLIP = "GX010042.MP4"
CLIP_BYTES = 1_402_000_000  # about 90 s at the HERO13's ~120 Mb/s
# How 90 s at 12 samples a second might split (made up; adds up to 1080).
DEMO_COUNTS = {"samples": 1080, "no_board": 212, "blurred": 57, "moving": 604, "duplicate": 167}


def demo_clip(rec, *, mismatch):
    """A clip entry as the job lists it, with check rows from the app's own compare."""
    probe = {
        "width": rec.width,
        "height": rec.height,
        "avg_frame_rate": 29.97 if mismatch else 59.94,
        "r_frame_rate": 29.97 if mismatch else 59.94,
        "codec": "hevc",
        "duration_s": 90.1,
        "rotation": 0,
    }
    meta = {
        "model": "HERO13 Black",
        "serial": DEMO_SERIAL,
        "eise": "Y" if mismatch else "N",
        "eisa": "HS Boost" if mismatch else "N/A",
        "vfov": None,  # which letter a lens mod writes is not documented
        "zfov": None,
        "shutter_s": None,  # SHUT on a HERO13 is unverified: the row stays unknown
        "has_imu": True,
        "gpmf_found": True,
    }
    rows = compare(probe, meta, rec)
    return {
        "name": CLIP,
        "size_bytes": CLIP_BYTES,
        "check": rows,
        "mismatch_count": sum(row["status"] == "mismatch" for row in rows),
        "probe": probe,
        "metadata": meta,
        "counts": None,
    }


def rec_status(config, state, message, **updates):
    """The recording job's status, every key present as in RecordingJob.status()."""
    targets = config.coverage_targets
    status = {
        "state": state,
        "stage": None,
        "progress": 0.0,
        "message": message,
        "upload": None,
        **no_results(),
        "route": "recording",
        "run_id": None,
        "run_dir": "",
        "output_dir": "",
        "camera_name": None,
        "camera_named_from_serial": False,
        "camera_name_note": None,
        "serial": None,
        "image_size": None,
        "captures": 0,
        "coverage": coverage_summary([], targets),
        "guide": guide_status([], None, targets),
        "clips": [],
        "counts": empty_counts(),
        "mismatch_count": 0,
        "mismatch_fields": [],
        "mismatch_clips": [],
        "refused_clip": None,
        "previous_run_id": None,
        "previous_output_dir": None,
        "camera_switch": None,
        "min_frames": config.solver.min_frames,
        "preview_open": False,
    }
    with tempfile.TemporaryDirectory() as tmp:
        assert set(status) == set(RecordingJob(runs_dir=Path(tmp)).status())
    status.update(updates)
    return status


def build_recording_states(config, workdir, views):
    rec = config.recording
    targets = config.coverage_targets
    poses = [detection.pose for _frame, detection in views]
    size = [rec.width, rec.height]  # shown as the HERO13 clip's size (see the docstring)
    first_name = config.camera.camera_name
    run_id = f"{first_name}_20260930_101500"
    named = camera_name_from_serial(DEMO_SERIAL)
    named_run = f"{named}_20260930_101500"

    frames_dir = workdir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for index, (frame, _detection) in enumerate(views, start=1):
        cv2.imwrite(
            str(frames_dir / f"capture_{index:03d}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 95]
        )
    camera = config.camera.model_copy(
        update={"camera_name": named, "width": views[0][0].shape[1], "height": views[0][0].shape[0]}
    )
    summary = solve_from_frames(
        frames_dir=frames_dir,
        output_dir=workdir,
        camera=camera,
        board_config=config.board,
        solver=config.solver,
        coverage_targets=targets,
    )
    # The job's own ORB-SLAM3 rewrite, for a clip of the preset's size.
    summary["image_size"] = size
    job = RecordingJob(runs_dir=workdir)
    job.config = config
    orbslam3 = job._write_orbslam3(summary)
    shown = f"runs/{named_run}"
    results = json.loads(json.dumps(summary["results"]).replace(str(workdir), shown))
    if orbslam3 is not None:
        orbslam3["path"] = orbslam3["path"].replace(str(workdir), shown)

    base = rec_status(config, "idle", "no recording run yet")
    run = {
        "run_id": run_id,
        "run_dir": f"runs/{run_id}",
        "output_dir": f"runs/{run_id}",
        "camera_name": first_name,
    }
    uploading = rec_status(
        config, "uploading", f"Copying {CLIP} into the run folder.",
        stage="upload", progress=0.43,
        upload={"name": CLIP, "received_bytes": 602_860_000, "total_bytes": CLIP_BYTES},
        **run,
    )
    # The job names the run after the serial straight after the clip check.
    named_dir = {"run_id": named_run, "run_dir": f"runs/{named_run}",
                 "output_dir": f"runs/{named_run}", "camera_name": named,
                 "camera_named_from_serial": True}
    analysing = rec_status(
        config, "analysing", f"Picking views from {CLIP}.",
        stage="extract", progress=0.37, image_size=size, serial=DEMO_SERIAL,
        clips=[demo_clip(rec, mismatch=False)], **named_dir,
    )

    def solved_status(clip, kept_poses, message):
        counts = {**empty_counts(), **DEMO_COUNTS, "kept": len(kept_poses)}
        clip = dict(clip, counts=counts, kept=[f"capture_{i:03d}.jpg" for i in range(1, 41)])
        mismatches = [row["label"] for row in clip["check"] if row["status"] == "mismatch"]
        mode_camera = camera.model_copy(update={"width": size[0], "height": size[1]})
        return rec_status(
            config, "solved", message,
            stage="done", progress=1.0,
            run_id=named_run, run_dir=shown, output_dir=shown, camera_name=named,
            camera_named_from_serial=True,
            camera_name_note=(
                f"Named this camera {named} from its serial number, so its files match the "
                "physical camera."
            ),
            serial=DEMO_SERIAL, image_size=size, captures=len(kept_poses),
            coverage=coverage_summary(kept_poses, targets),
            guide=guide_status(kept_poses, None, targets),
            clips=[clip], counts=counts,
            mismatch_count=len(mismatches), mismatch_fields=mismatches,
            mismatch_clips=[CLIP] if mismatches else [],
            results=results,
            summary_path=f"{shown}/caib_marker_board_calibration_summary.json",
            acquisition_mode=describe_recording_mode(mode_camera, rec, clip),
            rejected_points=_discarded_points(summary),
            orbslam3=orbslam3,
        )

    solved = solved_status(demo_clip(rec, mismatch=False), poses, "Calibration solved.")
    mismatch = solved_status(
        demo_clip(rec, mismatch=True), poses,
        f"Calibration solved. 2 settings differ from the preset in {CLIP}: "
        "frame rate, hypersmooth.",
    )
    def needs_more(kept_poses):
        """The job's state when the views picked so far are too few to solve: no
        results, and a request for another clip."""
        counts = {**empty_counts(), **DEMO_COUNTS, "kept": len(kept_poses)}
        counts["duplicate"] += len(poses) - len(kept_poses)
        status = solved_status(demo_clip(rec, mismatch=False), kept_poses, "")
        status.update(no_results())
        status.update(
            state="error", stage="extract", counts=counts, needs_more_views=True,
            message=(
                f"Only {len(kept_poses)} usable views so far; solving needs "
                f"{config.solver.min_frames}. Record another clip that holds the board still "
                "at more positions, and add it to this run."
            ),
        )
        status["clips"] = [
            dict(status["clips"][0], counts=counts,
                 kept=[f"capture_{i:03d}.jpg" for i in range(1, len(kept_poses) + 1)]),
        ]
        return status

    # A first clip with too few usable views, some positions missing.
    retake = needs_more(poses[:9])
    # Every position covered, one view each, but still fewer views than solving needs
    # (23 < 25). Made up: the kept views sit exactly on the guide's positions.
    covered = needs_more([
        PoseParams(cp.x, cp.y, cp.size, cp.skew) for cp in default_checkpoints(targets)
    ])
    # A second clip in another mode is refused; the run's earlier result stays. The
    # clip is not in the camera setup's mode, so the job asks for it to be recorded again.
    refused_message = (
        "GX010043.MP4 is 1920x1080 but this run's first clip was 4000x3000. The clip was not "
        "used and was removed from this run's folder. Record every clip of one run in the same "
        "mode. This clip is not in the camera setup's mode (4000x3000): set the camera up again "
        "as in step 2, record the clip again, then drop the new clip."
    )
    refused_clip = {
        "name": "GX010043.MP4",
        "reason": "different_size",
        "serial": DEMO_SERIAL,
        "run_serial": DEMO_SERIAL,
        "check": [],
        "message": refused_message,
        "size": [1920, 1080],
        "matches_setup": False,
    }
    refused = dict(
        solved, state="error", stage="check", message=refused_message, refused_clip=refused_clip,
    )
    # The clip check's problem stands and a later clip is refused as well.
    mismatch_refused = dict(
        mismatch, state="error", stage="check", message=refused_message,
        refused_clip=refused_clip,
    )
    # A retake being solved after a too-few-views result and a refused clip: the server
    # clears needs_more_views and refused_clip only once the next clip gets that far.
    retake_solving = dict(
        retake, state="solving", stage="solve", progress=0.0, message="Solving.",
        refused_clip=refused_clip,
    )
    return {
        "idle": base,
        "uploading": uploading,
        "analysing": analysing,
        "mismatch": mismatch,
        "retake": retake,
        "covered": covered,
        "solved": solved,
        "refused": refused,
        "mismatch_refused": mismatch_refused,
        "retake_solving": retake_solving,
    }


# ---- browser ------------------------------------------------------------

LAYOUT_CHECK = """
(checks) => {
  const {expect, primary, overlayEmpty, mustSay, mustNotSay, absent, steps} = checks;
  const {setupHighlighted, drawn, within, fold} = checks;
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
  for (const el of document.querySelectorAll(".qr-card img")) {
    if (el.offsetParent && !(el.complete && el.naturalWidth > 0)) {
      problems.push(`image not loaded: #${el.id}`);
    }
  }
  for (const selector of drawn) {
    const canvas = document.querySelector(selector);
    const data = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
    let count = 0;
    for (let i = 3; i < data.length; i += 4) count += data[i] > 0;
    if (count < 100) problems.push(`nothing drawn on ${selector}`);
  }
  // Loud things must be on the first screen, not below the fold.
  for (const selector of fold || []) {
    const el = document.querySelector(selector);
    const top = el ? el.getBoundingClientRect().top + window.scrollY : Infinity;
    if (top >= window.innerHeight) {
      problems.push(`${selector} starts at ${Math.round(top)} px, below the first screen`);
    }
  }
  for (const [selector, texts] of Object.entries(within)) {
    const el = document.querySelector(selector);
    const text = el ? el.innerText : "";
    for (const want of texts) {
      if (!text.includes(want)) problems.push(`${selector} does not say: "${want}"`);
    }
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
    // Inside a closed disclosure an element keeps its offsetParent, but is not on show.
    if (el && el.offsetParent && !el.closest("details:not([open])")) {
      problems.push(`should not be shown: ${selector}`);
    }
  }
  for (const [id, want] of Object.entries(steps)) {
    const got = document.getElementById(id).dataset.stepState;
    if (got !== want) problems.push(`step ${id} is ${got}, expected ${want}`);
  }
  // The step bar shows the chosen route's steps, and only those.
  if (Object.keys(steps).length) {
    const visible = [...document.querySelectorAll(".step")]
      .filter((el) => el.offsetParent)
      .map((el) => el.id);
    const want = JSON.stringify(Object.keys(steps));
    if (JSON.stringify(visible) !== want) {
      problems.push(`steps shown ${JSON.stringify(visible)}, expected ${want}`);
    }
  }
  if (document.querySelectorAll("[aria-current=step]").length > 1) {
    problems.push("more than one step is marked current");
  }
  for (const el of document.querySelectorAll("[aria-current=step]")) {
    if (!el.offsetParent) problems.push(`hidden step #${el.id} is marked current`);
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


def decode_qr(png: bytes) -> str:
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    text, _points, _straight = cv2.QRCodeDetector().detectAndDecode(image)
    return text


def shoot(
    browser, base_url, status, jpeg, path, viewport, checks, text_path=None, custom=False,
    extra=None,
):
    """`extra` (recording route): route, rec_status (the faked /api/recording/status),
    actions (JavaScript run after load, in order), reduced_motion, qr (codes expected)."""
    extra = extra or {}
    errors = []
    page = browser.new_page(
        viewport=viewport,
        device_scale_factor=1,
        reduced_motion="reduce" if extra.get("reduced_motion") else "no-preference",
    )
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    if extra.get("route"):
        page.add_init_script(
            f"try {{ localStorage.setItem({json.dumps(ROUTE_KEY)}, "
            f"{json.dumps(extra['route'])}); }} catch {{}}"
        )
    if extra.get("rec_status") is not None:
        rec_body = json.dumps(extra["rec_status"])
        page.route(
            "**/api/recording/status",
            lambda route: route.fulfill(status=200, content_type="application/json", body=rec_body),
        )
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
    if extra:
        page.wait_for_function("() => recStatus !== null")
        for action in extra.get("actions", []):
            page.evaluate(action)
            page.wait_for_timeout(300)
        # The QR codes and the positions load after the route is shown.
        page.wait_for_function(
            "() => recGuide !== null && (recLabs !== null || !recordingConfig())"
        )
        page.evaluate("() => renderRecording()")
        page.wait_for_timeout(200)
    problems = page.evaluate(LAYOUT_CHECK, checks) + [f"console: {e}" for e in errors]
    for selector, want in (extra.get("qr") or {}).items():
        got = decode_qr(page.locator(selector).screenshot())
        if got != want:
            problems.append(f"QR {selector} on screen reads {got!r}, expected {want!r}")
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
    rec_camera = recording_camera(config.board)
    print(f"rendering {args.views} synthetic 4:3 views for the recording route…", flush=True)
    # Seed 34 reaches every coverage target with 40 views, so the solved shot passes.
    rec_views = synthetic_views(rec_camera, args.views, np.random.default_rng(34))
    with tempfile.TemporaryDirectory() as tmp:
        print("solving them with the app's solver…", flush=True)
        rec_states = build_recording_states(config, Path(tmp), rec_views)
    for result in rec_states["solved"]["results"]:
        state = "ok" if result.get("ok") else f"FAILED: {result.get('error', '')[:120]}"
        print(f"  {result['model']}: rms {result.get('rms')} ({state})")
    calibration_qr = labs_command(config.recording, calibration=True)
    dataset_qr = labs_command(config.recording, calibration=False)

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

    def rec_row(setup, settings, record, drop):
        names = ("stepSetup", "stepSettings", "stepRecord", "stepDrop")
        return dict(zip(names, (setup, settings, record, drop), strict=True))

    rec_common = [".steps", ".setup", "#recBench"]

    def rec_checks(expect, primary, steps, texts=(), absent=(), not_texts=(), drawn=(), within=None,
                   setup=False, fold=()):
        result = checks(expect, primary, texts=texts, absent=absent, steps=steps,
                        not_texts=not_texts, setup=setup)
        result["expect"] = rec_common + list(expect)
        result["drawn"] = list(drawn)
        result["within"] = within or {}
        result["fold"] = list(fold)
        return result

    route_texts = [
        "How will you calibrate?",
        "Calibrates the webcam stream only. Not valid for footage recorded on the camera.",
        "For footage recorded on the camera, like UMI.",
        "Check the code printed on the lens mod: ADWAL-002 = Max Lens Mod 2.0, "
        "AEWAL-001 = Ultra Wide Lens Mod.",
    ]
    record_text = (
        "Record 60–90 s. Move slowly and hold each position for about a second. "
        "Push the board right to the edges of the frame."
    )
    banner = (
        "This clip was not recorded with the preset's settings: Frame rate is 29.97 fps, "
        "expected 60 fps; HyperSmooth is On (HS Boost), expected Off."
    )
    confirm = "() => document.getElementById('recSettingsBtn').click()"
    recorded = "() => document.getElementById('recRecordedBtn').click()"
    rec = {"route": "recording", "rec_status": rec_states["idle"]}

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
                texts=["Choose From a recording in step 1 instead"],
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
        # ---- From a recording ----
        (
            # A first visit: the live route, with the choice of route in step 1. Picking
            # From a recording switches the step bar and is remembered.
            "rec-route-choice",
            None,
            b"",
            review / "rec-route-choice.png",
            desktop,
            rec_checks(
                ["#routeChoice", "#recSettingsPanel"],
                "#recSettingsBtn",
                rec_row("done", "current", "pending", "pending"),
                texts=route_texts,
                absent=["#previewBtn", ".bench", "#stepConnect"],
            ),
            False,
            {
                "rec_status": rec_states["idle"],
                "actions": [
                    "() => document.querySelector('input[value=recording]').click()",
                    f"() => {{ if (localStorage.getItem({json.dumps(ROUTE_KEY)}) !== 'recording')"
                    " throw new Error('route not remembered'); }",
                ],
            },
        ),
        (
            "rec-settings",
            None,
            b"",
            review / "rec-settings.png",
            desktop,
            rec_checks(
                ["#recSettingsPanel", "#qrCalibration", "#qrLater", "#labsChecklist tr",
                 "#shutterWarning", "#labsUnverified"],
                "#recSettingsBtn",
                rec_row("done", "current", "pending", "pending"),
                texts=[
                    "Scan before the calibration clip",
                    "Show QR code 2 (for after the calibration clip)",
                    "Keep QR code 2 folded away while you scan",
                    "Check the camera screen after scanning",
                    "Switch the shutter back after the calibration clip.",
                    calibration_qr,
                    "4K, aspect ratio 4:3",
                    "The QR code should select the Ultra Wide lens.",
                    "If it shows another lens, set Lens to Ultra Wide by hand.",
                    "set Shutter to 1/480 in Protune by hand.",
                ],
                # QR code 2 is folded away: the camera sees the whole screen.
                not_texts=["oX3", dataset_qr, "Scan after, to go back to dataset settings"],
                absent=["#resultsPanel", "#clipCheck", "#recShowSettingsBtn", "#labsAlternatives",
                        "#qrDataset"],
                within={"#labsBody thead": ["NOTES"]},  # the header is set in capitals
            ),
            False,
            {**rec, "qr": {"#qrCalibration": calibration_qr}},
        ),
        (
            # Review only: QR code 2 unfolded, for after the calibration clip.
            "rec-settings-qr2",
            None,
            b"",
            review / "rec-settings-qr2.png",
            desktop,
            rec_checks(
                ["#qrCalibration", "#qrDataset"],
                "#recSettingsBtn",
                rec_row("done", "current", "pending", "pending"),
                texts=["Scan after, to go back to dataset settings", dataset_qr,
                       "Fold it away again before you scan QR code 1"],
            ),
            False,
            {**rec, "actions": ["() => { document.getElementById('qrLater').open = true; }"],
             "qr": {"#qrCalibration": calibration_qr, "#qrDataset": dataset_qr}},
        ),
        (
            # Review only: the Ultra Wide Lens Mod setup, with the codes still to try
            # kept in a closed disclosure.
            "rec-settings-uwlm",
            None,
            b"",
            review / "rec-settings-uwlm.png",
            desktop,
            rec_checks(
                ["#labsUnverified", "#labsAlternatives"],
                "#recSettingsBtn",
                rec_row("done", "current", "pending", "pending"),
                texts=["Check the camera screen shows the Ultra Wide Lens Mod.",
                       "The QR code should turn on lens-mod detection;"],
                not_texts=["oX3fX may be"],
            ),
            False,
            {
                **rec,
                "actions": [
                    f"() => {{ presetSelect.value = {json.dumps(UWLM_PRESET)};"
                    " presetSelect.dispatchEvent(new Event('change')); }",
                ],
            },
        ),
        (
            # Step 2 confirmed, then another lens-mod setup picked before any clip: its
            # QR code differs, so step 2 is the current step again.
            "rec-setup-change",
            None,
            b"",
            review / "rec-setup-change.png",
            desktop,
            rec_checks(
                ["#recSettingsPanel", "#qrCalibration"],
                "#recSettingsBtn",
                rec_row("done", "current", "pending", "pending"),
                within={"#qrCalibrationCode": ["oX10"]},
                absent=["#recRecordPanel"],
            ),
            False,
            {
                **rec,
                "actions": [
                    confirm,
                    f"() => {{ presetSelect.value = {json.dumps(UWLM_PRESET)};"
                    " presetSelect.dispatchEvent(new Event('change')); }",
                ],
            },
        ),
        (
            "rec-settings-mobile",
            None,
            b"",
            review / "rec-settings-mobile.png",
            {"width": 390, "height": 844},
            rec_checks(["#qrCalibration", "#labsChecklist tr"], "#recSettingsBtn",
                       rec_row("done", "current", "pending", "pending")),
            False,
            rec,
        ),
        (
            # Paused at a known moment (position 5, held) so the picture is the same
            # every run.
            "rec-record",
            None,
            b"",
            review / "rec-record.png",
            desktop,
            rec_checks(
                ["#recRecordPanel", "#recAnim", "#animToggle", "[data-anim-moving]"],
                "#recRecordedBtn",
                rec_row("done", "done", "current", "pending"),
                texts=[record_text, "Play", "Orange dot: where the board goes now"],
                not_texts=["Ringed: hold the board tilted"],
                drawn=["#recAnim"],
                within={"#animCaption": ["position 5/23 · far · top-right corner"]},
                absent=["#recSettingsBtn"],
            ),
            False,
            {**rec, "actions": [confirm, "() => seekAnimation(4, 0.9)"]},
        ),
        (
            # Review only: a tilted position, drawn as a trapezoid, mid-move.
            "rec-record-tilt",
            None,
            b"",
            review / "rec-record-tilt.png",
            desktop,
            rec_checks(
                ["#recAnim"], "#recRecordedBtn", rec_row("done", "done", "current", "pending"),
                drawn=["#recAnim"],
                within={
                    "#animCaption": ["position 21/23 · tilted · upper right"],
                    "#recHint": ["finish steps 2 and 3 first"],
                },
            ),
            False,
            {**rec, "actions": [
                confirm, "() => seekAnimation(20, 0.35)",
                "() => recDropAnywhere([{name: 'GX010042.MP4'}])",
            ]},
        ),
        (
            # prefers-reduced-motion: a still, numbered map and the list in order.
            "rec-record-reduced",
            None,
            b"",
            review / "rec-record-reduced.png",
            desktop,
            rec_checks(
                ["#recAnim", "#positionsOl li"],
                "#recRecordedBtn",
                rec_row("done", "done", "current", "pending"),
                texts=["Hold the board at each numbered position in turn, 1 to 23."],
                drawn=["#recAnim"],
                absent=["#animToggle", "[data-anim-moving]"],
                within={"#animLegend": ["Ringed: hold the board tilted"]},
            ),
            False,
            {**rec, "reduced_motion": True, "actions": [
                confirm,
                # Closed by the operator, it stays closed through the next render.
                "() => { positionsList.open = false; renderRecording(); "
                "if (positionsList.open) throw new Error('positions list reopened'); "
                "positionsList.open = true; }",
            ]},
        ),
        (
            "rec-drop",
            None,
            b"",
            review / "rec-drop.png",
            desktop,
            rec_checks(
                ["#recDropPanel", "#dropZone", "#recShowSettingsBtn", "#recShowRecordBtn"],
                "#recChooseBtn",
                rec_row("done", "done", "done", "current"),
                texts=["Choose clip…", "Drop the clip from the camera's card here"],
                absent=["#recNextBtn", "#recStats", "#resultsPanel"],
            ),
            False,
            {**rec, "actions": [confirm, recorded]},
        ),
        (
            "rec-uploading",
            None,
            b"",
            review / "rec-uploading.png",
            desktop,
            rec_checks(
                ["#recProgress"],
                None,
                rec_row("done", "done", "done", "current"),
                texts=[f"Copying {CLIP} into the run folder", "43 %", "603 MB of 1.40 GB copied"],
                absent=["#resultsPanel", "#clipCheck"],
                within={"#recCamera": ["for now. The run is named after the camera's serial "
                                       "number once the clip is read."]},
            ),
            False,
            {"route": "recording", "rec_status": rec_states["uploading"]},
        ),
        (
            "rec-analysing",
            None,
            b"",
            review / "rec-analysing.png",
            desktop,
            rec_checks(
                ["#recProgress"],
                None,
                rec_row("done", "done", "done", "current"),
                texts=["Picking views from the clip", "37 %",
                       "Working on the clip. Wait for the result."],
                not_texts=["Add another clip"],
                within={"#recChooseBtn": ["Choose clip…"]},
                absent=["#resultsPanel", "#clipCheck", "#recMapWrap", "#recReminder"],
            ),
            False,
            {"route": "recording", "rec_status": rec_states["analysing"]},
        ),
        (
            "rec-mismatch-pass",
            None,
            b"",
            review / "rec-mismatch-pass.png",
            desktop,
            rec_checks(
                ["#clipBanner", "#clipUnknown", "#resultsPanel", "#recMapWrap", "#recStats",
                 "#recNextBtn"],
                "#recRedoBtn",
                rec_row("done", "done", "done", "current"),
                fold=["#clipBanner"],
                not_texts=["Calibration solved.", "Record your data in exactly this mode."],
                absent=["#recReminder"],
                texts=[
                    banner,
                    "Check every setting on the camera again, and record the clip again if any "
                    "differ. The calibration below is only valid for footage recorded exactly "
                    "like this clip.",
                    "PASS",
                    "The file cannot confirm these settings",
                    "Named this camera gopro13_1234 from its serial number",
                ],
                within={
                    "#verdictText": [
                        banner, "Calibration passed, but it is only valid for footage "
                        "recorded exactly like this clip.",
                    ],
                    "#recPrompt": ["click Start this camera again", "Do not add the new clip"],
                    "#statusLine": ["Calibration done, but the clip was not recorded"],
                    "#recDropDesc": ["see the red box"],
                    "#dropZoneTitle": ["Add a clip to this run (not a re-recorded one)"],
                    "#resultGrid": ["lens Ultra Wide and Max Lens Mod 2.0 (ADWAL-002) as set "
                                    "on the camera (not in the file)"],
                },
                drawn=["#recMap"],
            ),
            False,
            {"route": "recording", "rec_status": rec_states["mismatch"]},
        ),
        (
            # A clip dropped onto the flagged run: the page asks first, and the browser's
            # confirm is dismissed here (Playwright's default), so nothing is copied.
            "rec-mismatch-drop-cancel",
            None,
            b"",
            review / "rec-mismatch-drop-cancel.png",
            desktop,
            rec_checks(
                ["#clipBanner", "#recHint"],
                "#recRedoBtn",
                rec_row("done", "done", "done", "current"),
                within={"#recHint": ["Not added: GX010044.MP4.",
                                     "click Start this camera again, then drop it in step 4"]},
                absent=["#recProgress"],
            ),
            False,
            {"route": "recording", "rec_status": rec_states["mismatch"], "actions": [
                "() => recDropAnywhere([{name: 'GX010044.MP4'}])",
                "() => { if (recUpload || recQueue.length || recRunning)"
                " throw new Error('the clip was queued'); }",
            ]},
        ),
        (
            # The clip check's problem stands and a later clip was refused: the prompt
            # names the same action as the amber button.
            "rec-mismatch-refused",
            None,
            b"",
            review / "rec-mismatch-refused.png",
            desktop,
            rec_checks(
                ["#clipBanner", "#recAlert"],
                "#recRedoBtn",
                rec_row("done", "done", "done", "current"),
                within={"#recPrompt": ["The last clip was not used either",
                                       "click Start this camera again"]},
            ),
            False,
            {"route": "recording", "rec_status": rec_states["mismatch_refused"]},
        ),
        (
            # Start this camera again after the mismatch, then steps 2 and 3 again: the
            # fresh run's drop step shows nothing of the last run (no banner, no result).
            "rec-redo-fresh-run",
            None,
            b"",
            review / "rec-redo-fresh-run.png",
            desktop,
            rec_checks(
                ["#dropZone"],
                "#recChooseBtn",
                rec_row("done", "done", "done", "current"),
                texts=["Choose clip…", "Drop the clip from the camera's card here"],
                absent=["#clipBanner", "#clipCheck", "#resultsPanel", "#recAlert", "#recStats",
                        "#recRedoBtn", "#recNextBtn"],
                not_texts=["This clip was not recorded with the preset's settings"],
            ),
            False,
            {"route": "recording", "rec_status": rec_states["mismatch"], "actions": [
                "() => document.getElementById('recRedoBtn').click()",
                "() => { if (recStatus.run_id) throw new Error('the run did not end'); }",
                confirm,
                recorded,
            ]},
        ),
        (
            "rec-mismatch-mobile",
            None,
            b"",
            review / "rec-mismatch-mobile.png",
            {"width": 390, "height": 844},
            rec_checks(["#clipBanner", "#resultsPanel"], "#recRedoBtn",
                       rec_row("done", "done", "done", "current"), texts=[banner]),
            False,
            {"route": "recording", "rec_status": rec_states["mismatch"]},
        ),
        (
            "rec-retake",
            None,
            b"",
            review / "rec-retake.png",
            desktop,
            rec_checks(
                ["#recMissing", "#recMissingList li", "#recNextBtn"],
                "#recChooseBtn",
                rec_row("done", "done", "done", "current"),
                texts=["Add another clip", "Missing positions",
                       "Not enough views to solve yet: 9 views kept, 25 needed.",
                       "Retake: record another clip that covers the"],
                absent=["#clipBanner", "#resultsPanel", "#recRedoBtn", "#recAlert",
                        "#recReminder"],
                not_texts=["Choose clip…", "Solve again", "covers the 0 missing"],
                within={
                    "#recStats": ["9", "need 25"],
                    "#recPrompt": ["Most frames were left out because the board was moving"],
                },
            ),
            False,
            {"route": "recording", "rec_status": rec_states["retake"]},
        ),
        (
            # That retake being solved: the old too-few-views line and the old refusal
            # are stale, so the status line and the progress bar agree.
            "rec-retake-solving",
            None,
            b"",
            review / "rec-retake-solving.png",
            desktop,
            rec_checks(
                ["#recProgress"],
                None,
                rec_row("done", "done", "done", "current"),
                within={"#statusLine": ["Solving… this can take a minute."],
                        "#recProgressLabel": ["Solving"]},
                absent=["#recAlert", "#clipBanner", "#resultsPanel", "#recMissing"],
                not_texts=["Not enough views to solve yet", "the last clip was not used"],
            ),
            False,
            {"route": "recording", "rec_status": rec_states["retake_solving"]},
        ),
        (
            # Every position covered, but too few views to solve: the prompt asks for
            # more still positions, not for a list of missing ones.
            "rec-needs-more-covered",
            None,
            b"",
            review / "rec-needs-more-covered.png",
            desktop,
            rec_checks(
                ["#recStats", "#recMapWrap"],
                "#recChooseBtn",
                rec_row("done", "done", "done", "current"),
                texts=["Not enough views to solve yet: 23 views kept, 25 needed."],
                absent=["#recMissing", "#clipBanner", "#recRedoBtn", "#resultsPanel",
                        "#recAlert", "#recReminder"],
                not_texts=["0 missing", "missing positions listed"],
                within={
                    "#recPrompt": ["holds the board still at more positions"],
                    "#recStats": ["23 / 23"],
                },
            ),
            False,
            {"route": "recording", "rec_status": rec_states["covered"]},
        ),
        (
            "rec-refused",
            None,
            b"",
            review / "rec-refused.png",
            desktop,
            rec_checks(
                ["#recAlert", "#resultsPanel"],
                "#recNextBtn",
                rec_row("done", "done", "done", "current"),
                texts=[
                    "GX010043.MP4 is 1920x1080 but this run's first clip was 4000x3000",
                    "This clip is not in the camera setup's mode (4000x3000)",
                    "Problem: the last clip was not used.",
                    "The calibration below still passes",
                ],
                # A clip in the wrong mode gets no run of its own.
                absent=["#recMissing", "#recRedoBtn"],
            ),
            False,
            {"route": "recording", "rec_status": rec_states["refused"]},
        ),
        (
            "rec-solved",
            None,
            b"",
            review / "rec-solved.png",
            desktop,
            rec_checks(
                ["#resultsPanel", "#clipUnknown", "#recCamera", ".figure", "#recReminder",
                 "#qrDatasetSmall"],
                "#recNextBtn",
                rec_row("done", "done", "done", "done"),
                not_texts=["Calibration solved."],
                within={"#recPrompt": ["scan QR code 2 below"]},
                texts=[
                    "PASS",
                    "Calibration passed. The files are listed below. Click Next camera",
                    "Named this camera gopro13_1234 from its serial number",
                    "Written for 960x720 frames",
                    "Blue: not covered (not needed, the result passed)",
                    "as set on the camera (not in the file), 4000x3000",
                ],
                absent=["#clipBanner", "#recMissing", "#recRedoBtn"],
            ),
            False,
            {"route": "recording", "rec_status": rec_states["solved"],
             "qr": {"#qrDatasetSmall": dataset_qr}},
        ),
        (
            # A camera setup with no recording section: step 1 is next again.
            "rec-live-only-preset",
            None,
            b"",
            review / "rec-live-only-preset.png",
            desktop,
            rec_checks(
                ["#labsError"],
                None,
                rec_row("current", "pending", "pending", "pending"),
                texts=["This camera setup has no settings for recording on the camera."],
                setup=True,
            ),
            False,
            {
                **rec,
                "actions": [
                    f"() => {{ presetSelect.value = {json.dumps(LIVE_ONLY_PRESET)};"
                    " presetSelect.dispatchEvent(new Event('change')); }",
                ],
            },
        ),
    ]
    failures = 0
    launch = {"executable_path": shutil.which("chromium")} if shutil.which("chromium") else {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        for name, status, frame, path, viewport, shot_checks, custom, *rest in shots:
            extra = rest[0] if rest else None
            shot_checks.setdefault("drawn", [])
            shot_checks.setdefault("within", {})
            shot_checks.setdefault("fold", [])
            jpeg = (
                cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
                if len(frame)
                else b""
            )
            text_path = args.review_dir / f"{name}.txt"
            problems = shoot(
                browser, base_url, status, jpeg, path, viewport, shot_checks, text_path, custom,
                extra,
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
