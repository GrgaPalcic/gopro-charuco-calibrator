# GoPro Calibration — Issues & Research Findings

_Session date: 2026-06-10. Scope: why GoPro calibration was failing in this tool, what the
robotics/UMI literature actually does, and the camera-model / capture-interface decisions
that follow._

---

## TL;DR

- **The tool was pinhole-only.** It now also fits a **fisheye (Kannala–Brandt)** model, plus
  safer defaults and per-role presets. Good enough for lenses up to ~130°.
- **SuperView/HyperView cannot be calibrated by any standard model** — they are non-uniform
  anamorphic *stretches*, not radially-symmetric projections. A real run captured at SuperView
  gave **~27–44 px RMS** under pinhole.
- **The Max Lens Mod over USB webcam is a dead end.** Webcam mode can't apply Max-Lens
  processing, so you get a **~177° circular fisheye with black-corner vignette** that **no
  OpenCV model calibrates** (pinhole 52 px, `cv2.fisheye` won't converge, `cv2.omnidir`
  diverges — all confirmed on the user's real frames). This is why results came back `n/a`.
- **UMI never uses USB webcam.** It runs the GoPro through **Media Mod → clean HDMI → a UVC
  capture card → `/dev/videoX`** (live, for deployment) and calibrates the wide fisheye with a
  **Double Sphere** model via OpenImuCameraCalibrator (offline, on recorded video).
- **Decision still open:** for the wide UMI-style cams you need **HDMI capture + Double Sphere**;
  `cv2.fisheye` + webcam only suffices for narrower (≤~130°) cams like a chest cam.

---

## 1. Issues encountered (in order)

1. **HERO13 looked heavily fisheye, HERO11 looked flat; RMS "huge".** Root cause: the solver
   fit only a pinhole model, which cannot represent a wide fisheye; and HERO11's USB-webcam
   FOV is effectively narrow/flat (legacy bridge), so pinhole fit it fine (~1–2 px).
2. **Calibrator was pinhole-only.** `cv2.aruco.calibrateCameraAruco` → `plumb_bob` (5-coeff) and
   `rational_polynomial` (8-coeff). No fisheye path existed.
3. **Unsafe defaults → ~30 px garbage.** A real run (`gopro13_hyperview_20260610_113917`) used
   the defaults `webcam_fov=3` (SuperView) + `webcam_resolution=7` (720p) → pinhole RMS
   26.97 px (selected) / 44.11 px (all frames). SuperView is uncalibratable.
4. **Mislabeled runs.** `CameraConfig.camera_name` defaulted to `"gopro13_hyperview"`, so
   non-GoPro-13 runs were stamped with that name. **The user does not own a GoPro 13** — the
   `gopro13_hyperview_*` run dirs are mislabeled. Run-dir/camera-name labels are not ground truth.
5. **`n/a` results.** The new fisheye-only gripper preset run (`gopro13_umi_gripper_20260610_140106`,
   Wide + 1080p + Max Lens Mod 2.0) produced **no summary** → UI shows `n/a`. The fisheye solve
   *raised* `"could not converge"` and wrote nothing. (Also a UX bug: the error wasn't surfaced.)
6. **Vignette over webcam with the Max Lens Mod.** The streamed image is a ~177° circular
   fisheye with black corners — the symptom of webcam mode not applying Max-Lens processing.

---

## 2. Knowledge gathered (research, with confidence)

> Confidence tags reflect how directly each claim was verified against primary sources in this
> session. "High" = confirmed from the primary artifact; "Med" = inference/secondary or a
> primary source that couldn't be fully fetched.

### 2.1 GoPro digital lenses & FOV (High)
- **Linear ≈ 90°** — a firmware *rectilinear* de-fish; the only mode safe to treat as pinhole.
  Rectilinear cannot be made wide (edges diverge near 120°), so Linear ≈ 90–110° regardless of
  any lens in front of it.
- **Wide ≈ 120–130°** — native fisheye/barrel; a *true fisheye*, not pinhole-modelable.
- **SuperView ≈ 170°, HyperView wider** — **non-uniform anamorphic stretches** of the 4:3 / 8:7
  sensor to 16:9 via a proprietary equation. **Not radially symmetric → uncalibratable by any
  standard pinhole or fisheye model.** Joshi (ICRA 2022) states SuperView "generates non-linear
  distortions that thwart the calibration of the camera's intrinsic parameters."

### 2.2 Which camera model for which FOV (High)
- **Pinhole radtan/plumb_bob:** valid only to ~90–95° (MathWorks; Tangram MetriCal). Hard
  geometric limit: pinhole can't map rays ≥90° off-axis.
- **Rational (8-coeff):** extends pinhole modestly; still bounded by the 90° limit; does **not**
  rescue ~130° Wide.
- **Kannala–Brandt fisheye (`cv2.fisheye`):** good to ~140–150°. OpenCV implements it as
  *pinhole-projection-then-distortion*, which has a **hard ~180° wall** — it degrades badly
  approaching 180° and cannot represent ≥180°. (The KB *model* can exceed 180°; OpenCV's/Kalibr's
  *implementation* cannot.)
- **Double Sphere / EUCM / omnidir (Scaramuzza/Mei):** the correct models for ~150–195°. Double
  Sphere has a closed-form inverse and no z=0 singularity. Within <1% of KB-8 accuracy and faster.
- Double Sphere paper: pinhole is "suboptimal for FOV > 120°"; tested lenses 122–195° incl. a
  150° GoPro lens.

### 2.3 UMI's actual pipeline (High)
- **Camera:** wrist-mounted **GoPro HERO 9** + **original 155° Max Lens Mod (ADWAL-001)** — a
  *physical* lens, not a digital FOV mode. GoPro Labs firmware.
- **Intrinsics:** the committed `gopro_intrinsics_2_7k.json` is **`intrinsic_type: FISHEYE`
  (Kannala–Brandt, 4 coeffs, 0.29 px reproj)**, calibrated with **OpenImuCameraCalibrator**
  (which also offers Double Sphere / EUCM / Division-Undistortion). _(A research agent initially
  claimed "Double Sphere"; the committed artifact is KB fisheye — corrected.)_
- **Pose / action labels:** an **ORB-SLAM3 fork** (cheng-chi/ORB_SLAM3), inertial-monocular,
  using the GoPro's **GPMF IMU** (accel/gyro embedded in the recorded mp4).
- **Data collection = recorded mp4** (offline SLAM). **Deployment = live**, see 2.5.
- The policy consumes **raw fisheye** (no rectification); rectifying a 155° lens to pinhole
  produces extreme edge distortion unsuitable for learning.

### 2.4 Max Lens Mod 1.0 vs 2.0 (High)
| | Fits | FOV | Notes |
|---|---|---|---|
| **MLM 1.0** (ADWAL-001) | HERO 9/10/11/12 | **155°** | what UMI uses; max 2.7K60 |
| **MLM 2.0** (ADWAL-002) | **HERO 12/13 only** | **up to 177°** (Max HyperView) | the user's mod; 4K60 |

- The two are **not optically interchangeable** (different FOV → different intrinsics).
- **MLM 2.0 does not fit a HERO 11.** (Relevant if a HERO11 is in the fleet.)
- The 177°/167° "Max HyperView"/"Max SuperView" modes are **recording-mode** features.

### 2.5 Webcam vs HDMI capture — the pivotal finding (High for UMI path; Med for the HERO12/13 vignette specifics)
- **USB webcam mode is locked to the camera's native digital lens** (Wide/Narrow/SuperView/
  Linear), ≤1080p, and **cannot apply Max-Lens-Mod processing or be told a mod is attached.**
  The mod's circular image therefore doesn't fill the native-Wide frame → **black-corner
  vignette. Not fixable in webcam mode** (GoPro Labs lens-mod commands affect capture/preview/
  HDMI, not the USB webcam UVC path).
- The Open GoPro webcam FOV enum is literally `WIDE=0, NARROW=2, SUPERVIEW=3, LINEAR=4` — there
  is no "Max" value. The `fov=` request is also unreliable on HERO11/12/13 (Open GoPro issue
  #459, WONTFIX) — verify the streamed FOV empirically.
- **UMI deployment path (verified from the paper + code):**
  GoPro → **Media Mod 1.0 (USB-C→HDMI)** → **Elgato HD60 X (HDMI→USB-3 UVC)** → `/dev/videoX`,
  opened with `cv2.VideoCapture(path, cv2.CAP_V4L2)` (`uvc_camera.py`), 1080p60 or 4K30, then
  `fisheye_converter` + resize to 224×224. Clean HDMI requires **GoPro Labs firmware**
  (`HDMI=1` / `MHDMI1` QR, persistent with `*` prefix; Media Mod for the HDMI port).
- **Clean HDMI mirrors the record/preview pipeline, which DOES apply Max-Lens processing** →
  full wide image, no mismatch vignette. A generic **MS2109** HDMI grabber appears as a normal
  in-kernel `uvcvideo` `/dev/video0`; the **Elgato Cam Link 4K** advertises bad formats and
  needs a v4l2loopback+ffmpeg workaround, but the **HD60 X** (what UMI names) opens directly.
- **Consequence for this tool:** it reads `/dev/videoX`, so an HDMI capture card works
  **unchanged** as the capture source — and it's *live*, so the same path serves calibration
  and deployment.

### 2.6 `cv2.fisheye` / `cv2.omnidir` empirical limits (High — tested on the user's frames)
- On the real ~177° MLM-over-webcam frames (board verified correct, see §3):
  - **pinhole:** 52 px RMS (can't model the distortion).
  - **`cv2.fisheye` (KB):** raises / "could not converge" (past its ~180° wall).
  - **`cv2.omnidir` (Mei):** 48 px and/or numerically diverges; keeps only ~10/103 frames.
- Conclusion: **no OpenCV model reliably calibrates a ~177° circular fisheye.** That regime
  needs Double Sphere/EUCM (Kalibr or OpenImuCameraCalibrator), which OpenCV does not provide
  built-in. This matches why UMI uses OpenImuCameraCalibrator, not `cv2.fisheye`.

---

## 3. Diagnostics on the user's real data

- **`gopro13_hyperview_20260610_113917`** (SuperView, 720p, DICT_4X4_50 10×7): reproduced the
  original result exactly — plumb_bob 26.97/44.11 px, rational 26.77/43.72 px. SuperView is the
  cause; fisheye on the same data was *worse* (~49 px). Confirms SuperView is uncalibratable.
- **`gopro13_umi_gripper_20260610_140106`** (Wide, 1080p, MLM 2.0, `solver.models=[fisheye]`):
  108 frames, **no summary written** → fisheye solve raised "could not converge" → `n/a`.
  - **Board geometry verified correct.** A sweep of caib geometries: `cols=10, rows=7` fits at
    52 px vs 200–1000 px for every other geometry; max detected marker ID = 34 = exactly 35
    markers. So the board config was *not* the problem.
  - All three models fail (§2.6) → it's the ~177° lens, not the board or a bug.
- **Sanity (narrow Wide):** `cv2.fisheye` on a flat/narrow Wide capture gave **1.12 px** with a
  clean 4-coeff equidistant model — proves the new fisheye path is correct and works in range.
- **Board size:** 10 squares @ 21 mm is small; the user's HERO11 board (34 mm, 11×8) gave
  sub-pixel. Board size is **secondary** (the 177° lens dominates), but a bigger board helps any
  wide calibration converge — worth switching to regardless.

---

## 4. Code changes made this session

- **Fisheye solver** (`solver.py`): `fisheye_solve` via `cv2.fisheye` (Kannala–Brandt), flags
  `RECOMPUTE_EXTRINSIC | FIX_SKEW | CHECK_COND`, object points `(N,1,3)`, iteratively drops
  ill-conditioned/degenerate views and raises a clear error if it can't converge (instead of a
  raw OpenCV assertion). `CalibModelSpec` + `CALIB_MODELS` registry; `solve_model` dispatch.
- **Model selection** (`models.py`): `SolverConfig.models: list[...]`; `solve_from_frames` loops
  over it. JSON-safe summaries (dropped views → `null`, finite-only median/worst).
- **YAML** (`ros_yaml.py`): fisheye writes `distortion_model: equidistant` and a fisheye-aware
  projection matrix (`estimateNewCameraMatrixForUndistortRectify`), not the pinhole
  `getOptimalNewCameraMatrix`.
- **Frontend** (`index.html`/`app.js`): "Calibration model" dropdown (pinhole / fisheye / both),
  mapped to `solver.models` (so a preset's model choice survives `readForm`).
- **Safer defaults** (`models.py`): `webcam_fov 3→0` (Wide), `webcam_resolution 7→12` (1080p),
  `camera_name "gopro13_hyperview"→"gopro_camera"`, `solver.models → [plumb_bob, rational, fisheye]`
  ("Both"). Three `test_gopro` tests made explicit about resolution so they don't depend on defaults.
- **Presets:** removed `gopro13_wide_1080p` / `gopro13_linear_1080p`; added
  `gopro13_umi_gripper_fisheye_1080p`, `gopro13_central_chest_fisheye_1080p`,
  `gopro13_central_linear_pinhole_1080p`; kept `gopro11_wide_1080p`.
- All 20 tests pass; ruff clean. **Nothing committed** — changes are in the working tree.

### Still TODO
- **Surface solve errors in the UI** (the silent `n/a` bug).
- **Double Sphere / EUCM** model (or import OpenImuCameraCalibrator output) for ≥155° fisheye —
  required for the wide MLM cams; not yet implemented.

---

## 5. Open decisions & recommendations

Two **independent** knobs:

1. **Capture interface.** USB webcam ❌ (can't do the Max Lens Mod; vignette) → **HDMI →
   capture card** ✅ (clean wide image, live, reads as `/dev/videoX`, = UMI's setup). Needs
   Media Mod + GoPro Labs clean HDMI + a UVC capture card.
2. **Calibration model by FOV.**
   - Linear (~90°) → pinhole.
   - Wide (~130°) → `cv2.fisheye` (already shipped).
   - 155–177° (Max Lens Mod) → **Double Sphere/EUCM** (not yet in the tool; UMI uses
     OpenImuCameraCalibrator).

**Recommendation:** wide UMI-style gripper cams → HDMI capture + Double Sphere (match UMI);
chest/scene cam that doesn't need 155° → webcam Wide + `cv2.fisheye` is fine. "Narrow to Linear"
does **not** preserve the wide FOV (~90–110° rectilinear), so it's only for cams where wide FOV
is not required.

**Hard prerequisite for the wide cams:** a **Media Mod + capture card**. Without HDMI capture,
the Max-Lens-Mod cameras cannot be calibrated or deployed properly, regardless of model.

---

## 6. Key sources

- UMI paper — https://arxiv.org/html/2402.10329v3 ; PDF https://umi-gripper.github.io/umi.pdf
- UMI repo (README, `umi/real_world/uvc_camera.py`, `bimanual_umi_env.py`) —
  https://github.com/real-stanford/universal_manipulation_interface
- UMI intrinsics (KB fisheye) —
  https://raw.githubusercontent.com/real-stanford/universal_manipulation_interface/main/example/calibration/gopro_intrinsics_2_7k.json
- OpenImuCameraCalibrator — https://github.com/urbste/OpenImuCameraCalibrator
- ORB-SLAM3 fork — https://github.com/cheng-chi/ORB_SLAM3
- Double Sphere paper — https://arxiv.org/abs/1807.08957
- MathWorks fisheye basics — https://www.mathworks.com/help/vision/ug/fisheye-calibration-basics.html
- Tangram MetriCal camera models — https://docs.tangramvision.com/metrical/14.1/calibration_models/cameras/
- Joshi GoPro VI-SLAM (SuperView warning) — https://joshi-bharat.github.io/projects/gopro/ ; https://arxiv.org/pdf/2203.05640
- Gyroflow GoPro lens notes — https://github.com/gyroflow/docs.gyroflow.xyz/blob/main/getting-started/supported-cameras/gopro.md
- Kalibr supported models — https://github.com/ethz-asl/kalibr/wiki/supported-models
- Open GoPro SDK (WebcamFOV / http_commands) — https://github.com/gopro/OpenGoPro
- Open GoPro webcam `fov=` WONTFIX — https://github.com/gopro/OpenGoPro/issues/459
- Max Lens Mod 2.0 (177°, HERO12/13) — https://gopro.com/en/us/shop/mounts-accessories/max-lens-mod-2/ADWAL-002.html
- Max Lens Mod 1.0 (155°) — https://gopro.com/en/us/shop/mounts-accessories/max-lens-mod/ADWAL-001.html
- GoPro Labs clean-HDMI control — https://gopro.github.io/labs/control/extensions
- FastUMI (T265 for pose, GoPro via HDMI) — https://arxiv.org/html/2409.19499v1
- MS2109 grabber on Linux — https://mjt.me.uk/posts/macrosilicon-ms2109/
- Elgato Cam Link 4K Linux workaround — https://github.com/AdamGleave/elgato-camlink-workaround
