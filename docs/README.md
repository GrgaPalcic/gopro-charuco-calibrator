# Docs

What we learned calibrating wide GoPro lenses for robotics, with the evidence behind it. Start
with the doc that answers your question.

| Doc | Answers |
|---|---|
| [recording-route.md](recording-route.md) | How the From a recording route works: the camera settings and why, the two Labs QR codes and which codes are confirmed on a HERO13, how to record, what the clip check reads from the mp4, why frames are left out, what the run writes, and how UMI loads the files. |
| [lens-modes-and-models.md](lens-modes-and-models.md) | Which GoPro modes can be calibrated, which model fits which field of view, the HERO13 lens mods at 4:3, which capture paths exist (webcam, recording, HDMI, Wi-Fi, Labs), and their limits. |
| [footguns.md](footguns.md) | Why a calibration can look impossible when it isn't. Each trap as symptom → cause → how to tell → fix, plus the claims we had to retract. |
| [measurements.md](measurements.md) | Every result we measured, with date, dataset, board, lens mode, solver and error, including the synthetic check of both OpenICC models at 167°. Also the pending video-vs-webcam comparison, and the place for the first real recording-route clip. |
| [double-sphere-backend.md](double-sphere-backend.md) | How the app drives OpenICC for Double Sphere and Kannala–Brandt, the source patch its image carries, how to read the results, and how to calibrate an on-camera recording. |
| [umi-and-deployment.md](umi-and-deployment.md) | What UMI does, how to load our Kannala–Brandt files into it, the live paths for a robot, and multi-camera sync. |

Tags used throughout:
- **verified:** measured by us, or read from the primary source.
- **inferred:** consistent with the evidence, but not directly demonstrated.
- **unverified:** documented for another camera or mode, or not documented, and not yet tried on
  our cameras.
- **assessed:** established practice, not re-benchmarked here.

These docs replace the June reports (`calibration-report.html` and
`calibration-research-report.md`, both in git history). Their corrections are folded in rather than
appended; the retracted claims are listed at the end of
[footguns.md](footguns.md#what-we-got-wrong-before).

## Glossary

- **Intrinsics:** the numbers that describe a lens: focal length, principal point (image centre)
  and distortion. Calibration measures them.
- **Reprojection error (RMS):** how far, in pixels, the model puts the board's corners from where
  they were detected. Under ~1 px is good; 30–50 px means the model does not fit at all.
- **Pinhole, `plumb_bob`, `rational_polynomial`:** the standard narrow-lens model, with 5 or 8
  distortion coefficients. Fits up to ~90–95°.
- **Kannala–Brandt (KB), `fisheye`, `equidistant`:** the standard fisheye model. OpenCV's version
  has 4 coefficients; "KB8" in ORB-SLAM3 is the same 4-coefficient model with 8 parameters in total
  (fx, fy, cx, cy plus k1–k4). In this app `fisheye` is OpenCV's solve and `kannala_brandt` is
  OpenICC's (`FISHEYE`), the file UMI loads.
- **Double Sphere (DS):** a fisheye model for ~150–195° lenses. Beyond focal length and principal
  point it has two parameters: **xi**, the offset between its two projection spheres, and
  **alpha**, in the range 0–1, which blends between them. f, xi and alpha trade off against each
  other, so compare DS models by projecting rays, not by their numbers.
- **EUCM:** the Enhanced Unified Camera Model, another wide-angle model that OpenICC offers.
- **Ray projection (comparing two models):** take a set of 3D directions (for example every 2° out
  to 70° off-axis), project each through both models, and compare the pixel positions. See
  `gopro_charuco_calibrator/projection.py`.
- **Anamorphic:** stretched unevenly in one direction. GoPro SuperView and HyperView are anamorphic,
  so no lens model fits them.
- **Centre-weighted views:** the views a solver kept after dropping its worst ones. On a very wide
  lens these are mostly near the centre, which makes an unsuitable model look better than it is.
- **Layout parity (caib.io board):** which of the alternating checkerboard cells carry markers. It
  varies with the board's dimensions, and assuming the wrong one ruins the solve.
- **OpenICC:** OpenImuCameraCalibrator, the external tool that solves Double Sphere and
  Kannala–Brandt for this app, and UMI's own KB calibration.
- **GPMF:** GoPro's metadata in the mp4: camera facts (model, serial, lens, stabilisation) and the
  IMU (accelerometer and gyro). The recording route's clip check reads it.
- **GoPro Labs:** GoPro's experimental firmware. It lets a camera take its settings from a QR
  code, which the recording route uses.
- **Lens mod:** extra glass fitted over the camera's lens to widen it: the Max Lens Mod 2.0
  (ADWAL-002) or the Ultra Wide Lens Mod (AEWAL-001) on a HERO13.
- **UVC:** USB Video Class, a standard USB camera. Linux exposes it as `/dev/videoX`.
- **UMI:** the Universal Manipulation Interface, a robot-learning project that collects GoPro
  demonstrations. It is our reference pipeline.
