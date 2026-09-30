# GoPro ChArUco Calibrator

Calibrate GoPro lenses for robotics, including the HERO13 lens mods' ultra-wide fisheye, with
a calib.io ChArUco board. You can calibrate from a clip recorded on the camera, or from the live
USB webcam stream. It runs in your browser, needs no ROS, and works one camera after another.

![The calibrator after a solve on the From a recording route: step 1 with the HERO13 + Max Lens Mod 2.0 camera setup and From a recording chosen, the four steps with Next camera highlighted, the QR code for going back to dataset settings, the views kept and the positions covered, and a passed Double Sphere result with the files it wrote](docs/screenshot.png)

<sub>A solved run on the From a recording route. The frames are simulated 4:3 views, rendered by
[`scripts/screenshot.py`](scripts/screenshot.py) and shown as if they came from a 4000×3000
HERO13 clip. The clip's serial and file facts are made up for the picture, because no real HERO13
clip has been through the app yet. The detection, solve and UI are the app's own.</sub>

## Two routes

The app has two ways to calibrate. You choose one in step 1, under **How will you calibrate?**.
A calibration is only valid for images made the same way, so pick the route that matches how your
data is recorded.

| Your data is… | Use | Why |
|---|---|---|
| recorded on the camera to its card (mp4), like UMI and the Ludis dataset | **From a recording**, always | It calibrates a clip recorded in exactly the dataset's mode. |
| the live USB webcam stream, read by a computer | **Live over USB** | It calibrates the webcam stream only. |

**A USB calibration is not valid for footage recorded on the camera.** The webcam stops at 1080p
16:9, its lens menu has no lens-mod lens, and it handles stabilisation its own way
([details](docs/footguns.md#a-webcam-calibration-is-not-valid-for-on-camera-recordings)).

## Why

Wide GoPro lenses break the usual calibration recipe. A pinhole model can't fit Wide. OpenCV's
fisheye model only looks good on the lens mod because it keeps the centre views and drops the
edge ones. SuperView cannot be fitted by any model at all.
This tool:
- **picks a model that fits your lens.** It solves the lens mod with Double Sphere through
  OpenICC, and also writes a Kannala–Brandt file that UMI can load;
- **checks what the camera actually did.** On the recording route it reads the clip's own
  metadata; on the USB route it reads back the settings the camera applied, because the lens you
  request is not always the lens you get;
- **shows where the board has been**, so you know the edges of the image are covered.

The hard-won details are in [docs/](docs/README.md).

## Quick start

You need [uv](https://docs.astral.sh/uv/) and `ffmpeg` (`sudo apt install ffmpeg` or
`sudo pacman -S ffmpeg`). For a HERO13 lens mod you also need Docker, usable without sudo.

```bash
git clone https://github.com/GrgaPalcic/gopro-charuco-calibrator.git
cd gopro-charuco-calibrator
uv run gopro-charuco setup-openicc      # once per machine, for a lens mod: about 10 minutes, no GPU
uv run gopro-charuco serve --config gopro_charuco_calibrator/presets/gopro13_mlm2_adwal002.yaml
```

Open http://localhost:8765. The first `uv run` fetches Python 3.12 and every dependency.

- **`setup-openicc`** builds the OpenICC backend, which solves Double Sphere and the
  Kannala–Brandt file for UMI. It builds the image `gopro-charuco-openicc:d75dda5-p1`. If you built
  the older `openicc` image, run the command again: the app no longer uses that image, and its
  solver was unpatched ([why](docs/double-sphere-backend.md#the-source-patch)). Without the
  image, those two models report the setup command and the other models still solve.
- **`--config`** picks the camera setup the page opens with. You can also pick it in step 1.
- Nothing needs `sudo`, except possibly a one-time [firewall rule](#firewall) for the USB route.

Work through the four steps across the top. When a button is amber, it is the next thing to
click; while the app is busy, none is. A greyed-out button tells you why when you hover over it.

## From a recording

Use this route for any footage recorded on the camera. You set the camera up with a QR code,
record one clip while moving the board, and drop the clip into the app. The app checks the clip,
picks the views and solves. Details and the evidence behind each setting are in
[docs/recording-route.md](docs/recording-route.md).

![The Record step of the From a recording route: steps 1 and 2 done, the I've recorded the clip button highlighted, the instruction to record 60 to 90 seconds, and the board animation paused at position 5 of 23, far, top-right corner, with the dots for the positions already shown and still to come](docs/screenshot-recording.png)

**Before you start:** a HERO13 with GoPro Labs firmware, the lens mod fitted, a charged battery,
a card with room (a 90 s clip is about 1.4 GB), and the ChArUco board printed flat: 11×8 squares
of 34 mm, 25 mm markers from `DICT_5X5_100`, first marker ID 2 (ours is an A3 print from calib.io;
see [Presets](#presets)). The clip is copied into the run folder, so the computer needs that much
room too.

1. **Camera setup.** Pick the camera setup that matches your lens mod. Check the code printed on
   the mod: **ADWAL-002** is the Max Lens Mod 2.0, **AEWAL-001** the Ultra Wide Lens Mod
   ([how they differ](#hero13-with-a-lens-mod)). Under **How will you calibrate?**, choose
   **From a recording**.
2. **Settings.** Click **Show settings** if it is not open.
   - Switch the camera on, point it at **QR code 1** on the screen and let it scan. QR code 1 is
     for the calibration clip: 4K 4:3, 60 fps, lens Ultra Wide, HyperSmooth Off, the lens mod,
     and the shutter locked at 1/480 s.
   - Keep **QR code 2** folded away while you scan: the camera sees the whole screen. QR code 2 is
     the same settings with the shutter back on Auto, for recording the dataset afterwards.
   - Check each line of the checklist against the camera screen. Some codes are not yet confirmed
     on a HERO13; the page lists them and says what to set by hand if the screen shows something
     else ([what is unverified](#what-is-not-confirmed-on-a-camera-yet)).
   - Click **I've set the camera** when everything matches.
3. **Record.** The animation shows the camera's view of the 4:3 frame and the board moving
   through the 23 positions the calibration needs: the centre, a far ring, a near ring, then four
   tilted positions. The caption names each one ("position 5/23 · far · top-right corner").
   **Play** and **Pause** control it; **All positions in order** lists them. With reduced motion
   turned on in your system, it shows a numbered map instead.
   - Record 60–90 s on the camera. Move slowly and hold each position for about a second. Push the
     board right to the edges of the frame.
   - Click **I've recorded the clip**.
4. **Drop clip & solve.** Copy the clip (`GX01xxxx.MP4`) from the camera's card, then drop it on
   the page or click **Choose clip…**. Use the original file, not an exported or re-encoded copy.
   - The page shows the copy's progress, then the analysis: frames read, views kept and why the
     others were left out, and the positions covered. Keep the page open while the clip copies.
     A 90 s clip can take a few minutes to analyse (not yet timed on a real clip).
   - Then it solves, and shows the result with the files it wrote and what each one is for. Its
     badge is **PASS** (coverage met, the recommended model looks sound), **RETAKE** (see
     [Retake](#retake) below), **INCOMPLETE** (Double Sphere or the UMI file did not solve; the
     result says how to fix it) or **FAILED** (no model solved; the reasons are listed).
   - If you left **Camera name** as the camera setup filled it in, the run is renamed after the
     camera's serial number in the clip (for example `gopro13_1234`), so each file maps to one
     camera. If you typed a name yourself, it stays for the next camera too: change it for each
     camera, or put back the camera setup's name so the serial is used.

### The clip check

Before it picks views, the app reads the clip's file facts and the GoPro metadata inside it, and
compares them with the camera setup: camera model, resolution, frame rate, HyperSmooth and
shutter, plus the length, the motion sensor data and the serial. It never blocks the solve, but
when a setting differs it shows a red box above the result, and the headline repeats it:

> This clip was not recorded with the preset's settings: Frame rate is 29.97 fps, expected
> 60 fps. Check every setting on the camera again, and record the clip again if any differ.
> The calibration below is only valid for footage recorded exactly like this clip.

(The frame rate is an example: the box names whichever settings differ.)

Then fix the setting on the camera, click **Start this camera again** (the run keeps its files),
and do steps 2 to 4 with a new clip. Do not add the new clip to the flagged run. The file cannot
show the lens or the lens mod, so the page always asks you to check those on the camera screen.
What each field means is in [docs/recording-route.md](docs/recording-route.md#the-clip-check).

### Retake

If the result is RETAKE, or there are not yet enough views to solve, the page lists the missing
positions by their animation numbers and says why most frames were left out (for example "the
board was moving"). Record another clip that covers them and click **Add another clip**. Its
views are added to this run and everything is solved again. When the only problem is that the two
fisheye solves disagree, there is no position to list: add another clip with views near the edge
of the circle.

What happens to the retake clip:
- A clip from another camera starts a run of its own, with this run's camera setup (see
  [Next camera](#next-camera) if your cameras have different lens mods).
- A clip of another picture size than the run's first clip is not used.
- A clip at another frame rate or with HyperSmooth on is used, but flagged in red.
- Another lens cannot be told from the file, so check the camera screen before every retake.

### Next camera

When the result passes, scan **QR code 2** (it is shown again next to the result) before you
record the dataset, so the shutter goes back to Auto. Then click **Next camera**. It ends this
run, keeps its files, and starts again at step 2 for the next GoPro, with the same camera setup.

Check the code on the next camera's lens mod. If it differs, pick its camera setup in step 1
before you scan QR code 1: the two setups have different QR codes, and the clip check cannot see
which mod was set.

## Live over USB

**This route calibrates the webcam stream only; it is not valid for on-camera recordings.** Use it
when the data is the live webcam stream itself. Choose **Live over USB** in step 1.

1. **Camera setup.** Pick the camera and lens, for example "HERO13 + Max Lens Mod 2.0
   (ADWAL-002)". The settings on the right fill in at once.
2. **Connect.** Plug the GoPro in over USB, switch it on and click **Open preview**. The dot at the
   top turns green when frames arrive, and the chips at the top right show the lens and lens mod
   the camera reports. **Stop** ends the webcam stream.
3. **Capture.** Click **Start new run**, then hold the board where the orange box is, matching its
   size and tilt. Views save automatically when the board matches; **Capture now** saves the
   current view straight away. **Pause** stops saving views while the preview keeps running.
4. **Solve.** This runs on its own when the guide is done, or click **Solve** once there are
   enough views. The result names the recommended model and lists the files it wrote, with what
   each one is for. Its badge says:
   - **PASS:** the coverage targets are met and the recommended model looks sound;
   - **RETAKE:** follow the amber button: usually **Resume**, add the views it asks for, and solve
     again; when the only problem is that the two fisheye solves disagree, **Solve** again first;
   - **INCOMPLETE:** Double Sphere or the UMI file (Kannala–Brandt) did not solve, for example
     because the OpenICC image is not built; the result says how to fix it;
   - **FAILED:** no model solved; the reasons are listed.

For the next camera, click **Next camera** in step 4. It ends the run. Plug in the next GoPro, give
it its own **Camera name** in Settings (it names the files), and click **Open preview**.

With a lens mod on this route, check before capturing that the preview is a **round image with
black corners**, and that the readout shows **Lens Wide (0)** and the right **Mod** with no
**Check** chip. If not, the mod may be off or the lens mode wrong.

## HERO13 with a lens mod

Two lens mods fit the HERO13. GoPro says the Max Lens Mod 2.0 "has the same settings and
features" as the Ultra Wide Lens Mod, "except for auto-detection". Each has its own camera setup
in step 1, and each carries the settings for both routes.

| | Max Lens Mod 2.0 | Ultra Wide Lens Mod |
|---|---|---|
| Code printed on the mod | **ADWAL-002** | **AEWAL-001** |
| Camera setup in step 1 | HERO13 + Max Lens Mod 2.0 (ADWAL-002) | HERO13 + Ultra Wide Lens Mod (AEWAL-001) |
| Preset file | `gopro13_mlm2_adwal002.yaml` | `gopro13_uwlm_aewal001.yaml` |
| How the camera knows it is fitted | **not detected:** select it on the camera, or scan QR code 1 | detected automatically when fitted |
| Open GoPro setting 189, set by the USB route | 2 = Max Lens 2.0 | 100 = Auto Detect |
| Labs lens-mod code in the QR codes | `oX2` (Max Lens Mod 2.0) | `oX10` (auto-detect lens mods) |
| Recording mode (both QR codes) | 4K 4:3 (4000×3000), 60 fps, Ultra Wide, HyperSmooth Off | the same |

Setting 189 also has 3 = Max Lens 2.5, for the HERO13 only. That is almost certainly the Ultra
Wide Lens Mod, but no GoPro source says so, so its camera setup uses Auto Detect.

With a lens mod, 4:3 exists only at 4K (4000×3000), at 60, 50, 30, 25 or 24 fps, with the lenses
Ultra Wide, Wide or Linear. Ultra Wide at 4:3 is 145° horizontal × 113° vertical × 176° diagonal
(GoPro's figures; more in
[lens-modes-and-models.md](docs/lens-modes-and-models.md#hero13-lens-mods-at-43)).

### What is not confirmed on a camera yet

These come from the GoPro Labs and Open GoPro docs, but nobody has tried them on our cameras. The
page marks each one and says what to set by hand if the camera screen shows something else.
- **The lens code `fX`.** The Labs QR creator documents `oX2fX` as "Enable MSV" (Max SuperView)
  for HERO12–13. On a HERO13 at 4:3 the mod's widest lens is called Ultra Wide; that `fX`
  selects it is not documented.
- **`oX10` with `fX`** (Ultra Wide Lens Mod): how auto-detect and the lens code combine is not
  documented. The Labs notes also list `oX3` for Max Lens 2.5, so `oX3fX` may be the direct
  counterpart; the page offers it as an alternative to try.
- **The shutter lock `S45`** (45° at 60 fps = 1/480 s): check that the camera shows 1/480.
- **What the HERO13 writes in the clip's metadata.** The lens tag, the shutter tag and their
  exact shapes have not been read from a real HERO13 clip, so the clip check reads them
  defensively and says "unknown" where the file does not say.

Once a camera has scanned the codes and one real clip has been checked, the result goes into
[measurements.md](docs/measurements.md#recording-route-first-real-clip).

### Checking the result

- **alpha** (Double Sphere) should sit clearly below 1.0. alpha at its limit means the board
  missed the edge: add views near the edge (a clip on the recording route, **Resume** on the USB
  route).
- The **For UMI** row reads "Kannala–Brandt for UMI: matches Double Sphere within X px out to Y°".
  Y is the widest angle the board reached. If the two disagree by more than 1 px (at 1080p; the
  limit grows with the image height), the result turns RETAKE and lists the gap. On the USB route,
  click **Solve** again first, and add views near the edge only if the gap stays. On the recording
  route there is no Solve button: click **Add another clip** and record views near the edge; the
  run is solved again with them. A warning about the aspect ratio only adds a note under PASS
  ("See the note on the For UMI row").
- For scale: on simulated lens-mod views, where the true lens is known, both models landed
  within 0.5 px of it out to the widest angle the board reached
  ([measurements](docs/measurements.md#synthetic-max-lens-mod-through-openicc-2026-09-29)).
  No real clip has been through the recording route yet. On real webcam frames the only in-app
  Double Sphere result so far is 1.11 px RMS, made in June with the older unpatched solver.

Calibrate every camera and mod pair separately, and again after refitting a mod. To compare two
Double Sphere results, project rays through both; the focal lengths alone can differ by nearly
30 px at the same error, and by over 50 px with the old unpatched image
([why](docs/footguns.md#double-sphere-parameters-are-not-unique)).

### Using the files in UMI

- **Intrinsics.** Use `<camera>_kannala_brandt.json`. UMI reads a fixed file name, so copy it as
  `gopro_intrinsics_2_7k.json` into a calibration folder that also holds `aruco_config.yaml`
  (UMI's `example/calibration/aruco_config.yaml`, or your own if your gripper markers differ), and
  run `python run_slam_pipeline.py -c <that folder> <session_dir>`. On the recording route the
  json stays at the clip's own size (4000×3000); UMI rescales it to the video.
- **ORB-SLAM3.** `<camera>_kannala_brandt_orbslam3.yaml` replaces the lines with the same keys in
  UMI's settings file, `gopro10_maxlens_fisheye_setting_v1_720.yaml`; keep its `File.version`,
  `Camera.RGB` and IMU lines. On the recording route the block is written at **960×720**, the
  size UMI's SLAM runs at (4000×3000 × 0.24); on the USB route it stays at 1920×1080.
- **Several cameras.** UMI reads one `gopro_intrinsics_2_7k.json` per `-c` folder, so make one
  calibration folder per camera, from that camera's run (`gopro13_<last 4 of the serial>` on the
  recording route), and run each camera's sessions with its own folder. Whether one UMI session
  can mix cameras with a file each is not something we have checked (**unverified**).
- **Before relying on SLAM,** read
  [what we have not checked](docs/umi-and-deployment.md#loading-our-calibration-in-umi):
  UMI's SLAM masks UMI's own gripper, and its IMU settings are not ours.
- The Double Sphere JSON is the reference fit for tools that take that model.

## Presets

Every preset uses the 11×8 `DICT_5X5_100` board (34 mm squares, 25 mm markers), which never
collides with 4X4 gripper markers. On the USB route every preset uses the webcam at 1080p, the
webcam maximum.

| Preset | Shown in step 1 as | Routes | Models solved |
|---|---|---|---|
| `gopro13_mlm2_adwal002` | HERO13 + Max Lens Mod 2.0 (ADWAL-002) | From a recording (4K 4:3, 60 fps, Ultra Wide); Live over USB (Wide 1080p, setting 189 = 2) | `double_sphere` (recommended), `kannala_brandt` (the file for UMI), `fisheye` (the ROS YAML) |
| `gopro13_uwlm_aewal001` | HERO13 + Ultra Wide Lens Mod (AEWAL-001) | From a recording (4K 4:3, 60 fps, Ultra Wide); Live over USB (Wide 1080p, setting 189 = 100) | the same |
| `gopro13_wide_1080p` | HERO13 Wide 1080p (stock lens) | Live over USB | `fisheye` |
| `gopro13_linear_1080p` | HERO13 Linear 1080p (stock lens) | Live over USB | `plumb_bob`, `rational_polynomial` |
| `gopro11_wide_1080p` | HERO11 Wide 1080p (stock lens) | Live over USB | `plumb_bob`, `rational_polynomial`, `fisheye` |

`gopro13_mlm2_adwal002` replaces `gopro13_umi_gripper_fisheye_1080p` ("HERO13 + Max Lens Mod 2.0
(UMI gripper)"), with the same USB settings. Its **Camera name** is still `gopro13_umi_gripper`
(`gopro13_uwlm_gripper` for the Ultra Wide Lens Mod), so USB-route runs from it are still written
to `runs/gopro13_umi_gripper_<timestamp>/`; recording-route runs are renamed after the camera's
serial. A preset without recording settings offers only the USB route.

Pick a preset in step 1, **Camera setup**, where it applies as soon as you choose it, or pass it
with `serve --config`. In the settings panel, **Save as…** writes the current settings to
`~/.config/gopro-charuco-calibrator/presets/`, where they override shipped presets of the same name
and appear in step 1. **Starting settings** puts back the settings the server started with.

## Lens → model

| Lens mode | Field of view | Model |
|---|---|---|
| Linear | ~90° | `plumb_bob` / `rational_polynomial` (ROS camera_info) |
| Wide | ~123–130° | `fisheye` (Kannala–Brandt, ROS `equidistant`) |
| Ultra Wide 4:3 with a lens mod (recording route) | 145° × 113°, 176° diagonal (GoPro) | `double_sphere` (OpenICC), plus `kannala_brandt` (OpenICC `FISHEYE`) for UMI |
| Webcam Wide with a lens mod (USB route) | ~167° (the 16:9 Max SuperView figure; [how it relates to 176°](docs/lens-modes-and-models.md#hero13-lens-mods-at-43)) | the same |
| SuperView, HyperView, Max HyperView | — | none: anamorphic, never calibrate these |

The live stream tops out at 1080p over USB, Wi-Fi and Labs RTMP alike. 4K exists only in
on-camera recordings. A calibration from one path is not valid for the other. The full picture,
including Labs firmware and HDMI capture, is in
[lens-modes-and-models.md](docs/lens-modes-and-models.md).

## Outputs

Each run writes `runs/<camera_name>_<timestamp>/`:

```text
config.json                                 settings, plus what the camera or clip reported
frames/capture_###.jpg                      the views (captured live, or picked from the clip)
overlays/capture_###.jpg                    the same views with detections drawn
<camera>_<model>.yaml                       ROS camera_info per solved cv2 model
<camera>_all_frames_<model>.yaml            the same, before outlier rejection
<camera>_<model>_frame_diagnostics.csv      per-view error and why each view was kept or dropped
<camera>_double_sphere.json                 Double Sphere intrinsics (OpenICC layout)
<camera>_kannala_brandt.json                Kannala–Brandt intrinsics for UMI (its gopro_intrinsics_2_7k.json layout)
<camera>_kannala_brandt_orbslam3.yaml       the same as an ORB-SLAM3 KannalaBrandt8 camera block
openicc/calibrate_camera.log                OpenICC's command and output for Double Sphere
openicc_kannala_brandt/calibrate_camera.log the same for Kannala–Brandt
caib_marker_board_calibration_summary.json  everything, including recommended_model and the UMI check
```

The recording route adds or changes these:

| Item | What it holds |
|---|---|
| `clips/` | Every clip you dropped into the run, copied as it was (`GX010042.MP4`, a second copy of the same name as `GX010042_2.MP4`). |
| `config.json` | `config.camera` is the camera as recorded (the clip's size and frame rate), and `acquisition_mode.route` is `"recording"`, with the lens and lens mod as set on the camera, the clip's codec, camera model, serial and firmware. |
| Summary, `recording.clips` | Per clip: every clip-check row (`field`, `label`, `expected`, `found`, `status`, `advice`), the ffprobe facts, the GoPro metadata read, the drop counts and the views kept from it. |
| Summary, `recording.counts` | Frames read (`samples`), `kept`, and why the rest were left out: `no_board`, `blurred`, `moving`, `duplicate`, `over_cap`. |
| Summary, `recording.mismatch_*` | The settings that differ from the camera setup, and the clips they are in. |
| Summary, `recording.serial`, `recording.camera_named_from_serial` | `serial` is the camera's serial; `camera_named_from_serial` says whether the run was named from it. |
| `<camera>_kannala_brandt_orbslam3.yaml` | Written at **960×720** for a 4:3 clip, the size UMI's SLAM runs at; the json files stay at 4000×3000. `recording.orbslam3` in the summary says so. |

## CLI

Solve a folder of frames without the UI:

```bash
uv run gopro-charuco solve-frames --frames-dir runs/<run>/frames --output-dir runs/<run> \
  --config runs/<run>/config.json
```

The board can be given with `--cols --rows --square-mm --marker-mm --aruco-dict --start-id
--marker-count` instead. `uv run gopro-charuco setup-openicc` builds the OpenICC backend for
`double_sphere` and `kannala_brandt`. For a clip recorded on the camera, the From a recording
route does the frame picking for you; the manual way is in
[double-sphere-backend.md](docs/double-sphere-backend.md#on-camera-recordings).

## Firewall

On the USB route, if the camera answers but no video arrives, a host firewall is dropping the
incoming UDP stream. The app detects this and shows the exact command with a copy button, for
example:

```bash
sudo ufw allow in on <gopro-usb-interface> from <gopro-ip> to any port 8554 proto udp
```

firewalld and iptables variants are shown too. Run it once, then open the preview again.

## Docs

| | |
|---|---|
| [recording-route.md](docs/recording-route.md) | The From a recording route: settings, QR codes and what is verified, how to record, the clip check, how views are picked, outputs, loading the files in UMI |
| [lens-modes-and-models.md](docs/lens-modes-and-models.md) | Which modes can be calibrated, model per field of view, the HERO13 lens mods at 4:3, capture paths and their limits |
| [footguns.md](docs/footguns.md) | Traps that make a good lens look impossible, as symptom → cause → fix |
| [measurements.md](docs/measurements.md) | Every result we measured, the pending video-vs-webcam test, and a place for the first real recording-route clip |
| [double-sphere-backend.md](docs/double-sphere-backend.md) | How the OpenICC backend solves Double Sphere and Kannala–Brandt, its source patch; calibrating recordings by hand |
| [umi-and-deployment.md](docs/umi-and-deployment.md) | UMI, loading our files into it, HDMI capture for a live robot, multi-camera sync |

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest                                             # the OpenICC tests need the setup-openicc image
uv run --with playwright python scripts/screenshot.py     # regenerate the two docs/ screenshots, with layout checks
```

The recording-route tests need `ffmpeg` and skip without it.

To calibrate a plain V4L2 camera or an HDMI capture card, turn off automatic GoPro setup and set
the device under **Frame size and device**.

## License

Apache-2.0, see [LICENSE](LICENSE). The optional OpenICC backend is AGPL-3.0; the app only runs
it as a separate process, and you build it yourself.
