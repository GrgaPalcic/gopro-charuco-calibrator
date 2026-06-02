# GoPro ChArUco Calibrator

Local web app for calibrating GoPro HERO11 and HERO13 intrinsics over USB webcam
mode, using a caib.io style ChArUco marker board. It does not require ROS and is
meant for repeated field calibration of cameras that a robotics stack will later
use. ffmpeg decodes the camera stream directly into the app, so there is no
v4l2loopback device to create.

Features:

- editable caib.io board specs instead of a fixed board
- low latency live preview with a guide route to trace
- direct ffmpeg decode of the GoPro USB stream (no v4l2loopback, no modprobe)
- auto capture of stable, diverse board poses, plus manual capture
- x/y/size/skew coverage feedback and a pass or retake verdict after solving
- camera presets you can load and save so the camera mode stays consistent
- plumb_bob and rational_polynomial solves with ROS camera_info YAML, JSON
  summaries, and CSV diagnostics

## Quick start

Prerequisites: [uv](https://docs.astral.sh/uv/) and `ffmpeg`. Install ffmpeg with
your package manager, for example `sudo apt install ffmpeg` or
`sudo pacman -S ffmpeg`.

```bash
git clone https://github.com/GrgaPalcic/gopro-charuco-calibrator.git
cd gopro-charuco-calibrator
uv run gopro-charuco serve
```

Then open http://localhost:8765 in a browser. The first `uv run` fetches Python
3.12 and every dependency automatically, so this is all you need. No `modprobe`
or `sudo` is required; the only command you might run once is a firewall allow
rule (see [Firewall](#firewall)) if the incoming UDP video is blocked.

To serve other machines, add `--host 0.0.0.0 --port 8765` and open
`http://<host-ip>:8765`. Run a single server process; the live session is kept
in memory.

Optional, start from a preset so the camera mode is identical every time:

```bash
uv run gopro-charuco serve --config gopro_charuco_calibrator/presets/gopro13_wide_1080p.yaml
```

## Typical flow

1. Connect the GoPro over USB.
2. Turn on `Use GoPro auto-setup`, then pick `Lens / FOV` and `Resolution` (these
   two define the calibrated camera model), or load a preset.
3. Click `Open Preview`. The stream status dot turns green when frames arrive.
4. Click `Start New Run`.
5. Move the board center along the guide route and match the highlighted box size
   and tilt. The box turns green when the live pose matches the target.
6. Use `Capture` for any pose the auto capture does not take.
7. Click `Solve` once the route is complete or coverage is sufficient. The
   results panel shows a pass or retake verdict and the recommended model.

For the next camera, click `Next Camera`, then `Start New Run`. The server stays
up between cameras.

## GoPro settings

The GoPro panel uses the camera HTTP API. With the URL blank, the app probes the
GoPro USB convention where the host is usually 172.x.x.52 and the camera answers
on 172.x.x.51, puts the camera into webcam mode, and decodes the UDP MPEG-TS
stream directly. Only the two settings that define the calibrated model are shown
by default:

- `Lens / FOV`: 0 is Wide, 2 is Narrow, 3 is SuperView, 4 is Linear
- `Resolution`: 4 is 480p, 7 is 720p, 12 is 1080p

Connection toggles and the recording settings sit in collapsed advanced sections
with safe defaults, so you normally do not touch them. To calibrate a plain USB
or V4L2 camera instead, turn off `Use GoPro auto-setup` and the app opens
`camera.device` directly with OpenCV.

## Firewall

If HTTP control works but no video frames arrive, a host firewall is usually
dropping the incoming UDP video while letting the TCP control through. The app
detects this and shows the exact command with a copy button, for example:

```bash
sudo ufw allow in on <gopro-usb-interface> from <gopro-ip> to any port 8554 proto udp
```

firewalld and iptables variants are shown too. Run it once on the host, then
click `Open Preview` again. This is the only step that may need `sudo`.

## Presets

Presets are small YAML files that lock a consistent camera mode and board so
calibration data is reproducible across runs and machines. Shipped presets live
in `gopro_charuco_calibrator/presets/`:

- `gopro13_wide_1080p.yaml` (default): HERO13, Wide lens, 1080p
- `gopro13_linear_1080p.yaml`: HERO13, Linear lens, 1080p
- `gopro11_wide_1080p.yaml`: HERO11, Wide lens, 1080p

In the web UI, pick a preset from the dropdown and click `Load`, or store the
current form with `Save as preset`. Saved presets are written to
`~/.config/gopro-charuco-calibrator/presets/` and override shipped presets of the
same name. The CLI accepts the same files via `serve --config <preset.yaml>`.

## Outputs

Each run is written under:

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

Pick the YAML with the better median and worst view error and stable distortion
coefficients. For wide GoPro modes `rational_polynomial` often wins, but the
diagnostics decide.

## CLI solver

You can solve an existing frame directory without the web app:

```bash
uv run gopro-charuco solve-frames \
  --frames-dir runs/gopro13_wide_1080p_20260601_120000/frames \
  --output-dir runs/gopro13_wide_1080p_20260601_120000 \
  --config runs/gopro13_wide_1080p_20260601_120000/config.json
```

Board settings can also be supplied directly:

```bash
uv run gopro-charuco solve-frames \
  --frames-dir ./frames \
  --output-dir ./calibration_out \
  --camera-name gopro13_wide_1080p \
  --cols 11 --rows 8 \
  --square-mm 34 --marker-mm 25 \
  --aruco-dict DICT_5X5_100 \
  --start-id 2 --marker-count 44 \
  --min-markers 8 --min-frames 25
```

## Notes

- Intrinsics are only valid for the exact camera mode used during capture.
- Matte prints and even lighting matter more than chasing a huge sample count.
- The x/y target rectangle is 0.2 to 0.8, so push the board into corners and edges.
- Size coverage needs both small (far) and large (near) board views.
- Skew coverage needs tilted board poses, not only translations.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
```

## License

Apache-2.0. See [LICENSE](LICENSE).
