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
- plumb_bob, rational_polynomial, fisheye (Kannala-Brandt), and double_sphere
  (ultra-wide, via the OpenICC backend) solves, with ROS camera_info YAML, JSON
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
3.12 and every dependency automatically, so this is all you need for the
`plumb_bob`, `rational_polynomial`, and `fisheye` solves. No `modprobe` or `sudo`
is required; the only command you might run once is a firewall allow rule (see
[Firewall](#firewall)) if the incoming UDP video is blocked.

The one extra step is the `double_sphere` model (only used by the UMI gripper
preset): it needs the OpenICC Docker image built once, see
[Double Sphere](#double-sphere-openicc-backend). Without it, only that single
model fails (a red row in the results); every other model still solves, so a
quick Wide/fisheye calibration needs nothing beyond `serve`.

To serve other machines, add `--host 0.0.0.0 --port 8765` and open
`http://<host-ip>:8765`. Run a single server process; the live session is kept
in memory.

Optional, start from a preset so the camera mode is identical every time:

```bash
uv run gopro-charuco serve --config gopro_charuco_calibrator/presets/gopro13_wide_1080p.yaml
```

## HERO13 + Max Lens Mod 2.0 (UMI gripper)

Use this when the dataset was recorded from the HERO13 USB webcam stream with the
Max Lens Mod 2.0 fitted. Calibrate the camera exactly as it records: mod on,
webcam Wide, 1080p.

1. One-time setup on each machine: install Docker, make sure your user can run
   `docker` without sudo (member of the `docker` group), then build the OpenICC
   backend (about 10 minutes, no GPU):

   ```bash
   uv run gopro-charuco setup-openicc
   ```

2. Start the app with the gripper preset:

   ```bash
   uv run gopro-charuco serve --config gopro_charuco_calibrator/presets/gopro13_umi_gripper_fisheye_1080p.yaml
   ```

3. Connect the camera over USB with the mod fitted and click `Open Preview`.
   Check two things before capturing:
   - the preview is a **round image with black corners**. An image that fills
     the whole frame means a SuperView/HyperView stretch, which cannot be
     calibrated;
   - the status line shows `camera reports: webcam digital lens Wide (0),
     max lens mod Max Lens 2.0 (2)` with no `WARNING`.
4. `Start New Run` and follow the guide. Push the board into the curved edge of
   the circle, near and far, with plenty of tilt. Edge coverage is what makes the
   ultra-wide model accurate.
5. `Solve`. The recommended model is `double_sphere` whenever it solves (it is
   the only model valid over the whole circular image; the `fisheye` numbers look
   better only because its frame selection drops the edge views). A good result
   is about 0.6 to 1.1 px with `alpha` clearly below 1.0 in
   `<camera_name>_double_sphere.json`. `alpha` at or near 1.0 means weak edge
   coverage: `Resume`, add edge poses, solve again.

Calibrate every camera and mod pair separately, and again whenever a mod is taken
off and refitted. The preset expects the 11x8 `DICT_5X5_100` calib.io board
(34 mm squares, 25 mm markers); edit the board fields if yours differs.

What the camera can and cannot do (Open GoPro API v2.0, GoPro Labs):

- The live webcam stream tops out at **1080p**, over USB or Wi-Fi. Labs RTMP
  live streaming is 480p/720p/1080p too. No live path gives 4K.
- Wi-Fi webcam works on HERO12/13 but offers the same lens modes and adds
  compression, so it gains nothing for calibration. This app drives the USB
  connection only.
- 4K and 2.7K exist only as on-camera recordings. Intrinsics from a recording do
  not transfer to the webcam stream (different crop and processing), or the
  reverse. Calibrate the stream your data was recorded from.
- Labs helps with sharpness and repeatability, not resolution: `Max Shutter
  Angle` gives crisper board corners, and a Labs settings QR code locks the same
  mode on every camera.

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

- `Lens / FOV`: 0 is Wide, 2 is Narrow, 3 is SuperView, 4 is Linear. SuperView
  is an anamorphic stretch that no camera model fits; never calibrate it.
- `Resolution`: 4 is 480p, 7 is 720p, 12 is 1080p

After the webcam starts, the app reads back what the camera actually applied
(webcam lens, lens mod, HyperSmooth) from `/gopro/camera/state`. It shows the
values on the status line, warns on a mismatch, and records them as `reported_*`
in `config.json` and the solve summary. The read-back never blocks the preview.

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

- `gopro13_wide_1080p.yaml` (default): HERO13, Wide, 1080p — `fisheye` solve
- `gopro13_central_chest_fisheye_1080p.yaml`: HERO13 chest-mounted scene camera,
  Wide, 1080p — `fisheye` solve
- `gopro13_umi_gripper_fisheye_1080p.yaml`: HERO13 UMI gripper with Max Lens Mod
  2.0, Wide, 1080p — `fisheye` **plus** `double_sphere` (the DS model needs the
  OpenICC Docker image; see [Double Sphere](#double-sphere-openicc-backend))
- `gopro13_central_linear_pinhole_1080p.yaml`: HERO13 Linear, 1080p —
  `plumb_bob`/`rational_polynomial` for a ROS image_proc pipeline
- `gopro13_linear_1080p.yaml`: HERO13, Linear, 1080p — pinhole solve
- `gopro11_wide_1080p.yaml`: HERO11, Wide, 1080p

All shipped presets are USB-webcam mode at **1080p / 30 fps** — the GoPro webcam
stream tops out at 1080p. 4K/2.7K is only available when *recording on the
camera*, which is a separate offline-calibration workflow (not this app's live
webcam capture). All use the 11x8 `DICT_5X5_100` calib.io board (34 mm square /
25 mm marker), which does not collide with `DICT_4X4` gripper markers.

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
<camera_name>_<model>.yaml              # one per solved model, e.g. _fisheye.yaml
<camera_name>_all_frames_<model>.yaml
<camera_name>_double_sphere.json       # only when the double_sphere model runs
<camera_name>_*_frame_diagnostics.csv
openicc/calibrate_camera.log          # double_sphere backend log (if it ran)
caib_marker_board_calibration_summary.json
```

Pick the YAML with the better median and worst view error and stable distortion
coefficients. For Linear use `plumb_bob`/`rational_polynomial`; for Wide use
`fisheye`; for the Max Lens Mod ultra-wide use `double_sphere` (it fits the
widest lenses best, with `fisheye` as the Docker-free fallback). The diagnostics
decide.

## Double Sphere (OpenICC backend)

Ultra-wide fisheye lenses (GoPro Max Lens Mod, ~150–195°) exceed what the
built-in pinhole/Kannala-Brandt models can fit. The `double_sphere` model covers
them by driving [OpenImuCameraCalibrator](https://github.com/urbste/OpenImuCameraCalibrator)
(AGPL-3.0) as an external subprocess — nothing is linked or vendored. Build its
Docker image once (it fetches OpenICC at a pinned commit into
`~/.cache/gopro-charuco-calibrator/openicc`):

```bash
uv run gopro-charuco setup-openicc
```

If the image is missing or your user cannot reach the Docker daemon, the
double_sphere row in the results says so and names the fix.

Then select **Fisheye + Double Sphere** (or **Double Sphere only**) as the
calibration model, or put `double_sphere` in a preset's `solver.models`. The
solve emits `<camera_name>_double_sphere.json` (UMI/OpenICC-native intrinsics:
focal length, principal point, xi, alpha) at native resolution, plus
`openicc/calibrate_camera.log` for diagnostics. 4K inputs are solved at 1080p
coordinate scale automatically and rescaled.

Environment variables: `OPENICC_DOCKER_IMAGE` (default `openicc`),
`OPENICC_BINARY` (use a native `calibrate_camera` build instead of Docker),
`OPENICC_GRID_SIZE` (default 0.1), `OPENICC_TIMEOUT_S` (default 360). If the
backend is unavailable the model shows up as a failed row and the other models
still solve.

The solver also auto-detects the caib.io marker-column parity per run
(`board.layout` in the summary): caib boards mirror which checkerboard cells
carry markers depending on the grid dimensions, and assuming the wrong parity
silently shifts every odd-row marker by a full square.

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
