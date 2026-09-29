# Footguns

The traps that cost us time, in the form **symptom → cause → how to tell → fix**. Most of them
look exactly like "this lens cannot be calibrated", and most of them are not.

## Capture

### SuperView and HyperView cannot be calibrated
- **Symptom:** 27–44 px RMS whatever model you pick (fisheye is even worse, ~49 px).
- **Cause:** these modes stretch the sensor into 16:9 unevenly. That is not a lens projection any
  model can describe.
- **How to tell:** the image fills the whole 16:9 frame, and straight lines bend differently near
  the left and right edges than near the top and bottom.
- **Fix:** Wide for fisheye, Linear for pinhole, and Max SuperView (recording) or webcam Wide with
  the mod fitted for Double Sphere. The UI labels these modes "anamorphic, uncalibratable". The
  old app defaulted to SuperView at 720p, which is how a whole run of these was produced.

### "177°" is the anamorphic mode
- **Symptom:** you pick the Max Lens Mod 2.0's headline mode and get the numbers above.
- **Cause:** 177° is **Max HyperView**, anamorphic. The clean fisheye is **Max SuperView, 167°**.
- **Fix:** never Max HyperView.

### The webcam lens request can be ignored
- **Symptom:** the preview does not look like the lens you asked for.
- **Cause:** `fov=` on `/gopro/webcam/start` is unreliable (Open GoPro #459, WONTFIX). On a HERO11,
  Wide and SuperView both looked flat over webcam.
- **How to tell:** the status line shows what the camera reports (`webcam digital lens`, setting
  43; `max lens mod`, setting 189) and prints `WARNING` on a mismatch. The same values are saved
  as `reported_*` in `config.json` and the solve summary.
- **Fix:** trust your eyes and the read-back, not the dropdown. With the mod fitted, the webcam
  Wide image is a circle with black corners.

### Stabilisation warps frames
- **Symptom:** a solve that is unstable or worse than it should be.
- **Cause:** HyperSmooth and horizon lock warp each frame non-rigidly, so the lens model changes
  from frame to frame.
- **Fix:** both off. The read-back records `hypersmooth` (setting 135). Treat that as the video
  preset's value; its effect on the webcam stream is unverified.

### Intrinsics belong to one path, one mode, one resolution
- **Symptom:** a calibration that was fine suddenly does not fit the data.
- **Cause:** webcam, recording and HDMI reframe the same optics differently. Changing lens mode or
  resolution changes the model. Taking a mod off and putting it back can move it slightly.
- **Fix:** calibrate exactly the stream the data came from. Do it per camera and mod pair, and
  again after refitting a mod. The Ludis dataset is webcam Wide at 1080p with the mod fitted.

## Board

### The caib.io board layout can be mirrored
This was the most expensive bug. It made a good lens look impossible for days.

- **Symptom:** 52–108 px RMS that looks exactly like a hopeless lens, while the median residual
  of each view stays small. Whole rows of markers are outliers.
- **Cause:** caib.io puts markers on alternating checkerboard cells, and which parity row 0 starts
  on depends on the board's dimensions (its corner colour). Our 11×8 5X5 board matched the parity
  the code assumed, so it calibrated and seemed to prove the board builder right. The 10×7 4X4
  board is mirrored: every odd-row marker sat one full square from where the model put it.
- **How it was found:** a per-marker homography check. IDs 5–9, 15–19 and 25–29 (the odd rows)
  were all off by ~75 px, about one square. Testing the parity variants gave 1.9 px for the
  mirrored one against 37 px for the assumed one. A geometry sweep had earlier "verified" the
  board (`cols=10, rows=7` fit best, highest ID 34); a sweep cannot see a parity mirror.
- **How to tell:** tens of pixels of error with small per-view medians. Check residuals per marker
  ID: structured outliers (whole rows or columns) mean a wrong board model, not the lens.
- **Fix, already in the app:** `detect_board_layout()` scores both parities against the real
  detections, using a RANSAC homography and the median per-corner residual over the five best
  views, and keeps the better one. The choice is recorded as `board.layout: standard | flipped`
  in the summary. No configuration needed.

### Calibration board and gripper markers share a dictionary
- **Symptom:** outliers when a gripper or scene marker is in view. Gripper tracking gets confused
  whenever the board is visible.
- **Cause:** the board and the functional markers share a dictionary and ID range, so each can be
  read as the other: false 3D↔2D correspondences, the same outlier class as the layout bug.
- **Fix:** calibration boards and gripper markers must use different dictionary families. The
  5X5 board with 4X4 grippers never cross-decodes (different bit grids). This is why every preset
  uses the 11×8 `DICT_5X5_100` board.

### Board choice
- **Detail:** OpenICC's default board spec (`DICT_ARUCO_ORIGINAL`, marker = ½ square) is a tool
  default, not a best practice. The original dictionary has weak inter-marker Hamming distance,
  and larger markers (calib.io's ~0.71 ratio) detect better.
- **Why it rarely matters:** the app feeds OpenICC its own detections, so neither constraint
  applies. They matter only when you run OpenICC's extractor directly (see
  [double-sphere-backend.md](double-sphere-backend.md#manual-openicc-on-a-recording)).
- **Guidance:** bigger squares and closer views give crisper corners. That helps, but it never
  fixes a wrong mode or model.

## Solving

### Kannala–Brandt looks better than it is on the Max Lens Mod
- **Symptom:** `fisheye` reports ~1.2 px on the Max Lens Mod circle, close to Double Sphere.
- **Cause:** its frame selection kept ~50 centre-weighted views and dropped the edge ones. OpenCV's
  KB misfits the far periphery, which is exactly where UMI's side mirrors sit. On the 4K recording
  with edge views, it fails outright.
- **Fix:** Double Sphere is the model for the Max Lens Mod. The app recommends `double_sphere`
  whenever it solves, and never compares its error against a centre-weighted median. Keep
  `fisheye` as a Docker-free cross-check only.

### Double Sphere parameters are not unique
- **Symptom:** two solves of the same data disagree on focal length by 50 px (573 vs 626) at the
  same 0.186 px RMS.
- **Cause:** f, xi and alpha trade off against each other, and OpenICC's view selection is not
  deterministic. Measured 2026-09-29 (see [measurements.md](measurements.md#synthetic-double-sphere-through-openicc-2026-09-29)).
- **How to tell:** project the same set of rays through both models and compare pixels. Here they
  agreed within 0.73 px out to 70° off-axis.
- **Fix:** compare, test and monitor Double Sphere models by ray projection, never by f, xi or
  alpha alone. The same goes for any focal comparison between recording and webcam.

### alpha pinned near 1.0 means weak coverage
- **Symptom:** a Double Sphere result with `alpha` at or near 1.0.
- **Cause:** the board never reached the curved edge of the circle, so the edge is unconstrained.
  The 4K recording gave 0.82 px with alpha 1.0; the better-spread webcam set gave 0.62 px with
  alpha 0.71.
- **Fix:** sweep the whole circle: edges, near and far, lots of tilt. Even then the black-corner
  periphery is never sampled, so the model is best where the board went.

### OpenICC drops views with an absolute 2 px threshold
- **Symptom:** a 4K Double Sphere solve that keeps too few views and dies.
- **Cause:** 2 px at 4K is 1 px at 1080p. Equally good views get purged at higher resolution.
- **Fix, in the app:** coordinates are divided down to ≤1080p before the solve, and focal length,
  principal point and error are scaled back afterwards. It is an exact unit change: corners are
  still detected at full resolution, xi and alpha are scale-free, and
  `solve_downsample_factor` is recorded in the artifact.

### `cv2.fisheye` needs coaxing
- **Detail:** use `CALIB_RECOMPUTE_EXTRINSIC | CALIB_FIX_SKEW | CALIB_CHECK_COND`, with object
  points shaped `(N,1,3)`.
- **Errors:** on "Ill-conditioned matrix for input array N" or `InitExtrinsics: fabs(norm_u1) > 0`,
  drop that view rather than disabling the check. The app does this automatically.

### `cv2.omnidir` is unusable here
- **To make it run at all:** pass a list of variable-length `(N,1,3)` float64 C-contiguous arrays
  with `K=None, xi=None, D=None`. Equal-shape lists get coerced into one Mat and assert at
  `omnidir.cpp:854`.
- **Even then:** it diverged (RMS 10⁵⁶) or kept 1 of 18 views on our data. That is OpenCV's
  binding and optimiser, not the Mei model. Use a purpose-built Double Sphere tool.

## Downstream

### A Double Sphere result does not drop into UMI
- **Symptom:** UMI's loader fails, or you are tempted to convert the numbers by hand.
- **Cause:** UMI's `umi/common/cv_util.py` does `assert json_data['intrinsic_type'] == 'FISHEYE'`
  and reads four Kannala–Brandt coefficients. The `cheng-chi/ORB_SLAM3` fork UMI uses has only
  `Pinhole` and `KannalaBrandt8` camera models. **verified** 2026-09-29. The app's
  `<camera>_double_sphere.json` has the same OpenICC layout but `intrinsic_type: DOUBLE_SPHERE`.
- **Fix:** open. Either calibrate a Kannala–Brandt model with OpenICC (its KB is not OpenCV's, and
  UMI's own 155° calibration is OpenICC `FISHEYE` at 0.29 px), or fit KB8 to the Double Sphere
  model over the observed rays and check agreement by ray projection. Neither exists in the app
  yet. See [umi-and-deployment.md](umi-and-deployment.md).

### ROS image_proc mishandles fisheye intrinsics
- **Detail:** ROS `image_proc` pinhole rectification does not correctly consume equidistant
  fisheye intrinsics (image_pipeline #682).
- **Fix:** undistort with `cv2.fisheye.initUndistortRectifyMap` and
  `estimateNewCameraMatrixForUndistortRectify`, and accept the FOV crop if you need a pinhole
  output. The app's fisheye YAML already builds its projection matrix that way. There is no
  camera_info standard for Double Sphere, so that model has no ROS YAML.

## Bookkeeping

### Run folder names are not ground truth
- **Detail:** the old default camera name `gopro13_hyperview` stamped runs from other cameras, and
  those `gopro13_hyperview_*` folders are not HERO13 data.
- **Fix:** trust `config.json`, not the folder name. The default is now the neutral
  `gopro_camera`, and every preset sets a real name.

### Long solves and builds: judge by artifacts
- **Symptom:** a background job reports success, or goes quiet, and nothing was actually made.
- **Fix:** run solves and Docker builds in the foreground with a bounded timeout, and check the
  files on disk (`out.json`, the image). Background "fire and forget" status pings proved
  unreliable. The app bounds OpenICC with `OPENICC_TIMEOUT_S` and clears stale results before
  each run.

## What we got wrong before

Kept so the same mistakes are not made twice.

| Claim | Status | What is true |
|---|---|---|
| "USB webcam can't use the Max Lens Mod; it's a dead end" (06-10) | wrong | The webcam frames calibrate at 0.62 px with Double Sphere. The failures were OpenCV's solvers plus the layout bug. |
| "The board is verified correct" (06-10) | wrong in detail | The geometry was right but the marker parity was mirrored. |
| "UMI calibrates with Double Sphere" | wrong | UMI's committed intrinsics are OpenICC `FISHEYE` (KB). OpenICC merely offers DS and EUCM. |
| "Drop our intrinsics into UMI's .json and ORB-SLAM3 .yaml" (06-16) | wrong for DS | UMI and the fork accept KB only (see above). |
| "The lens is 177°" | wrong | The usable clean mode is 167°. 177° is Max HyperView (anamorphic). |
| "Pinhole is 30–40% worse, 0.150–0.235 px" | unsupported | It was attributed to the Double Sphere paper's Table 1, which has no pinhole baseline. The paper only says pinhole is suboptimal above 120°. |
| "Gyroflow says SuperView calibration fails unless reversed" | overstated | Gyroflow only says the modes are not recommended and not exactly mapped. The strong claim comes from Joshi (ICRA 2022). |
| "Clean-HDMI-on-boot bug fixed in HERO13 v2.04.70" | wrong version | Fixed in v2.02.70. |
| "Compare recording and webcam by scale-normalised focal length" (06-16 protocol) | flawed | Double Sphere f is not unique. Compare by ray projection. |

## Sources

- Webcam `fov=` WONTFIX: https://github.com/gopro/OpenGoPro/issues/459
- UMI code (`cv_util.py` loader) and intrinsics: https://github.com/real-stanford/universal_manipulation_interface and https://github.com/real-stanford/universal_manipulation_interface/blob/main/example/calibration/gopro_intrinsics_2_7k.json
- ORB-SLAM3 fork used by UMI: https://github.com/cheng-chi/ORB_SLAM3
- `cv2.omnidir` working format: https://github.com/Kazuhito00/OpenCV-CameraCalibration-Example and https://github.com/opencv/opencv/issues/21748
- Joshi et al. (SuperView): https://joshi-bharat.github.io/projects/gopro/
- Gyroflow GoPro notes: https://github.com/gyroflow/docs.gyroflow.xyz/blob/main/getting-started/supported-cameras/gopro.md
