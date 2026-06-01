# GoPro ChArUco Calibrator

Local web app for calibrating GoPro 13 intrinsics from a V4L2 webcam feed and a
caib.io-style ChArUco marker board. It is ROS-free and is intended for repeated
field calibration of cameras that will later be used by a robotics stack.

The app preserves the SO-101 field calibration behavior:

- editable caib.io board specs instead of a fixed board
- live detection overlay from a USB/V4L2 camera
- auto-capture of stable, diverse board poses
- manual capture from the browser
- x/y/size/skew coverage feedback, including an x/y target band of `0.2..0.8`
- all-frame and selected-frame solves for `plumb_bob` and `rational_polynomial`
- ROS `camera_info` YAML output, JSON summaries, CSV diagnostics, frames, and overlays

## Install

Use Python 3.12. OpenCV wheels are not reliable on every newer Python release.

```bash
git clone https://github.com/GrgaPalcic/gopro-charuco-calibrator.git
cd gopro-charuco-calibrator
uv sync --extra dev
```

## Run The Web App

```bash
uv run gopro-charuco serve --host 0.0.0.0 --port 8765
```

Open:

```text
http://localhost:8765
```

For a remote robot or camera host, replace `localhost` with the host IP.

## Typical GoPro 13 Flow

1. Put the GoPro in the exact lens mode, resolution, and crop that runtime will use.
2. Start the V4L2/webcam feed and confirm the device path, for example `/dev/video42`.
3. Enter the camera name, device, resolution, FPS, and FourCC.
4. Enter the caib.io board specs:
   - columns and rows are checkerboard square counts
   - square and marker sizes are in millimeters in the UI
   - dictionary should match the print, for example `DICT_5X5_100`
   - start ID and marker count should match the generated caib.io board
5. Click `Start Capture`.
6. Move the board through the image while watching x/y/size/skew coverage.
7. Use `Capture` for important poses if auto-capture does not take them.
8. Click `Solve` after the capture target is reached or enough coverage is available.

Outputs are written under:

```text
runs/<camera_name>_<timestamp>/
```

Key files:

```text
config.json
frames/capture_###.jpg
overlays/capture_###.jpg
<camera_name>_plumb_bob.yaml
<camera_name>_rational_polynomial.yaml
<camera_name>_all_frames_plumb_bob.yaml
<camera_name>_all_frames_rational_polynomial.yaml
<camera_name>_*_frame_diagnostics.csv
caib_marker_board_calibration_summary.json
```

Pick the YAML with the better median/worst view error and stable-looking
distortion coefficients. For wide GoPro modes, `rational_polynomial` often wins,
but the diagnostics should decide.

## CLI Solver

You can solve an existing frame directory without starting the web app:

```bash
uv run gopro-charuco solve-frames \
  --frames-dir runs/gopro13_hyperview_20260601_120000/frames \
  --output-dir runs/gopro13_hyperview_20260601_120000 \
  --config runs/gopro13_hyperview_20260601_120000/config.json
```

Board settings can also be supplied directly:

```bash
uv run gopro-charuco solve-frames \
  --frames-dir ./frames \
  --output-dir ./calibration_out \
  --camera-name gopro13_hyperview \
  --cols 11 --rows 8 \
  --square-mm 34 --marker-mm 25 \
  --aruco-dict DICT_5X5_100 \
  --start-id 2 --marker-count 44 \
  --min-markers 8 --min-frames 25
```

## Notes

- Intrinsics are only valid for the exact camera mode used during capture.
- Matte prints and even lighting matter more than chasing a huge sample count.
- The x/y target rectangle is `0.2..0.8`; push the board into corners and edges.
- Size coverage needs both far/small and near/large board views.
- Skew coverage needs tilted board poses, not only translations.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
```

## License

Apache-2.0. See [LICENSE](LICENSE).
