from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

from . import presets
from .bridge import stop_gopro_video_bridge
from .capture import CaptureSession, default_runs_dir
from .gopro import gopro_options_for_ui
from .labs import labs_payload
from .models import AppConfig, SolveFramesRequest, StartRequest
from .recording import (
    NO_RECORDING_SETTINGS,
    MissingToolError,
    RecordingError,
    RecordingJob,
    recording_guide,
)
from .solver import solve_from_frames

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Startup: kill any orphan ffmpeg bridge left by a previous/crashed run so we
    # start from a clean slate (and so the new low-latency bridge is used, not a
    # stale one that "looks healthy").
    try:
        stop_gopro_video_bridge(_session.config)
    except Exception:  # noqa: BLE001 - never block startup on cleanup
        pass
    yield
    # Shutdown: end any open MJPEG stream so Ctrl-C does not hang, then release
    # the camera and exit GoPro webcam mode.
    _shutdown.set()
    try:
        with _session_lock:
            _session.close()
    except Exception:  # noqa: BLE001 - never block shutdown on cleanup
        pass


class _RevalidatedStaticFiles(StaticFiles):
    """Static files the browser must revalidate on every load (ETag makes that a
    cheap 304), so a UI update is picked up without hand-bumped ?v= query strings."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="GoPro ChArUco Calibrator", lifespan=_lifespan)
app.mount("/static", _RevalidatedStaticFiles(directory=STATIC_DIR), name="static")

_session_lock = threading.Lock()
_session = CaptureSession()
# The "From a recording" route: its own job, independent of the live session.
_recording = RecordingJob()
# Set only on server shutdown; ends the MJPEG generator. NOT the per-session stop
# (a per-session stop is handled by the browser closing the stream connection),
# so reopening a preview after Stop/Next Camera still streams.
_shutdown = threading.Event()

_STREAM_MAX_FPS = 30.0
_STREAM_MIN_INTERVAL = 1.0 / _STREAM_MAX_FPS


def set_default_config(config) -> None:
    """Seed the session's startup config (used by ``serve --config <preset>``)."""
    with _session_lock:
        _session.config = config


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/defaults")
def defaults():
    with _session_lock:
        config = _session.config
    return {
        "config": config.model_dump(),
        "runs_dir": str(default_runs_dir()),
        "gopro_options": gopro_options_for_ui(),
        "presets": presets.list_presets(),
        "aruco_dictionaries": [
            "DICT_4X4_50",
            "DICT_4X4_100",
            "DICT_4X4_250",
            "DICT_5X5_50",
            "DICT_5X5_100",
            "DICT_5X5_250",
            "DICT_6X6_250",
            "DICT_7X7_1000",
        ],
    }


@app.post("/api/session/start")
def start_session(request: StartRequest):
    """Compatibility shortcut: open preview and immediately start a new run."""
    with _session_lock:
        if request.runs_dir is not None:
            _session.runs_dir = request.runs_dir
        return _session.start_run(request.config)


@app.post("/api/session/preview")
def open_preview(request: StartRequest):
    with _session_lock:
        if request.runs_dir is not None:
            _session.runs_dir = request.runs_dir
        return _session.open_preview(request.config)


@app.post("/api/session/run")
def start_run(request: StartRequest):
    with _session_lock:
        if request.runs_dir is not None:
            _session.runs_dir = request.runs_dir
        return _session.start_run(request.config)


@app.post("/api/session/pause")
def pause_session():
    with _session_lock:
        return _session.pause()


@app.post("/api/session/resume")
def resume_session():
    with _session_lock:
        return _session.resume()


@app.post("/api/session/next-camera")
def next_camera(request: StartRequest):
    with _session_lock:
        if request.runs_dir is not None:
            _session.runs_dir = request.runs_dir
        return _session.next_camera(request.config)


@app.post("/api/session/stop")
def stop_session():
    with _session_lock:
        return _session.close()


@app.get("/api/session/status")
def session_status():
    with _session_lock:
        return _session.status()


@app.post("/api/session/capture")
def manual_capture():
    with _session_lock:
        return _session.request_capture()


@app.post("/api/session/solve")
def solve_session():
    with _session_lock:
        session = _session
    try:
        return session.solve()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/session/latest.jpg")
def latest_detection():
    with _session_lock:
        path = _session.latest_detection_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="No preview frame yet")
    return FileResponse(path, media_type="image/jpeg")


def _mjpeg_chunk(jpeg: bytes) -> bytes:
    return (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
    )


async def _mjpeg_generator(session: CaptureSession):
    """Push the latest detection JPEG as a multipart stream at up to ~30 fps.

    Runs entirely off the global ``_session_lock``; it only touches the session's
    frame condition (via a worker thread) so it never blocks other endpoints.
    """
    last_seq = -1
    last_emit = 0.0
    jpeg, last_seq = session.latest_jpeg()
    if jpeg is not None:
        last_emit = time.monotonic()
        yield _mjpeg_chunk(jpeg)
    try:
        while not _shutdown.is_set():
            jpeg, seq = await anyio.to_thread.run_sync(session.wait_for_jpeg, last_seq, 1.0)
            if jpeg is None or seq == last_seq:
                continue
            last_seq = seq
            dt = time.monotonic() - last_emit
            if dt < _STREAM_MIN_INTERVAL:
                await anyio.sleep(_STREAM_MIN_INTERVAL - dt)
            last_emit = time.monotonic()
            yield _mjpeg_chunk(jpeg)
    except GeneratorExit:
        return


@app.get("/api/session/stream.mjpg")
def stream_mjpg():
    with _session_lock:
        session = _session
    return StreamingResponse(
        _mjpeg_generator(session),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/presets")
def list_presets_endpoint():
    return {"presets": presets.list_presets()}


@app.get("/api/presets/{name}")
def get_preset_endpoint(name: str):
    try:
        title, config = presets.get_preset(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Preset not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"name": name, "title": title, "config": config.model_dump()}


@app.post("/api/presets/{name}")
def save_preset_endpoint(name: str, request: StartRequest):
    try:
        path = presets.save_preset(name, request.config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "name": name, "path": str(path)}


@app.get("/api/runs")
def list_runs(runs_dir: Path | None = None):
    root = runs_dir or default_runs_dir()
    if not root.exists():
        return {"runs": []}
    runs = []
    for path in sorted(root.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        summary_path = path / "caib_marker_board_calibration_summary.json"
        config_path = path / "config.json"
        runs.append(
            {
                "run_id": path.name,
                "path": str(path),
                "has_summary": summary_path.exists(),
                "has_config": config_path.exists(),
            }
        )
    return {"runs": runs}


@app.get("/api/runs/{run_id}/summary")
def run_summary(run_id: str, runs_dir: Path | None = None):
    root = runs_dir or default_runs_dir()
    summary_path = root / run_id / "caib_marker_board_calibration_summary.json"
    if not summary_path.exists():
        raise HTTPException(status_code=404, detail="Run summary not found")
    with summary_path.open(encoding="utf-8") as stream:
        return json.load(stream)


@app.post("/api/solve-frames")
def solve_frames(request: SolveFramesRequest):
    return solve_from_frames(
        frames_dir=request.frames_dir,
        output_dir=request.output_dir,
        camera=request.camera,
        board_config=request.board,
        solver=request.solver,
    )


# ---------------------------------------------------------------------------
# From a recording
# ---------------------------------------------------------------------------


class RecordingRequest(StartRequest):
    config: AppConfig | None = None


@app.post("/api/recording/start")
def recording_start(request: StartRequest):
    """Open a new recording-route run (runs/<camera>_<timestamp>/) for the next clip.

    400 when the preset has no recording section, 409 while a clip is being processed,
    503 when ffmpeg or ffprobe is not installed.
    """
    try:
        return _recording.start(request.config, request.runs_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MissingToolError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RecordingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/recording/new")
def recording_new(request: RecordingRequest | None = None):
    """End this camera's run and go back to idle for the next camera."""
    try:
        return _recording.new(None if request is None else request.config)
    except RecordingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/recording/status")
def recording_status():
    return _recording.status()


@app.put("/api/recording/clips")
async def recording_upload(request: Request, name: str):
    """Stream the request body into the run's clips/ folder, then process it.

    The body is the raw file (no multipart), so a 1.4 GB clip never sits in memory.
    A second upload to the same run adds a retake; a clip from another camera starts a
    new run for that camera. 409 while busy or before a run is started, 503 (nothing
    written) when ffmpeg or ffprobe is not installed.
    """
    try:
        total = int(request.headers.get("content-length") or 0) or None
    except ValueError:
        total = None
    try:
        path = _recording.begin_upload(name, total)
    except MissingToolError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RecordingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    received = 0
    try:
        async with await anyio.open_file(path, "wb") as stream:
            async for chunk in request.stream():
                if chunk:
                    await stream.write(chunk)
                    received += len(chunk)
                    _recording.upload_progress(received)
    except ClientDisconnect:
        # The browser went away (tab closed, network): nobody reads this response.
        message = "The upload stopped before the end. Drop the clip again."
        _recording.upload_failed(path, message)
        return JSONResponse(status_code=400, content={"detail": message})
    except OSError as exc:
        message = (
            "Could not save the clip on this computer"
            + (f" ({exc.strerror})" if exc.strerror else "")
            + ". Free some disk space and drop the clip again."
        )
        _recording.upload_failed(path, message)
        return JSONResponse(status_code=500, content={"detail": message})
    except BaseException:
        # Cancelled (server shutting down): clean up and let the cancellation through.
        _recording.upload_failed(path, "The upload stopped before the end. Drop the clip again.")
        raise
    if received == 0:
        _recording.upload_failed(path, "The file was empty.")
        raise HTTPException(status_code=400, detail="The file was empty.")
    try:
        return _recording.start_processing(path)
    except RecordingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/recording/guide")
def recording_guide_endpoint():
    """The current recording run's checkpoints, with plain words for the animation.

    Before a run is started this falls back to the live session's config; to draw the
    guide for the preset picked in the UI before starting, POST that config instead.
    """
    config = _recording.config if _recording.output_dir is not None else _session.config
    return recording_guide(config)


@app.post("/api/recording/guide")
def recording_guide_for_config(request: StartRequest):
    """The checkpoints for a given config (its coverage_targets), no run needed."""
    return recording_guide(request.config)


@app.post("/api/recording/labs")
def recording_labs(request: StartRequest):
    """The GoPro Labs QR codes (calibration clip, then dataset) and the settings checklist.

    400 when the preset has no recording section (it only supports the live route).
    """
    rec = request.config.recording
    if rec is None:
        raise HTTPException(status_code=400, detail=NO_RECORDING_SETTINGS)
    try:
        return labs_payload(rec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
