"""Check a clip recorded on the camera against the preset's recording settings.

The check never blocks a solve: it lists what it read from the file and says which
settings differ, so the UI can warn loudly. ffprobe gives the video stream (size,
frame rate, codec, length); the GoPro metadata comes from ``gpmf.read_clip_metadata``.

A field is ``mismatch`` only when both the expected value and the value in the file
are documented; when the file does not say (or GoPro does not document what it
writes, as for the lens), the field is ``unknown`` with advice on what to check on
the camera.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from .gpmf import read_clip_metadata
from .models import RecordingConfig

# 59.94 fps (60000/1001) is what "60" means on a GoPro: accept 0.2 % either way.
FPS_TOLERANCE = 0.002
# A locked shutter reads within a few percent of its value; auto exposure wanders.
SHUTTER_TOLERANCE = 0.15
# OpenICC's GoPro advice is 20-30 s of slow motion; below this a clip is short.
SHORT_CLIP_S = 20.0


def _rate(value: Any) -> float | None:
    try:
        rate = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return float(rate) if rate > 0 else None


def _number(value: Any) -> float | None:
    """A finite float, or None ("N/A", missing, nan): the status must stay valid JSON."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int(value: Any) -> int:
    number = _number(value)
    return 0 if number is None else int(number)


def ffmpeg_error_line(stderr: str, path: Path) -> str | None:
    """The last line ffmpeg or ffprobe printed, without the file's folder in it.

    They start an error with the input's full path ("/runs/.../clip.mp4: Invalid data
    found when processing input"); the status only needs what went wrong.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return None
    line = lines[-1]
    for prefix in (f"{path}:", f"{path.name}:"):
        if line.startswith(prefix):
            line = line[len(prefix) :].strip()
    return line.replace(str(path), path.name) or None


def probe_video(path: Path) -> dict[str, Any]:
    """Width, height, frame rates, codec, duration and rotation of the first video stream.

    Returns ``{"error": ...}`` when ffprobe is missing or cannot read the file.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return {"error": "ffprobe is not installed (it comes with ffmpeg)"}
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error", "-print_format", "json",
                "-show_streams", "-show_format", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"ffprobe failed: {exc}"}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if proc.returncode != 0 or video is None:
        return {"error": ffmpeg_error_line(proc.stderr, path) or "no video stream in this file"}
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = _int(side["rotation"])
    if "rotate" in (video.get("tags") or {}):
        rotation = _int(video["tags"]["rotate"])
    duration = _number(video.get("duration")) or _number(
        (data.get("format") or {}).get("duration")
    )
    return {
        "width": _int(video.get("width")),
        "height": _int(video.get("height")),
        "avg_frame_rate": _rate(video.get("avg_frame_rate")),
        "r_frame_rate": _rate(video.get("r_frame_rate")),
        "codec": video.get("codec_name"),
        "duration_s": duration,
        "rotation": rotation,
    }


def _row(field, label, expected, found, status, advice="") -> dict[str, Any]:
    return {
        "field": field,
        "label": label,
        "expected": expected,
        "found": found,
        "status": status,
        "advice": advice,
    }


# GoPro's names for the recording sizes this route supports.
RESOLUTION_NAMES = {(4000, 3000): "4K with the aspect ratio 4:3"}


def _size_text(width: int, height: int) -> str:
    name = RESOLUTION_NAMES.get((width, height))
    return f"{name} ({width}x{height})" if name else f"{width}x{height}"


def _fps_text(fps: float) -> str:
    return f"{fps:.2f}".rstrip("0").rstrip(".") + " fps"


def compare(
    probe: dict[str, Any],
    meta: dict[str, Any],
    rec: RecordingConfig,
    run_serial: str | None = None,
) -> list[dict]:
    """The clip-check rows: settings first, then facts about the file.

    ``run_serial`` is the serial of the run's earlier clips: a clip from another camera
    is a mismatch.
    """
    rows: list[dict[str, Any]] = []
    probed = "error" not in probe
    no_gpmf = (
        "This file has no GoPro metadata. Use the original file copied from the camera's "
        "card, not an exported or re-encoded copy."
    )

    model = meta.get("model")
    if model is None:
        rows.append(_row("model", "Camera", rec.camera_model, None, "unknown", no_gpmf))
    elif "HERO13" in model.upper().replace(" ", ""):
        rows.append(_row("model", "Camera", rec.camera_model, model, "ok"))
    else:
        rows.append(
            _row(
                "model", "Camera", rec.camera_model, model, "mismatch",
                f"These settings are for a {rec.camera_model}. Record the clip with that camera.",
            )
        )

    expected_size = f"{rec.width}x{rec.height}"
    if not probed:
        rows.append(
            _row("resolution", "Resolution", expected_size, None, "unknown", probe["error"])
        )
    else:
        found = f"{probe['width']}x{probe['height']}"
        ok = (probe["width"], probe["height"]) == (rec.width, rec.height)
        rows.append(
            _row(
                "resolution", "Resolution", expected_size, found, "ok" if ok else "mismatch",
                "" if ok else f"Set the resolution to {_size_text(rec.width, rec.height)}, "
                "then record again.",
            )
        )

    expected_fps = _fps_text(rec.fps)
    fps = (probe.get("avg_frame_rate") or probe.get("r_frame_rate")) if probed else None
    if fps is None:
        fps = meta.get("vfps")
    if fps is None:
        rows.append(
            _row("fps", "Frame rate", expected_fps, None, "unknown",
                 "Check the frame rate on the camera screen.")
        )
    else:
        ok = abs(fps - rec.fps) <= FPS_TOLERANCE * rec.fps
        rows.append(
            _row(
                "fps", "Frame rate", expected_fps, _fps_text(fps), "ok" if ok else "mismatch",
                "" if ok else f"Set the frame rate to {rec.fps:g}, then record again.",
            )
        )

    eise, eisa = meta.get("eise"), meta.get("eisa")
    if eise is None and eisa is None:
        rows.append(
            _row("hypersmooth", "HyperSmooth", "Off", None, "unknown",
                 "Check HyperSmooth is Off on the camera screen.")
        )
    else:
        on = (eise or "").upper() == "Y" or (eisa is not None and eisa.upper() != "N/A")
        found = f"On ({eisa})" if on and eisa and eisa.upper() != "N/A" else ("On" if on else "Off")
        rows.append(
            _row(
                "hypersmooth", "HyperSmooth", "Off", found, "mismatch" if on else "ok",
                "Turn HyperSmooth Off, then record again: stabilisation moves the image "
                "differently in every frame." if on else "",
            )
        )

    # GoPro documents VFOV as L, W, S, H; which letter a lens mod writes is not
    # documented, so the lens can never be confirmed from the file.
    vfov, zfov = meta.get("vfov"), meta.get("zfov")
    lens_found = None
    if vfov is not None or zfov is not None:
        parts = []
        if vfov is not None:
            parts.append(f"lens code {vfov}")
        if zfov is not None:
            parts.append(f"{zfov:.1f}° diagonal")
        lens_found = ", ".join(parts)
    rows.append(
        _row(
            "lens", "Lens", rec.lens, lens_found, "unknown",
            f"Check the lens on the camera screen: it must say {rec.lens}, with the "
            f"{rec.lens_mod_name} set. The file cannot confirm the lens.",
        )
    )

    shutter = meta.get("shutter_s")
    expected_shutter = f"{rec.calibration_shutter} s"
    if shutter is None or shutter <= 0:
        rows.append(
            _row("shutter", "Shutter", expected_shutter, None, "unknown",
                 f"Check the shutter shows {rec.calibration_shutter} on the camera screen.")
        )
    else:
        ok = abs(shutter - rec.shutter_s) <= SHUTTER_TOLERANCE * rec.shutter_s
        rows.append(
            _row(
                "shutter", "Shutter", expected_shutter, f"1/{1 / shutter:.0f} s",
                "ok" if ok else "mismatch",
                "" if ok else "Scan the calibration QR code (or set the shutter to "
                f"{rec.calibration_shutter} in Protune), then record again.",
            )
        )

    # GoPro originals are expected to carry no rotation flag (unverified); an exported
    # or re-muxed copy can. Frames are read as stored (ffmpeg -noautorotate), so the
    # calibration is for the stored pixels, not the turned picture a player shows.
    rotation = probe.get("rotation") if probed else None
    if rotation is None:
        rows.append(_row("rotation", "Rotation flag", "none", None, "unknown"))
    elif rotation % 360 == 0:
        rows.append(_row("rotation", "Rotation flag", "none", "none", "ok"))
    else:
        rows.append(
            _row(
                "rotation", "Rotation flag", "none", f"turn by {abs(rotation) % 360}°",
                "mismatch",
                "This file tells video players to turn the picture. The calibration uses "
                "the picture as stored, which may not be what the dataset tools read. Use "
                "the original file from the camera's card, and lock the camera's "
                "orientation before recording.",
            )
        )

    # Facts about the file: never a mismatch.
    codec = probe.get("codec") if probed else None
    rows.append(_row("codec", "Video codec", None, codec, "ok" if codec else "unknown"))
    duration = probe.get("duration_s") if probed else None
    rows.append(
        _row(
            "duration", "Length", None,
            None if duration is None else f"{duration:.0f} s",
            "ok" if duration is not None else "unknown",
            "Short clip: record 60 to 90 s so every board position is covered."
            if duration is not None and duration < SHORT_CLIP_S else "",
        )
    )
    rows.append(
        _row(
            "imu", "Motion sensor data", None,
            "present" if meta.get("has_imu") else None,
            "ok" if meta.get("has_imu") else "unknown",
        )
    )
    serial = meta.get("serial")
    if run_serial and serial and serial != run_serial:
        rows.append(
            _row(
                "serial", "Camera serial", run_serial, serial, "mismatch",
                f"This clip is from another camera (serial {serial}). It gets a "
                "calibration of its own.",
            )
        )
    else:
        rows.append(
            _row("serial", "Camera serial", run_serial, serial, "ok" if serial else "unknown")
        )
    return rows


def fallback_check(path: Path, rec: RecordingConfig, error: Exception) -> dict[str, Any]:
    """The clip check when reading the file's metadata failed: every setting unknown."""
    try:
        probe = probe_video(path)
    except Exception as exc:  # noqa: BLE001 - the check is advisory
        probe = {"error": f"ffprobe failed: {exc}"}
    meta = {"gpmf_found": False, "error": f"{type(error).__name__}: {error}"}
    rows = compare(probe, meta, rec)
    return {
        "probe": probe,
        "metadata": meta,
        "check": rows,
        "mismatch_count": sum(1 for row in rows if row["status"] == "mismatch"),
    }


def check_clip(
    path: Path, rec: RecordingConfig, run_serial: str | None = None
) -> dict[str, Any]:
    """Probe a clip and compare it with ``rec``. Never raises for a readable file."""
    probe = probe_video(path)
    meta = read_clip_metadata(path)
    rows = compare(probe, meta, rec, run_serial)
    return {
        "probe": probe,
        "metadata": meta,
        "check": rows,
        "mismatch_count": sum(1 for row in rows if row["status"] == "mismatch"),
    }
