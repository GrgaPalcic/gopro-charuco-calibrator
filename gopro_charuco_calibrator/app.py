from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .capture import CaptureSession, default_runs_dir
from .models import AppConfig, SolveFramesRequest, StartRequest
from .solver import solve_from_frames

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"

app = FastAPI(title="GoPro ChArUco Calibrator")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_session_lock = threading.Lock()
_session: CaptureSession | None = None


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/defaults")
def defaults():
    return {
        "config": AppConfig().model_dump(),
        "runs_dir": str(default_runs_dir()),
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
    global _session
    with _session_lock:
        if _session is not None:
            _session.stop()
        _session = CaptureSession(request.config, request.runs_dir)
        _session.start()
        return _session.status()


@app.post("/api/session/stop")
def stop_session():
    global _session
    with _session_lock:
        if _session is None:
            raise HTTPException(status_code=404, detail="No active capture session")
        _session.stop()
        status = _session.status()
        _session = None
        return status


@app.get("/api/session/status")
def session_status():
    with _session_lock:
        if _session is None:
            return {"state": "idle", "message": "no active session"}
        return _session.status()


@app.post("/api/session/capture")
def manual_capture():
    with _session_lock:
        if _session is None:
            raise HTTPException(status_code=404, detail="No active capture session")
        _session.request_capture()
        return _session.status()


@app.post("/api/session/solve")
def solve_session():
    with _session_lock:
        if _session is None:
            raise HTTPException(status_code=404, detail="No active capture session")
        session = _session
    return session.solve()


@app.get("/api/session/latest.jpg")
def latest_detection():
    with _session_lock:
        if _session is None:
            raise HTTPException(status_code=404, detail="No active capture session")
        path = _session.output_dir / "latest_detection.jpg"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No preview frame yet")
    return FileResponse(path, media_type="image/jpeg")


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
