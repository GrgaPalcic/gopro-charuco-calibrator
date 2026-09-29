# GoPro ChArUco Calibrator

Calibrate GoPro lenses for robotics, including the ~167° Max Lens Mod fisheye, from the live
USB webcam stream, using a calib.io ChArUco board. It runs in your browser, needs no ROS, and
works one camera after another.

![The calibrator after a solve: step bar, camera readout, live preview, coverage map, setup panel and a Double Sphere result](docs/screenshot.png)

<sub>A solved run on a simulated HERO13 + Max Lens Mod 2.0 feed. The frames are rendered by
[`scripts/screenshot.py`](scripts/screenshot.py); the detection, solve and UI are the app's own.</sub>

## Why

Wide GoPro lenses break the usual calibration recipe. A pinhole model can't fit Wide, OpenCV's
fisheye model can't fit the Max Lens Mod, and SuperView cannot be fitted by any model at all.
This tool:
- **picks a model that fits your lens**, and solves the Max Lens Mod with Double Sphere through
  OpenICC;
- **reads back what the camera actually applied**, because the lens you request is not always
  the lens you get;
- **shows where the board has been**, so you know the edges of the image are covered.

The hard-won details are in [docs/](docs/README.md).

## Quick start

You need [uv](https://docs.astral.sh/uv/) and `ffmpeg` (`sudo apt install ffmpeg` or
`sudo pacman -S ffmpeg`).

```bash
git clone https://github.com/GrgaPalcic/gopro-charuco-calibrator.git
cd gopro-charuco-calibrator
uv run gopro-charuco serve --config gopro_charuco_calibrator/presets/gopro13_wide_1080p.yaml
```

Open http://localhost:8765. Connect the GoPro over USB and click **Open preview**. The first
`uv run` fetches Python 3.12 and every dependency. Nothing needs `sudo`, except possibly a
one-time [firewall rule](#firewall).

Then work through the steps across the top:

1. **Connect.** Open preview. The dot turns green when frames arrive, and the chips next to it
   show the lens and lens mod the camera reports.
2. **Capture.** Start a new run and move the board centre along the guide, matching the box size
   and tilt. The box turns green on a match and views are captured automatically; use
   **Capture** for any it misses.
3. **Solve.** This runs on its own when the route completes. The result shows a pass or retake
   verdict and the recommended model. Resume to add views where the coverage map has gaps.

For the next camera, click **Next camera**, change the camera name, and open the preview again.

## HERO13 with the Max Lens Mod 2.0

Calibrate the camera exactly as your data is recorded. For the UMI-style gripper cameras that is:
mod fitted, USB webcam Wide, 1080p.

1. **Once per machine,** build the Double Sphere backend. You need Docker, usable without sudo;
   the build takes about 10 minutes and no GPU:
   ```bash
   uv run gopro-charuco setup-openicc
   ```
2. **Start with the gripper preset:**
   ```bash
   uv run gopro-charuco serve --config gopro_charuco_calibrator/presets/gopro13_umi_gripper_fisheye_1080p.yaml
   ```
3. **Before capturing, check:**
   - the preview is a **round image with black corners**. An image that fills the frame means an
     anamorphic mode, which cannot be calibrated;
   - the readout shows **Lens Wide (0)** and **Mod Max Lens 2.0 (2)**, with no **Check** chip.
4. **Capture,** pushing the board into the curved edge of the circle, near and far, with plenty
   of tilt.
5. **A good result** is `double_sphere` at about 0.6–1.1 px with alpha clearly below 1.0. alpha at
   its limit means the board missed the edge: Resume, add edge views, solve again.

The preset tells the camera the mod is fitted (setting 189 = Max Lens 2.0). The June reference
calibration was captured without the app setting that, and whether it changes the webcam image is
untested. Calibrate with the camera's lens-mod setting as it was when your data was recorded: the
**Mod** chip shows the current value. The camera keeps the setting, and the app writes it only
when **Lens mod** under **Recording settings** is set
([details](docs/footguns.md#setting-189-changes-the-conditions-maybe-the-image)).

Calibrate every camera and mod pair separately, and again after refitting a mod. To compare two
Double Sphere results, project rays through both; the focal lengths alone can differ by over 50 px at
the same error ([why](docs/footguns.md#double-sphere-parameters-are-not-unique)). A Double Sphere
result does not drop into UMI's pipeline, which expects Kannala–Brandt
([details](docs/umi-and-deployment.md#where-our-calibration-differs)).

## Presets

Every preset uses the USB webcam at 1080p (the webcam maximum) and the 11×8 `DICT_5X5_100` board
(34 mm squares, 25 mm markers), which never collides with 4X4 gripper markers.

| Preset | Camera and lens | Models solved |
|---|---|---|
| `gopro13_umi_gripper_fisheye_1080p` | HERO13 + Max Lens Mod 2.0, Wide | `double_sphere` (recommended), `fisheye` |
| `gopro13_wide_1080p` | HERO13, stock lens, Wide | `fisheye` |
| `gopro13_linear_1080p` | HERO13, stock lens, Linear | `plumb_bob`, `rational_polynomial` |
| `gopro11_wide_1080p` | HERO11, stock lens, Wide | `plumb_bob`, `rational_polynomial`, `fisheye` |

Pick a preset in the UI and click **Load**, or pass it with `serve --config`. **Save as…** writes
the current settings to `~/.config/gopro-charuco-calibrator/presets/`, where they override shipped
presets of the same name.

## Lens → model

| Lens mode | Field of view | Model |
|---|---|---|
| Linear | ~90° | `plumb_bob` / `rational_polynomial` (ROS camera_info) |
| Wide | ~123–130° | `fisheye` (Kannala–Brandt, ROS `equidistant`) |
| Wide with the Max Lens Mod | ~167° lens | `double_sphere` (OpenICC) |
| SuperView, HyperView, Max HyperView | — | none: anamorphic, never calibrate these |

The live stream tops out at 1080p over USB, Wi-Fi and Labs RTMP alike. 4K exists only in
on-camera recordings, and intrinsics from a recording shouldn't be reused on the webcam stream.
The full picture, including Labs firmware and HDMI capture, is in
[lens-modes-and-models.md](docs/lens-modes-and-models.md).

## Outputs

Each run writes `runs/<camera_name>_<timestamp>/`:

```text
config.json                                 settings, plus what the camera reported (reported_*)
frames/capture_###.jpg                      the captured views
overlays/capture_###.jpg                    the same views with detections drawn
<camera>_<model>.yaml                       ROS camera_info per solved cv2 model
<camera>_all_frames_<model>.yaml            the same, before outlier rejection
<camera>_<model>_frame_diagnostics.csv      per-view error and why each view was kept or dropped
<camera>_double_sphere.json                 Double Sphere intrinsics (OpenICC layout)
openicc/calibrate_camera.log                OpenICC's command and output
caib_marker_board_calibration_summary.json  everything, including recommended_model
```

## CLI

Solve a folder of frames without the UI. This includes frames extracted from an on-camera
recording ([how](docs/double-sphere-backend.md#in-app-solver-on-extracted-frames)):

```bash
uv run gopro-charuco solve-frames --frames-dir runs/<run>/frames --output-dir runs/<run> \
  --config runs/<run>/config.json
```

The board can be given with `--cols --rows --square-mm --marker-mm --aruco-dict --start-id
--marker-count` instead. `uv run gopro-charuco setup-openicc` builds the Double Sphere backend.

## Firewall

If the camera answers but no video arrives, a host firewall is dropping the incoming UDP stream.
The app detects this and shows the exact command with a copy button, for example:

```bash
sudo ufw allow in on <gopro-usb-interface> from <gopro-ip> to any port 8554 proto udp
```

firewalld and iptables variants are shown too. Run it once, then open the preview again.

## Docs

| | |
|---|---|
| [lens-modes-and-models.md](docs/lens-modes-and-models.md) | Which modes can be calibrated, model per field of view, capture paths and their limits |
| [footguns.md](docs/footguns.md) | Traps that make a good lens look impossible, as symptom → cause → fix |
| [measurements.md](docs/measurements.md) | Every result we measured, and the pending video-vs-webcam test |
| [double-sphere-backend.md](docs/double-sphere-backend.md) | How the OpenICC integration works; calibrating recordings |
| [umi-and-deployment.md](docs/umi-and-deployment.md) | UMI, HDMI capture for a live robot, multi-camera sync |

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest                                             # the Double Sphere test needs the openicc image
uv run --with playwright python scripts/screenshot.py     # regenerate docs/screenshot*.png, with layout checks
```

To calibrate a plain V4L2 camera or an HDMI capture card, turn off automatic GoPro setup and set
the device under **Frame size and device**.

## License

Apache-2.0, see [LICENSE](LICENSE). The optional OpenICC backend is AGPL-3.0; the app only runs
it as a separate process, and you build it yourself.
