# UMI, deployment and multi-camera recording

How the reference pipeline (UMI, the Universal Manipulation Interface) uses a GoPro, what that
means for our calibrations, and the hardware paths for a live robot feed.

## What UMI does

UMI is the closest precedent, so its choices are the benchmark. All points below are
**verified** from the UMI paper and repo unless marked otherwise.

- **Camera:** a wrist-mounted GoPro HERO9 with the original 155° Max Lens Mod 1.0, running GoPro
  Labs firmware.
- **Intrinsics:** Kannala–Brandt. The committed `gopro_intrinsics_2_7k.json` has
  `intrinsic_type: FISHEYE`, four coefficients and 0.29 px, and was calibrated with
  OpenImuCameraCalibrator.
- **Pose labels:** an ORB-SLAM3 fork (inertial-monocular) recovers the gripper's trajectory from
  the fisheye video plus the GoPro IMU embedded in the mp4 (GPMF).
- **Policy input:** raw fisheye, not rectified. Rectifying 155° to pinhole wrecks the edges. The
  physical side mirrors add implicit stereo.
- **Data collection vs deployment:** data collection records mp4, processed offline. Deployment is
  live over HDMI capture. The handheld demo device has no live link.
- **FastUMI:** a follow-up that keeps the GoPro fisheye image but tracks pose with a RealSense
  T265, calling the GoPro VIO pipeline fragile. **inferred**: it uses HDMI capture, based on its
  Elgato HD60 X.

UMI is lens-agnostic once you recalibrate (issue #41). You don't need 155°; you need a clean wide
fisheye and identical capture settings for training and deployment.

## Where our calibration differs

The Ludis dataset was recorded from the **HERO13 USB webcam stream at 1080p, Wide, with the Max
Lens Mod 2.0 fitted**. That is a ~167° circular fisheye, past what OpenCV's Kannala–Brandt fits,
so the app's primary model is **Double Sphere**.

**A Double Sphere result does not drop into UMI's pipeline.** **verified** 2026-09-29:
- UMI's loader `umi/common/cv_util.py` does `assert json_data['intrinsic_type'] == 'FISHEYE'` and
  reads four KB coefficients.
- The `cheng-chi/ORB_SLAM3` fork ships only `Pinhole` and `KannalaBrandt8` camera models.

The app's `<camera>_double_sphere.json` uses OpenICC's layout but says
`intrinsic_type: DOUBLE_SPHERE`. There are two ways forward, and neither is built yet:

1. **Calibrate KB with OpenICC:** `--camera_model_to_calibrate=FISHEYE` (see
   [double-sphere-backend.md](double-sphere-backend.md#manual-openicc-on-a-recording)). OpenICC's
   KB is not OpenCV's, and UMI's own 155° calibration came from it. Whether it holds up at 167°
   on our data is untested.
2. **Fit KB8 to the Double Sphere model:** fit over the rays the board actually covered, then
   check agreement by ray projection.

Either way, judge the result by ray-projection agreement with the Double Sphere model, not by
focal length.

## Live deployment: HDMI capture

For a live wide-lens feed at the robot, UMI converts the GoPro's HDMI into a normal USB video
device:

| Piece | What it does | Without it |
|---|---|---|
| GoPro **Media Mod** | micro-HDMI output | no HDMI |
| GoPro **Labs** firmware with `HDMI=1` (persistent with a `*` prefix) | clean output with no overlays | overlays and resets, unusable as a feed |
| **UVC capture card** (Elgato HD60 X, or a cheap MS2109) | turns HDMI into `/dev/videoX` | the signal never reaches the computer |

UMI's chain is Media Mod → Elgato HD60 X → UVC, opened with
`cv2.VideoCapture(path, cv2.CAP_V4L2)`. It then applies a fisheye conversion and a resize to
224×224, at 3840×2160@30 on a Cam Link, otherwise 1920×1080@60.

This app reads a plain V4L2 device when GoPro auto-setup is off, so a capture card works as a
calibration source unchanged. Calibrate through the card if that is how the robot will see.

Hardware caveats:
- The **Elgato Cam Link 4K** is UVC but advertises formats it doesn't support. OpenCV fails to open
  it without a v4l2loopback and ffmpeg workaround. Prefer the HD60 X or an MS2109.
- **MS2109** grabbers can need a udev rule to unbind `snd-usb-audio` and bind `uvcvideo`, and image
  quality varies.
- **HERO13 clean HDMI on boot** failed if HDMI was connected before power-on. Fixed in firmware
  v2.02.70; on older firmware, plug HDMI in after boot.
- **inferred:** no primary source shows a HERO12 or 13 giving clean HDMI with the Max Lens Mod and
  zero vignette. The claim rests on the mechanism, and "Labs is required for persistent clean
  HDMI" is consistent with UMI's README rather than stated there.

## Multi-camera sync

For UMI-style recordings from several GoPros (mp4 with GPMF):

- **Primary: clap cross-correlation.** One sharp clap on set, then cross-correlate the 48 kHz audio
  tracks (FFT). This gives sub-millisecond relative offsets with no special firmware.
- **Optional absolute anchor:** the GoPro Labs time and date QR code, to pair footage with robot or
  host logs. The QR encodes absolute UTC, so cameras do not need to scan it at the same moment;
  the residual is each camera's scan-to-set latency (ms-level, unverified), which is why the clap
  stays primary. Filming an on-screen millisecond clock works without Labs. Outdoors, Labs GPS
  time sync is an alternative.
- **Live webcam streams into one host** are timestamped on arrival by one clock. There is no
  cross-camera offset, and jitter is about one frame period.
- **BLE record start and stop** gets cameras rolling roughly together, but not frame-accurately.
- **Useful from Labs:** Max Shutter Angle (crisp corners), White Balance Lock, Wake on Power (the
  camera comes up with the robot).

**Rehearse once on real hardware.** Two cameras film the time QR, then a clap, and record for 2–3
minutes. Cross-correlating the audio gives the offset and drift, and that number validates the
recipe. The result has a row in [measurements.md](measurements.md#pending-video-vs-webcam-comparison).

## Sources

- UMI paper: https://arxiv.org/html/2402.10329v3 and https://umi-gripper.github.io/umi.pdf
- UMI code and intrinsics: https://github.com/real-stanford/universal_manipulation_interface and https://github.com/real-stanford/universal_manipulation_interface/blob/main/example/calibration/gopro_intrinsics_2_7k.json
- ORB-SLAM3 fork: https://github.com/cheng-chi/ORB_SLAM3
- FastUMI: https://arxiv.org/html/2409.19499v1
- OpenImuCameraCalibrator: https://github.com/urbste/OpenImuCameraCalibrator
- GoPro Labs (HDMI, time QR, shutter, WB lock): https://gopro.github.io/labs/ and https://gopro.github.io/labs/control/extensions
- MS2109 on Linux: https://mjt.me.uk/posts/macrosilicon-ms2109/
- Cam Link 4K workaround: https://github.com/AdamGleave/elgato-camlink-workaround
