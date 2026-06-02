from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn
import yaml

from .app import app, set_default_config
from .models import AppConfig, BoardConfig, CameraConfig, SolverConfig
from .solver import solve_from_frames


def _load_config(path: str | None) -> AppConfig:
    if not path:
        return AppConfig()
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if file_path.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Config {file_path.name} must be a mapping.")
    data.pop("title", None)  # presets may carry a human title; not part of the config
    if "config" in data and isinstance(data["config"], dict):
        data = data["config"]
    return AppConfig.model_validate(data)


def _add_board_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cols", type=int)
    parser.add_argument("--rows", type=int)
    parser.add_argument("--square-mm", type=float)
    parser.add_argument("--marker-mm", type=float)
    parser.add_argument("--aruco-dict")
    parser.add_argument("--start-id", type=int)
    parser.add_argument("--marker-count", type=int)


def _board_from_args(base: BoardConfig, args: argparse.Namespace) -> BoardConfig:
    data = base.model_dump()
    for arg_name, field_name in [
        ("cols", "cols"),
        ("rows", "rows"),
        ("aruco_dict", "aruco_dict"),
        ("start_id", "start_id"),
        ("marker_count", "marker_count"),
    ]:
        value = getattr(args, arg_name)
        if value is not None:
            data[field_name] = value
    if args.square_mm is not None:
        data["square_m"] = args.square_mm / 1000.0
    if args.marker_mm is not None:
        data["marker_m"] = args.marker_mm / 1000.0
    return BoardConfig.model_validate(data)


def cmd_serve(args: argparse.Namespace) -> int:
    if args.config:
        set_default_config(_load_config(args.config))
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_solve_frames(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    camera_data = config.camera.model_dump()
    solver_data = config.solver.model_dump()
    if args.camera_name:
        camera_data["camera_name"] = args.camera_name
    if args.min_frames is not None:
        solver_data["min_frames"] = args.min_frames
    if args.min_markers is not None:
        solver_data["min_markers"] = args.min_markers
    if args.max_view_error_px is not None:
        solver_data["max_view_error_px"] = args.max_view_error_px
    if args.no_auto_select:
        solver_data["auto_select"] = False

    summary = solve_from_frames(
        frames_dir=Path(args.frames_dir),
        output_dir=Path(args.output_dir),
        camera=CameraConfig.model_validate(camera_data),
        board_config=_board_from_args(config.board, args),
        solver=SolverConfig.model_validate(solver_data),
        coverage_targets=config.coverage_targets,
    )
    print(json.dumps({"results": summary["results"]}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GoPro caib.io ChArUco calibration app")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Start the local web app")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument(
        "--config",
        help="Optional preset (YAML or JSON) used as the startup default config",
    )
    serve.set_defaults(func=cmd_serve)

    solve = sub.add_parser("solve-frames", help="Solve intrinsics from an existing frame folder")
    solve.add_argument("--frames-dir", required=True)
    solve.add_argument("--output-dir", required=True)
    solve.add_argument("--config", help="Optional config.json from a capture run")
    solve.add_argument("--camera-name")
    solve.add_argument("--min-frames", type=int)
    solve.add_argument("--min-markers", type=int)
    solve.add_argument("--max-view-error-px", type=float)
    solve.add_argument("--no-auto-select", action="store_true")
    _add_board_args(solve)
    solve.set_defaults(func=cmd_solve_frames)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
