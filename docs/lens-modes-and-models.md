# GoPro lens modes, camera models and capture paths

What the camera actually delivers in each mode, which calibration model fits it, and which
capture paths exist. Measured numbers live in [measurements.md](measurements.md); the traps are
in [footguns.md](footguns.md).

Tags: **verified** = measured by us or read from the primary source; **inferred** = consistent
with the evidence but not directly demonstrated.

## The two layers: physical lens and digital mode

Everything downstream depends on keeping these apart.

- **The physical lens** is the glass in front of the sensor and decides how much of the world
  reaches it. A stock GoPro sees about 120 to 130°. The **Max Lens Mod** is extra glass that
  widens that: 155° for the Max Lens Mod 1.0, 167° clean fisheye for the 2.0.
- **The digital mode** (Wide, Linear, SuperView, HyperView and so on) is the firmware's
  reprojection and crop of what the lens captured. The camera only ever outputs a processed
  crop, never the raw sensor.

So "Linear" is not a narrow lens and "Wide" is not the Max Lens Mod. Linear is a rectilinear
de-fish, and a rectilinear image cannot be wide (edges run off to infinity near 180°), so Linear
stays around 90 to 110° whatever glass is in front of it. **verified**

## Digital modes

| Mode | FOV | Projection | Calibration verdict |
|---|---|---|---|
| Linear | ~90° | rectilinear, firmware de-fish | pinhole (`plumb_bob` / `rational_polynomial`). The only mode a pinhole model fits. Treat as pinhole only with stabilisation off. |
| Wide | ~120–130° (16:9 Wide ~122–123° diagonal on HERO12/13) | true fisheye / barrel | Kannala–Brandt `fisheye` |
| SuperView | ~170° | non-uniform anamorphic stretch | **none**. No model fits it. |
| HyperView | wider | non-uniform anamorphic stretch | **none** |

SuperView and HyperView stretch the 4:3 or 8:7 sensor into 16:9 unevenly (less in the centre,
more at the edges). That is not a radially symmetric projection, so no pinhole or fisheye model
can describe it. **verified**: we measured 27 to 44 px on a SuperView capture, and Joshi et al.
(ICRA 2022) state SuperView "generates non-linear distortions that thwart the calibration of the
camera's intrinsic parameters". Gyroflow calls the modes a "proprietary stretching equation which
is not exactly mapped".

## Max Lens Mod 1.0 vs 2.0

| | Max Lens Mod 1.0 (ADWAL-001) | Max Lens Mod 2.0 (ADWAL-002) |
|---|---|---|
| Clean fisheye mode (calibratable) | Max SuperView ~155° | **Max SuperView 167°** |
| Anamorphic mode (never calibrate) | none | **Max HyperView 177°** |
| Fits | HERO9, 10, 11, 12 | HERO12, 13 (not HERO11) |
| Max resolution | 2.7K60 | 4K60 |
| Used by | UMI (on a HERO9) | us (HERO13) |

**The 177° trap.** The "177°" GoPro advertises for the 2.0 is Max HyperView, an 8:7 frame
squeezed into 16:9: the same anamorphic family as SuperView and HyperView. The widest clean,
radially symmetric fisheye on the 2.0 is Max SuperView at 167°. **verified**, from a GoPro
engineer's lens breakdown (Abe Kislevitz). GoPro's own product copy never says "fisheye" or
"anamorphic", so read the lens menu on the camera before committing. **inferred**

A HERO13 has no clean lens near UMI's 155°. The choice is native Wide (~123°) or Max Lens Mod 2.0
Max SuperView (167°). Matching 155° is not required: UMI is lens-agnostic once you recalibrate
(UMI issue #41). **verified**

In the Open GoPro API the mod is setting **189** on the HERO13 (option 2 = Max Lens 2.0; also
HERO12 and Mission 1). Setting 190 ("Max Lens Mod Enable") is HERO12-only, and setting 162
("Max Lens") is for HERO9, 10 and 11. **verified** against the Open GoPro spec v2.0, 2026-09-29.

## Field of view → model

```
  pinhole            Kannala–Brandt fisheye      Double Sphere / EUCM
  up to ~95°         ~95–150°                    ~150–195°
  |------------------|---------------------------|--------------------|
  0°      Linear ~90°      Wide ~123–130°     Max SuperView 167° ✓   (Max HyperView 177° ✗ anamorphic)
```

| Model | Good for | How it behaved on our data |
|---|---|---|
| Pinhole (`plumb_bob`, 8-coefficient `rational_polynomial`) | up to ~90–95° | 1–2 px on Linear. On the Max Lens Mod circle it reaches 1.3 px only over centre-weighted views, and it cannot model the edges. The rational variant does not rescue ~130° Wide. |
| Kannala–Brandt (`cv2.fisheye`) | ~95–140°, degrades before 180° | 1.12 px on ~130° Wide. 1.19 px on the Max Lens Mod webcam circle, but only because frame selection kept centre views. It fails on the 4K recording once edge views are in. |
| Mei omnidirectional (`cv2.omnidir`) | ~150–195° in theory | Unusable in OpenCV 4.13: diverges (RMS 10⁵⁶) or keeps 1 of 18 views. |
| **Double Sphere via OpenICC** | ~150–195° | **0.62 px** on webcam frames and 0.82 px on the 167° recording (OpenICC's own extractor). In-app, from our detections: 1.11 px webcam and 2.16 px native on 4K. |
| Kalibr, Basalt (DS / EUCM / KB) | same range | Not used: they need an AprilGrid target, not ChArUco. |

Background, from the sources:
- MathWorks models pinhole up to 95°, Kannala–Brandt to 115° and Scaramuzza omnidir to 195°.
  Tangram MetriCal gives RadTan ≤90° and OpenCV-fisheye ≤140°.
- The Double Sphere paper (arXiv 1807.08957) calls pinhole "suboptimal for FOV > 120°". It tests
  lenses from 122 to 195°, including a 150° GoPro, and finds Double Sphere within 1% of KB-8
  accuracy with a closed-form inverse and no z=0 singularity.
- The 180° wall belongs to OpenCV's and Kalibr's pinhole-then-distort **implementation** of
  Kannala–Brandt, not to the model. With a proper implementation KB can fit a 190° lens.
  **verified** (TUM Double Sphere page)

OpenCV has no built-in Double Sphere. This app drives OpenICC for it; see
[double-sphere-backend.md](double-sphere-backend.md).

## Capture paths

Always calibrate in the exact path, lens mode and resolution the data is recorded or deployed
in. The same optics come out differently cropped over each path.

| Path | Max resolution | Lens control | Verdict |
|---|---|---|---|
| **USB webcam** (`/gopro/webcam/start`) | **1080p** (res 4 / 7 / 12) | Wide, Narrow, SuperView, Linear only; no "Max" modes | What this app uses, and how the Ludis dataset was recorded. With the mod fitted, Wide gives a circular fisheye with black corners that Double Sphere calibrates at 0.62 px. |
| Webcam over Wi-Fi | 1080p | same as USB | Supported on HERO12 and 13 only (not HERO9, 10, 11). Same modes plus compression, so nothing is gained for calibration. The app drives USB only. |
| On-camera recording (mp4) | 4K to 5.3K | all modes, incl. Max SuperView (121 = 7), Max HyperView (11), Ultra HyperView (104) | Offline only, with the GPMF IMU in the file. Calibrated at 0.82 px (Max SuperView, 4K). |
| HDMI via Media Mod → capture card | ~1080p (card-dependent) | the camera's current mode | UMI's live deployment path. See [umi-and-deployment.md](umi-and-deployment.md). |
| Wi-Fi preview stream (`/gopro/camera/stream/start`) | not selectable | none | Monitoring only. Not for calibration or a policy. |
| Labs RTMP live stream | 480p, 720p, 1080p | via video settings | Compressed, with latency. Not for robotics. |

- **No live path delivers 4K.** Every live option stops at 1080p. **verified** (Open GoPro spec
  v2.0; Labs live-stream page)
- **Intrinsics do not transfer between paths.** A recording and the webcam stream show the same
  optics reframed differently, so calibrate the one your data came from. **verified**
- **The webcam `fov=` request is not reliable** (Open GoPro #459, closed WONTFIX). The HERO13's
  support table for that parameter does not even list the HERO13. Setting 43 ("Webcam Digital
  Lenses") is the documented HERO13 control. The app reads settings 43, 189 and 135 back after
  the webcam starts and warns if they differ from what was asked for. Still confirm by eye:
  **verified** as a reported problem, **inferred** for our firmware.
- **BLE carries control only** (wake, settings, record start and stop). Images never travel over
  BLE.

### Does webcam mode "apply" the Max Lens Mod?

An early report (10 Jun) said webcam mode cannot apply Max Lens Mod processing and is a dead end.
The dead-end part is **wrong**: the webcam Wide frames with the mod fitted calibrate at 0.62 px
with Double Sphere, better than the 167° recording (0.82 px). The failures that led to that
belief came from OpenCV's solvers and a board-layout bug (see [footguns.md](footguns.md)).

What the camera does internally over webcam is still **unverified**. So is exactly how the
webcam image differs from a Max SuperView recording: crop, black corners, vignette. The planned
side-by-side test in [measurements.md](measurements.md#pending-video-vs-webcam-comparison) will
settle it. For calibration it does not matter, as long as you calibrate the path you record.

## GoPro Labs firmware

Labs adds no webcam resolution, lens or FOV command, so it cannot fix webcam `fov=` and cannot
unlock 4K live. **verified** (Labs extensions page). What it does give:

- **Max Shutter Angle.** A short shutter gives crisper board corners.
- **A settings QR code** puts every camera in exactly the same mode.
- **Time and date QR, and GPS time sync**, for multi-camera recordings (see
  [umi-and-deployment.md](umi-and-deployment.md)).
- **`HDMI=1`** for clean, overlay-free HDMI output with a Media Mod.
- **`EXPQ`** (fixed shutter) and **`WBLK`** (white-balance lock). Both apply to capture; their
  effect on the webcam stream is unverified.

## Sources

- Open GoPro HTTP API (settings 43, 121, 135, 162, 189, 190; webcam start):
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
