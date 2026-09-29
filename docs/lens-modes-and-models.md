# GoPro lens modes, camera models and capture paths

What the camera delivers in each mode, which calibration model fits it, and which capture paths
exist. Measured numbers live in [measurements.md](measurements.md); the traps are in
[footguns.md](footguns.md); terms are explained in the [glossary](README.md#glossary).

Tags: **verified** means measured by us or read from the primary source. **inferred** means
consistent with the evidence but not directly demonstrated. **assessed** means established
practice that we have not re-benchmarked.

## The two layers: physical lens and digital mode

Everything downstream depends on keeping these apart.

- **The physical lens** is the glass in front of the sensor, and decides how much of the world
  reaches it. A stock GoPro sees about 120 to 130°. The **Max Lens Mod** is extra glass that
  widens this: 155° for the Max Lens Mod 1.0, 167° clean fisheye for the 2.0.
- **The digital mode** (Wide, Linear, SuperView, HyperView and so on) is the firmware's
  reprojection and crop of what the lens captured. The camera only ever outputs a processed crop,
  never the raw sensor.

So "Linear" is not a narrow lens, and "Wide" is not the Max Lens Mod. Linear is a rectilinear
de-fish, and a rectilinear image cannot be wide (its edges run off to infinity near 180°). That
keeps Linear at about 90 to 110° whatever glass is in front of it. **verified**

## Digital modes

| Mode | FOV | Projection | Calibration verdict |
|---|---|---|---|
| Linear | ~90° | rectilinear, firmware de-fish | pinhole (`plumb_bob` / `rational_polynomial`). The only mode a pinhole model fits. Treat it as pinhole only with stabilisation off: it is a de-fish plus a stabilisation crop. |
| Wide | ~120–130° (16:9 Wide is ~122–123° diagonal on HERO12/13) | true fisheye / barrel | Kannala–Brandt `fisheye` |
| SuperView | ~170° | non-uniform anamorphic stretch | **none**: no model fits it |
| HyperView | wider | non-uniform anamorphic stretch | **none** |

SuperView and HyperView stretch the 4:3 or 8:7 sensor into 16:9 unevenly: less in the centre,
more at the edges. That is not a radially symmetric projection, so no pinhole or fisheye model can
describe it. **verified** from the literature:
- Joshi et al. (ICRA 2022) write that SuperView "generates non-linear distortions that thwart the
  calibration of the camera's intrinsic parameters". They used Wide at 1960×1080/60 with
  HyperSmooth off, measuring an H-FOV of 118° and a V-FOV of 69°.
- Gyroflow calls these modes a "proprietary stretching equation which is not exactly mapped".

Our own SuperView run gave 27–44 px. It was solved with the mirrored board layout, though, so
treat that number as consistent with this verdict, not as clean evidence for it (see
[measurements.md](measurements.md#other-lens-modes)).

## Max Lens Mod 1.0 vs 2.0

| | Max Lens Mod 1.0 (ADWAL-001) | Max Lens Mod 2.0 (ADWAL-002) |
|---|---|---|
| Clean fisheye mode (calibratable) | Max SuperView ~155° | **Max SuperView 167°** |
| Anamorphic mode (never calibrate it) | none | **Max HyperView 177°** |
| Fits | HERO9, 10, 11, 12 | HERO12, 13 (not HERO11) |
| Max resolution | 2.7K60 | 4K60 |
| Used by | UMI (on a HERO9) | us (HERO13) |

- **The 177° trap.** The "177°" GoPro advertises for the 2.0 is Max HyperView, an 8:7 frame
  squeezed into 16:9: the same anamorphic family as SuperView and HyperView. The widest clean,
  radially symmetric fisheye on the 2.0 is Max SuperView at 167°. **verified**, from a GoPro
  engineer's lens breakdown (Abe Kislevitz). GoPro's product copy never says "fisheye" or
  "anamorphic", so read the camera's lens menu and its reported FOV before committing.
  **inferred**
- **The Max Lens Mod 1.0 is not a HERO13 option.** GoPro lists it for HERO9–12 only. The "HERO13"
  in some shop listings is a retailer artifact, and there are reports of a poor physical fit.
  **verified**
- **So a HERO13 has no clean lens near UMI's 155°.** The choice is native Wide (~123°) or the Max
  Lens Mod 2.0's Max SuperView (167°). Matching 155° is not required: UMI is lens-agnostic once
  you recalibrate (UMI issue #41). **verified**

**The mod in the Open GoPro API.** On the HERO13 the mod is setting **189** (option 2 = Max Lens
2.0). HERO12 and Mission 1 use it too. Setting 190 ("Max Lens Mod Enable") is HERO12-only, and
setting 162 ("Max Lens") is for HERO9, 10 and 11. **verified** against the Open GoPro spec v2.0,
2026-09-29.

## Field of view → model

```
  pinhole            Kannala–Brandt fisheye                Double Sphere / EUCM
  up to ~95°         good to ~140°, soft limit ~150°,      ~150–195°
                     OpenCV's hard wall 180°
  |------------------|-------------------------------------|--------------------|
  0°      Linear ~90°      Wide ~123–130°         Max SuperView 167° ✓   (Max HyperView 177° ✗ anamorphic)
```

| Model | Good for | How it behaved on our data |
|---|---|---|
| Pinhole (`plumb_bob`, 8-coefficient `rational_polynomial`) | up to ~90–95° | 1–2 px on Linear and other narrow captures. On the Max Lens Mod webcam circle it reaches 1.30 px, but only over ~25 centre-weighted views, and it cannot model the edges. The rational variant does not rescue ~130° Wide. |
| Kannala–Brandt (`cv2.fisheye`) | good to ~140°, degrades toward 150°, cannot pass 180° | 1.12 px on a narrow Wide capture (camera not recorded). 1.19 px on the Max Lens Mod webcam circle, but only over ~50 centre-weighted views. It fails on the 4K recording once edge views are in. |
| Mei omnidirectional (`cv2.omnidir`) | ~150–195° in theory | Unusable in OpenCV 4.13: it diverges (RMS 10⁵⁶) or keeps 1 of 18 views. |
| **Double Sphere via OpenICC** (`double_sphere`) | ~150–195° | **0.617 px** on the webcam frames and 0.82 px (at 1080p coordinates) on the 167° recording, both with OpenICC's own extractor. In-app, from our detections: 1.11 px on the webcam frames and 2.16 px native (about 1.1 px at 1080p) on the 4K recording. All four were made in June with the unpatched solver. On synthetic Max Lens Mod views the patched solver lands within 0.32 px of the true lens out to the 83.5° rim. |
| **Kannala–Brandt via OpenICC** (`kannala_brandt`, OpenICC `FISHEYE`) | the file UMI loads; on the 167° image, good out to where the board reached | Synthetic Max Lens Mod views, patched solver: within 0.49 px of the true lens out to the board's reach, 0.32 px out to the rim when the board reached it; up to 1.12 px about 6° past the board. Not yet solved on real frames. |
| Kalibr, Basalt (Double Sphere, EUCM, KB) | same range | Not used: their targets are AprilGrid (plus plain checkerboard), not ChArUco. |

Background, from the sources:
- MathWorks models pinhole up to 95°, Kannala–Brandt to 115° and Scaramuzza omnidir to 195°. It
  also states that "the pinhole model cannot model a fisheye camera".
- Tangram MetriCal gives RadTan ≤90°, OpenCV-fisheye and Pinhole+KB ≤140°, and omnidirectional
  above 140°.
- The Double Sphere paper (arXiv 1807.08957) calls pinhole "suboptimal for FOV > 120°". It tests
  lenses from 122 to 195°, including a 150° GoPro, and finds Double Sphere within 1% of KB-8
  accuracy, faster to compute, with a closed-form inverse and no z=0 singularity.
- The 180° wall belongs to OpenCV's and Kalibr's pinhole-then-distort **implementation** of
  Kannala–Brandt, not to the model: with a proper implementation, KB can fit a 190° lens.
  **verified** (TUM Double Sphere page)

OpenCV has no built-in Double Sphere. This app drives OpenICC for it, and for the Kannala–Brandt
file UMI loads; see [double-sphere-backend.md](double-sphere-backend.md).

The app's **Model** dropdown maps onto these as follows:

| Option | Solves | Use for |
|---|---|---|
| Double Sphere + Kannala–Brandt for UMI (Max Lens Mod, ~167°) | `double_sphere`, `kannala_brandt`, `fisheye` | the Max Lens Mod. Double Sphere is the reference, `kannala_brandt` is the UMI file, and OpenCV's `fisheye` gives a ROS YAML |
| Double Sphere only | `double_sphere` | the Max Lens Mod, when no UMI file or ROS YAML is wanted |
| OpenCV fisheye, Kannala–Brandt (Wide, ~130°) | `fisheye` | Wide (~130°) |
| Pinhole: plumb_bob + rational (Linear, ~90°) | `plumb_bob`, `rational_polynomial` | Linear (~90°) |
| Pinhole + Kannala–Brandt fisheye (compare) | all three cv2 models | comparing on a lens near the boundary, as the HERO11 preset does |

A saved setup from an older version with models `[fisheye, double_sphere]` shows in the UI as the
first option, which now also solves `kannala_brandt`.

## Capture paths

Always calibrate in the exact path, lens mode and resolution your data is recorded in, and deploy
in that same path. The same optics come out differently over each one.

| Path | Resolution | Lens control | Verdict |
|---|---|---|---|
| **USB webcam** | **1080p max** (res 4 / 7 / 12) | Wide, Narrow, SuperView, Linear; no "Max" modes | What this app uses, and how the Ludis dataset was recorded. With the mod fitted, Wide gives a circular fisheye with black corners. Double Sphere fits it: 1.11 px in the app, 0.617 px with OpenICC's own extractor (June, unpatched solver). It is also a live deploy path: the policy sees the same image it was trained on. |
| Webcam over Wi-Fi | 1080p max | same as USB | The spec marks Wi-Fi webcam as not supported on HERO9, 10, 11 and 11 Mini. Same modes, so nothing is gained for calibration. The app drives USB only. |
| On-camera recording (mp4) | 4K to 5.3K | all modes, incl. Max SuperView (setting 121 = 7), Max HyperView (11), Ultra HyperView (104) | Offline only, with the GPMF IMU in the file. Calibrated at 0.82 px (1080p coordinates; Max SuperView, 4K). |
| HDMI via Media Mod → capture card | not verified for the GoPro; UMI's code requests 3840×2160@30 from a Cam Link 4K, otherwise 1920×1080@60 | the camera's current mode | UMI's live deployment path. It is a different image from the webcam stream, so it needs its own calibration and its own training data. See [umi-and-deployment.md](umi-and-deployment.md). |
| Wi-Fi preview stream (`/gopro/camera/stream/start`) | not selectable | none | Monitoring only. Not for calibration or a policy. |
| Labs RTMP live stream | 480p, 720p, 1080p | via video settings | Compressed, with latency. Not for robotics. |

- **No USB, Wi-Fi or RTMP path delivers 4K.** All three stop at 1080p. **verified** (Open GoPro
  spec v2.0; Labs live-stream page). Whether HDMI can carry 4K from a GoPro is not established.
- **Intrinsics from one path should not be reused on another.** Webcam, recording and HDMI reframe
  the same optics, and we have seen a small crop difference between webcam and recording. How
  large the difference in intrinsics is has not been measured yet; that is the pending comparison
  in [measurements.md](measurements.md#pending-video-vs-webcam-comparison). **inferred**
- **How the app starts the webcam.** When the camera answers the legacy gpWebcam API (the HERO11
  does), the app uses `/gp/gpWebcam/START` and sets the lens with `/gp/gpWebcam/SETTINGS?fov=`.
  Otherwise it uses the Open GoPro `/gopro/webcam/start?fov=`. Either way it requests the lens
  through `fov=` and does not write setting 43 (webcam digital lens). The **Lens** setting in the
  form is the request sent as `fov=`; the form has no field for 43. Afterwards it reads settings
  43, 189 and 135 back over the Open GoPro API, when the camera offers one. It warns if 43 differs
  from the requested lens, or if 189 differs from the requested mod when a preset asks for one.
- **`fov=` may be ignored.** Open GoPro #459, closed as WONTFIX, reports it. **inferred**: the issue
  was not re-read, but it matches what we saw on a HERO11. The spec's webcam `fov` table lists
  HERO9–12 and not the HERO13, while setting 43 does list the HERO13 (spec v2.0, read 2026-09-29).
  Trust the read-back and your eyes, not the dropdown.
- **BLE carries control only:** wake, settings, record start and stop. Images never travel over it.

### Does webcam mode "apply" the Max Lens Mod?

An early report (10 Jun) said webcam mode cannot apply Max Lens Mod processing, and is a dead end.
The dead-end part is **wrong**: the webcam Wide frames with the mod fitted calibrate at 0.617 px
with Double Sphere, better than the 167° recording (0.82 px). The failures that led to the belief
came from OpenCV's solvers and a board-layout bug (see [footguns.md](footguns.md)).

Still **unverified**:
- what the camera does internally over webcam;
- exactly how the webcam image differs from a Max SuperView recording (crop, black corners,
  vignette). The planned side-by-side test in
  [measurements.md](measurements.md#pending-video-vs-webcam-comparison) covers this.

The Max Lens Mod preset sets setting 189 = Max Lens 2.0. Calibrate and record with the same preset
(see [footguns.md](footguns.md#setting-189-tells-the-camera-the-mod-is-fitted)).

## GoPro Labs firmware

Labs adds no webcam resolution, lens or FOV command, so it cannot fix webcam `fov=`. Its live
stream stops at 1080p. **verified** (Labs extensions and live-stream pages). What it does give:

- **Max Shutter Angle:** a short shutter gives crisper board corners.
- **QR-code settings:** the same custom configuration can be scanned into every camera
  (**assessed**: the core Labs feature, not tried on our cameras).
- **Time and date QR, and GPS time sync,** for multi-camera recordings (see
  [umi-and-deployment.md](umi-and-deployment.md#multi-camera-sync)).
- **`HDMI=1` (Media Mod):** switches HDMI output from Gallery to clean monitoring with no overlays.
  In a QR command it is `oMHDMI=1` or `$HDMI=1` for the current power-on session, and
  `!MHDMI=1` or `*HDMI=1` to store it permanently; the short `$` and `*` forms need a HERO10 or
  newer. The June notes' `MHDMI=1` was most likely `!MHDMI=1` with the `!` lost. **verified**
  2026-09-29 (Labs extensions page).
- **`EXPQ` (fixed shutter) and `WBLK` (white-balance lock):** both apply to capture. Their effect on
  the webcam stream is unverified.

## Sources

- Open GoPro HTTP API spec v2.0 (settings 43, 121, 135, 162, 189, 190; webcam start):
  https://gopro.github.io/OpenGoPro/http/ and https://github.com/gopro/OpenGoPro
- Webcam `fov=` WONTFIX: https://github.com/gopro/OpenGoPro/issues/459
- Max Lens Mod 2.0: https://gopro.com/en/us/shop/mounts-accessories/max-lens-mod-2/ADWAL-002.html
- Max Lens Mod 1.0: https://gopro.com/en/us/shop/mounts-accessories/max-lens-mod/ADWAL-001.html
- HERO12 and 13 lens breakdown (Max SuperView 167° vs Max HyperView 177°): https://abekislevitz.com/hero12-things-you-should-know/
- Joshi et al., GoPro underwater VI-SLAM (SuperView warning): https://joshi-bharat.github.io/projects/gopro/ and https://arxiv.org/pdf/2203.05640
- Gyroflow GoPro lens notes: https://github.com/gyroflow/docs.gyroflow.xyz/blob/main/getting-started/supported-cameras/gopro.md
- Double Sphere camera model: https://arxiv.org/abs/1807.08957
- MathWorks fisheye basics: https://www.mathworks.com/help/vision/ug/fisheye-calibration-basics.html
- Tangram MetriCal camera models: https://docs.tangramvision.com/metrical/14.1/calibration_models/cameras/
- Kalibr supported models: https://github.com/ethz-asl/kalibr/wiki/supported-models
- GoPro Labs: https://gopro.github.io/labs/ and https://gopro.github.io/labs/control/extensions
- UMI camera code (the capture-card formats): https://github.com/real-stanford/universal_manipulation_interface
