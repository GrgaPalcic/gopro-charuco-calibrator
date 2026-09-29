# Footguns

The traps that cost us time, each as **symptom → cause → how to tell → fix**. Most of them look
exactly like "this lens cannot be calibrated", and most of them are not. Terms are explained in the
[glossary](README.md#glossary).

## Capture

### SuperView and HyperView cannot be calibrated
- **Symptom:** tens of pixels of RMS whatever model you pick. Our SuperView run gave 27–44 px, and
  fisheye was worse at ~49 px (that run also had the board-layout bug below).
- **Cause:** these modes stretch the sensor into 16:9 unevenly. That is not a lens projection any
  model can describe (Joshi, ICRA 2022; Gyroflow).
- **How to tell:** the image fills the whole 16:9 frame instead of showing a clean fisheye.
  Straight lines bend differently near the left and right edges than near the top and bottom.
  (A heuristic, **inferred**.)
- **Fix:** use Wide for fisheye, Linear for pinhole, and Max SuperView (recording) or webcam Wide
  with the mod fitted for Double Sphere. The UI labels these modes "anamorphic, uncalibratable". The
  old app defaulted to SuperView at 720p, which is how a whole run of these was produced.

### "177°" is the anamorphic mode
- **Symptom:** you pick the Max Lens Mod 2.0's headline mode and get the numbers above.
- **Cause:** 177° is **Max HyperView**, which is anamorphic. The clean fisheye is **Max SuperView,
  167°**.
- **Fix:** never Max HyperView.

### The Max Lens Mod 1.0 does not fit the HERO13
- **Symptom:** a shop lists the 1.0 as HERO13-compatible, and it does not fit well.
- **Cause:** GoPro lists the 1.0 for HERO9–12 only. "HERO13" in some listings is a retailer
  artifact, with poor-fit reports.
- **Fix:** on a HERO13, the Max Lens Mod 2.0.

### The webcam lens request can be ignored
- **Symptom:** the preview does not look like the lens you asked for.
- **Cause:** the app asks for the lens with `fov=` on the webcam start call (legacy gpWebcam or
  Open GoPro), and does not write setting 43. `fov=` is reported unreliable (Open GoPro #459,
  WONTFIX). On a HERO11, Wide and SuperView both looked flat over webcam.
- **How to tell:** after the webcam starts, the readout chips show what the camera reports:
  the **Lens** chip is setting 43 as reported (not the form's **Lens** setting, which is what was
  requested), **Mod** is 189 and **HyperSmooth** is 135. A **Check** chip appears if 43
  differs from the requested lens, or if 189 differs from the requested mod when the preset asks
  for one. HyperSmooth is recorded but not compared. The values are saved as `reported_*` in
  `config.json` and the solve summary. A camera without the Open GoPro API reports nothing.
- **Fix:** trust your eyes and the read-back, not the dropdown. With the mod fitted, the webcam
  Wide image is a circle with black corners.

### Setting 189 changes the conditions, maybe the image
- **Symptom:** none yet. It is an untested difference.
- **Cause:** the Max Lens Mod preset sets setting 189 = 2 (Max Lens 2.0) since 06-16. The June
  reference capture (0.617 px) was taken without it. Whether telling the camera the mod is fitted
  changes the webcam image is not known.
- **Tell:** the **Mod** chip shows what the camera currently has. The app writes 189 only when a
  preset or **Recording settings → Lens mod** asks for a value; otherwise it leaves the camera's
  value alone. The camera most likely keeps the value across power cycles, as it does other
  settings (**inferred**, not tested for 189).
- **Fix:** calibrate with 189 set the way it was when your data was recorded.
  - **For the Ludis dataset that value is unknown.** The dataset predates the read-back (added
    2026-09-29), so its runs have no `reported_*` fields. A run's `config.json` shows only what was
    requested (`gopro.max_lens_mod`).
  - **How to find out:** ask whoever recorded it which preset they used and whether they changed
    the camera's lens-mod menu, and look at the camera's own lens-mod setting. It is the recording
    value only if nobody changed it since.
  - The pending comparison in
    [measurements.md](measurements.md#pending-video-vs-webcam-comparison) tests both ways.

### Stabilisation warps frames
- **Symptom:** a solve that is unstable, or worse than it should be.
- **Cause:** HyperSmooth and horizon lock warp each frame non-rigidly, so the lens model changes
  from frame to frame.
- **Fix:** turn both off. The read-back records `hypersmooth` (setting 135). Treat that as the video
  preset's value; its effect on the webcam stream is unverified.

### Intrinsics belong to one path, one mode, one resolution
- **Symptom:** a calibration that was fine suddenly does not fit the data.
- **Cause:** webcam, recording and HDMI reframe the same optics differently, and changing the lens
  mode or resolution changes the model.
- **Fix:** calibrate exactly the stream the data came from, and deploy in that stream too. Do it
  per camera and mod pair. Recalibrating after refitting a mod is a precaution: that a refit moves
  the model has not been measured. The Ludis dataset is webcam Wide at 1080p with the mod fitted.

### Capture quality
- **Advice:** 20–40 or more good views. Large, crisp markers (a bigger board, or closer). A fast
  shutter (the Labs Max Shutter Angle helps). Good, even light and a matte print.
- **Sweep the whole field:** edges, corners, near and far, and plenty of tilt. Board size and view
  count help, but never fix a wrong mode or model.

## Board

### The caib.io board layout can be mirrored
This was the most expensive bug. It made a good lens look impossible for days.

- **Symptom:** 52–108 px RMS that looks exactly like a hopeless lens, while each view's median
  residual stays small. Whole rows of markers are outliers.
- **Cause:** caib.io puts markers on alternating checkerboard cells. Which parity row 0 starts on
  depends on the board's dimensions (its corner colour). Our 11×8 5X5 board matched the parity the
  code assumed, so it calibrated at 1.12 px and seemed to prove the board builder right. The 10×7
  4X4 board is mirrored: every odd-row marker sat one full square away from where the model put
  it.
- **How it was found:** a per-marker homography check. IDs 5–9, 15–19 and 25–29 (the odd rows) were
  all off by ~75 px, about one square. Testing the parity variants gave 1.9 px for the mirrored one
  against 37 px for the assumed one. An earlier geometry sweep had "verified" the board: 52 px for
  `cols=10, rows=7` against 200–1000 px for any other geometry, highest ID 34. A sweep cannot see a
  parity mirror.
- **How to tell:** tens of pixels of error alongside small per-view medians. Check the residuals per
  marker ID: structured outliers (whole rows or columns) mean a wrong board model, not the lens.
- **Fix (in the app):** `detect_board_layout()` scores both parities against the real detections,
  using a RANSAC homography and the median per-corner residual over the five best views, and keeps
  the better one. The choice is recorded as `board.layout: standard | flipped` in the summary. No
  configuration needed.

### Calibration board and gripper markers share a dictionary
- **Symptom:** outliers whenever a gripper or scene marker is in view, and confused gripper tracking
  whenever the board is visible.
- **Cause:** when the board and the functional markers share a dictionary and ID range, each can be
  read as the other. That produces false 3D↔2D correspondences, the same outlier class as the
  layout bug.
- **Fix:** give calibration boards and gripper markers different dictionary families. The 5X5 board
  with 4X4 grippers never cross-decodes (different bit grids). This is why every preset uses the
  11×8 `DICT_5X5_100` board.

### Board choice
- **assessed** (established ArUco practice, not re-benchmarked here): OpenICC's default board spec
  (`DICT_ARUCO_ORIGINAL`, marker = ½ square) is a tool default, not a best practice. The original
  dictionary has weak inter-marker Hamming distance, and fatter markers (calib.io's ~0.71 ratio)
  carry more pixels and detect better.
- **Why it rarely matters:** the app feeds OpenICC its own detections, so neither constraint
  applies. They matter only for OpenICC's own extractor (next item).

### OpenICC's extractor only reads stock ChArUco boards
- **Symptom:** running OpenICC's `extract_board_to_json` on our frames finds nothing, or solves a
  wrong board.
- **Cause:** it builds an OpenCV ChArUco board with IDs from 0 and marker = ½ square
  (`extract_board_to_json.cc:72`, `board_extractor.cc`). Our 5X5 board starts at ID 2, and the 4X4
  board's markers are 15/21 of a square.
- **Fix:** use the app's solver (it needs neither). For the 4X4 board, build a separate patched
  image (see [double-sphere-backend.md](double-sphere-backend.md#manual-openicc-on-a-recording)).
  The 5X5 board would need the ID offset patched too, which has not been done.

## Solving

### Kannala–Brandt looks better than it is on the Max Lens Mod
- **Symptom:** `fisheye` reports ~1.2 px on the Max Lens Mod circle, close to Double Sphere.
- **Cause:** its frame selection kept ~50 centre-weighted views and dropped the edge ones. OpenCV's
  KB misfits the far periphery, which is exactly where UMI's side mirrors sit. On the 4K recording,
  with edge views included, it fails outright.
- **Fix:** Double Sphere is the model for the Max Lens Mod. The app recommends `double_sphere`
  whenever it solves, and never compares its error against a centre-weighted median. Keep
  `fisheye` as a Docker-free cross-check only.

### Double Sphere parameters are not unique
- **Symptom:** solves of the same data disagree on focal length by up to ~53 px (573 vs 626) at the
  same 0.185–0.186 px RMS.
- **Cause:** f, xi and alpha trade off against each other, and OpenICC's view selection is not
  deterministic. Measured 2026-09-29 over ten runs (see
  [measurements.md](measurements.md#synthetic-double-sphere-through-openicc-2026-09-29)).
- **How to tell:** project the same rays through both models and compare pixels. Across those runs
  they agreed with the truth within 0.73 px out to 70° off-axis, and within 1.40 px out to 80°.
  The last degrees before the ~83.5° lens edge are the least constrained. There is a reference
  projection in `_project_double_sphere` in `tests/test_hero13_readiness.py`.
- **Fix:** compare, test and monitor Double Sphere models by ray projection, never by f, xi or alpha
  alone. That includes any focal comparison between recording and webcam.

### alpha pinned near 1.0 means weak coverage
- **Symptom:** a Double Sphere result with `alpha` at or near 1.0, the top of its 0–1 range.
- **Cause:** the board never reached the curved edge of the circle, so the edge is unconstrained.
  The recording gave 0.82 px with alpha 1.0; the better-spread webcam set gave 0.617 px with alpha
  0.71.
- **Fix:** sweep the whole circle. Even then, the black-corner periphery is never sampled, so the
  model is best where the board went. The app flags alpha ≥ 0.98.

### OpenICC drops views with an absolute 2 px threshold
- **Symptom:** a 4K Double Sphere solve that keeps too few views and dies.
- **Cause:** 2 px at 4K is 1 px at 1080p, so equally good views get purged at higher resolution.
- **Fix (in the app):** coordinates are divided down to ≤1080p before the solve, and focal length,
  principal point and error are scaled back afterwards. This is an exact change of units. Corners
  are still detected at full resolution, no image is resampled, xi and alpha are scale-free, and
  `solve_downsample_factor` is recorded in the artifact. OpenICC's own `--downsample_factor` gets
  the same effect on the threshold differently: it shrinks the image before detection, which does
  lose precision.

### `cv2.fisheye` needs coaxing
- **Flags:** use `CALIB_RECOMPUTE_EXTRINSIC | CALIB_FIX_SKEW | CALIB_CHECK_COND`, with object points
  shaped `(N,1,3)`.
- **Errors:** on "Ill-conditioned matrix for input array N", or `InitExtrinsics: fabs(norm_u1) > 0`,
  drop that view instead of disabling the check. The app does this automatically.

### `cv2.omnidir` is unusable here
- **To run it at all:** pass a list of variable-length `(N,1,3)` float64 C-contiguous arrays with
  `K=None, xi=None, D=None`. Equal-shape lists get coerced into one Mat and assert at
  `omnidir.cpp:854`.
- **Even then:** it diverged (RMS 10⁵⁶) or kept 1 of 18 views on our data. That is OpenCV's binding
  and optimiser, not the Mei model. Use a purpose-built Double Sphere tool.

## Downstream

### A Double Sphere result does not drop into UMI
- **Symptom:** UMI's loader fails, or you are tempted to convert the numbers by hand.
- **Cause:** **verified** 2026-09-29.
  - UMI's `umi/common/cv_util.py` does `assert json_data['intrinsic_type'] == 'FISHEYE'` and reads
    four Kannala–Brandt coefficients.
  - The `cheng-chi/ORB_SLAM3` fork UMI uses has only `Pinhole` and `KannalaBrandt8` camera models.
  - The app's `<camera>_double_sphere.json` has the same OpenICC layout but
    `intrinsic_type: DOUBLE_SPHERE`.
- **Fix:** open. Two routes, both untested at 167°:
  - calibrate a Kannala–Brandt model with OpenICC (UMI's own 155° calibration is OpenICC `FISHEYE`
    at 0.29 px);
  - fit KB to the Double Sphere model over the observed rays.

  Either way, check the result by ray projection against Double Sphere, and remember that UMI's
  code may evaluate KB through OpenCV, with OpenCV's limits. See
  [umi-and-deployment.md](umi-and-deployment.md#where-our-calibration-differs).

### ROS image_proc mishandles fisheye intrinsics
- **Cause:** ROS `image_proc` pinhole rectification does not correctly consume equidistant fisheye
  intrinsics (image_pipeline #682).
- **Fix:** undistort with `cv2.fisheye.initUndistortRectifyMap` and
  `estimateNewCameraMatrixForUndistortRectify`, and accept the FOV crop if you need a pinhole
  output. The app's fisheye YAML already builds its projection matrix that way.
- **Double Sphere:** there is no camera_info standard for it, so it gets no ROS YAML.

## Bookkeeping

### Run folder names are not ground truth
- **Cause:** the old default camera name `gopro13_hyperview` stamped runs from other cameras, so
  those `gopro13_hyperview_*` folders are not HERO13 data.
- **Fix:** trust `config.json`, not the folder name. The default is now the neutral `gopro_camera`,
  and every preset sets a real name.

### Long solves and builds: judge by artifacts
- **Symptom:** a background job reports success, or goes quiet, and nothing was actually made.
- **Fix:** run solves and Docker builds in the foreground with a bounded timeout, and check the
  files on disk (`out.json`, the image). Background "fire and forget" status pings proved
  unreliable. The app bounds OpenICC with `OPENICC_TIMEOUT_S` and clears stale results before each
  run.

## What we got wrong before

Kept so the same mistakes are not made twice.

| Claim | Status | What is true |
|---|---|---|
| "USB webcam can't use the Max Lens Mod; it's a dead end" (06-10) | wrong | The webcam frames calibrate at 0.617 px with Double Sphere. The failures were OpenCV's solvers plus the layout bug. |
| "The board is verified correct" (06-10) | wrong in detail | The geometry was right, but the marker parity was mirrored. |
| "The user does not own a GoPro 13" (06-10) | wrong | The 06-10 gripper run is HERO13 + Max Lens Mod 2.0. Only the `gopro13_hyperview_*` folders were mislabelled. |
| "UMI calibrates with Double Sphere" | wrong | UMI's committed intrinsics are OpenICC `FISHEYE` (KB). OpenICC merely offers DS and EUCM. |
| "Drop our intrinsics into UMI's .json and ORB-SLAM3 .yaml" (by 06-16) | wrong for Double Sphere | UMI and the fork accept KB only (see above). |
| "The lens is 177°" | wrong | The usable clean mode is 167°. 177° is Max HyperView, which is anamorphic. |
| "Pinhole is 30–40% worse, 0.150–0.235 px" | unsupported | Attributed to the Double Sphere paper's Table 1, which has no pinhole baseline. The paper only says pinhole is suboptimal above 120°. |
| "Gyroflow says SuperView calibration fails unless reversed" | overstated | Gyroflow only says the modes are not recommended and not exactly mapped. The strong claim is Joshi's (ICRA 2022). |
| "Clean-HDMI-on-boot bug fixed in HERO13 v2.04.70" | wrong version | It was fixed in v2.02.70. |
| "Compare recording and webcam by scale-normalised focal length" (June protocol) | flawed | Double Sphere f is not unique. Compare by ray projection. |

## Sources

- Webcam `fov=` WONTFIX: https://github.com/gopro/OpenGoPro/issues/459
- UMI code (`cv_util.py` loader) and intrinsics: https://github.com/real-stanford/universal_manipulation_interface and https://github.com/real-stanford/universal_manipulation_interface/blob/main/example/calibration/gopro_intrinsics_2_7k.json
- The ORB-SLAM3 fork used by UMI: https://github.com/cheng-chi/ORB_SLAM3
- `cv2.omnidir` working format: https://github.com/Kazuhito00/OpenCV-CameraCalibration-Example and https://github.com/opencv/opencv/issues/21748
- OpenICC extractor source: https://github.com/urbste/OpenImuCameraCalibrator
- Joshi et al. (SuperView): https://joshi-bharat.github.io/projects/gopro/
- Gyroflow GoPro notes: https://github.com/gyroflow/docs.gyroflow.xyz/blob/main/getting-started/supported-cameras/gopro.md
- Max Lens Mod 1.0 compatibility: https://gopro.com/en/us/shop/mounts-accessories/max-lens-mod/ADWAL-001.html
