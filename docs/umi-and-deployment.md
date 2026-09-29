# UMI, deployment and multi-camera recording

How the reference pipeline (UMI, the Universal Manipulation Interface) uses a GoPro, what that
means for our calibrations, which live paths exist for a robot, and how to sync several cameras.
Terms are explained in the [glossary](README.md#glossary).

## What UMI does

UMI is the closest precedent, so its choices are the benchmark. Everything below is **verified**
from the UMI paper and repo unless marked otherwise.

- **Camera:** a wrist-mounted GoPro HERO9 with the original 155° Max Lens Mod 1.0, running GoPro
  Labs firmware.
- **Intrinsics:** Kannala–Brandt. The committed `gopro_intrinsics_2_7k.json` has
  `intrinsic_type: FISHEYE`, four coefficients and 0.29 px, and was calibrated with
  OpenImuCameraCalibrator.
- **Pose labels:** an ORB-SLAM3 fork (inertial-monocular) recovers the gripper's trajectory from the
  fisheye video plus the GoPro's IMU, which is embedded in the mp4 as GPMF.
- **Policy input:** raw fisheye, not rectified. Rectifying 155° to pinhole wrecks the edges. The
  physical side mirrors add implicit stereo.
- **Data and deployment paths:** data collection records mp4, processed offline. Deployment is
  live, over HDMI capture. The handheld demo device has no live link.
- **FastUMI,** a follow-up, keeps the GoPro fisheye image but tracks pose with a RealSense T265,
  calling the GoPro VIO pipeline fragile. **inferred:** it uses HDMI capture, based on its Elgato
  HD60 X.

UMI is lens-agnostic once you recalibrate (issue #41). What matters is a clean wide fisheye, and
identical capture settings for training and deployment, not UMI's exact 155°.

## Where our calibration differs

The Ludis dataset was recorded from the **HERO13 USB webcam stream: 1080p, Wide, with the Max Lens
Mod 2.0 fitted**. That gives a circular fisheye with black corners. It is nominally the 167° lens;
the webcam image's exact field of view has not been measured. The app's primary model for it is
**Double Sphere**:
- OpenCV's Kannala–Brandt solved these frames at 1.19 px, but only over centre-weighted views.
- It failed on the 4K recording once edge views were included.
- It misfits the far periphery, which is where UMI's side mirrors sit.

**A Double Sphere result does not drop into UMI's pipeline.** **verified** 2026-09-29:
- UMI's loader `umi/common/cv_util.py` does `assert json_data['intrinsic_type'] == 'FISHEYE'` and
  reads four KB coefficients.
- The `cheng-chi/ORB_SLAM3` fork ships only `Pinhole` and `KannalaBrandt8` camera models.
- The app's `<camera>_double_sphere.json` uses OpenICC's layout but says
  `intrinsic_type: DOUBLE_SPHERE`.

Two ways forward. Neither is built, and neither is tested at 167°:

1. **Calibrate KB with OpenICC:** `--camera_model_to_calibrate=FISHEYE` (see
   [double-sphere-backend.md](double-sphere-backend.md#manual-openicc-on-a-recording)). UMI's own
   155° calibration came from it. Whether OpenICC's KB fit holds at 167° is unknown, and UMI's code
   may evaluate the model through OpenCV's fisheye functions, with their limits.
2. **Fit KB to the Double Sphere model** over the rays the board actually covered.

Either way, judge the KB result by ray-projection agreement with the Double Sphere model, not by
focal length.

## Live deployment

**Our live path is the USB webcam.** It is what the app captures and what the Ludis dataset was
recorded from, so a robot reading the same webcam stream sees the same image the policy was
trained on, with the same intrinsics. Timestamps come from one host clock, and the webcam
calibrates at 0.6–1.1 px.

**HDMI capture is UMI's path.** It is the robust option for a live feed: a stable latency, and no
overlays once Labs clean HDMI is on. But it is a **different image** from the webcam stream. It
needs its own calibration and, for a learned policy, training data captured the same way. Don't
mix the two.

| Piece | What it does | Without it |
|---|---|---|
| GoPro **Media Mod** | micro-HDMI output | no HDMI |
| GoPro **Labs** firmware, extension `HDMI=1` (QR command `oMHDMI=1`; a leading `*` makes it persist) | clean output with no overlays | overlays and resets, unusable as a feed |
| **UVC capture card** (Elgato HD60 X, or a cheap MS2109) | turns HDMI into `/dev/videoX` | the signal never reaches the computer |

UMI's chain:
- **Hardware:** GoPro → Media Mod 1.0 → Elgato HD60 X → UVC.
- **Opening the card:** `uvc_camera.py` / `bimanual_umi_env.py` open it with
  `cv2.VideoCapture(path, cv2.CAP_V4L2)`. They request 3840×2160@30 when the device path contains
  `Cam_Link_4K`, and 1920×1080@60 otherwise.
- **Processing:** each frame goes through a fisheye conversion and is resized to 224×224 for the
  policy.
- **Unknown:** what resolution the GoPro itself sends over HDMI.

This app reads a plain V4L2 device when automatic GoPro setup is off, so a capture card works as a
calibration source unchanged. Calibrate through the card if that is how the robot will see.

Hardware caveats:
- **Elgato Cam Link 4K:** UVC, but it advertises formats it doesn't support. OpenCV fails to open it
  without a v4l2loopback and ffmpeg workaround. Prefer the HD60 X or an MS2109.
- **MS2109 grabbers:** in-kernel `uvcvideo` (MJPEG or YUYV, ~1080p). Some need a udev rule to unbind
  `snd-usb-audio` and bind `uvcvideo`, and image quality varies.
- **HERO13 clean HDMI on boot** failed if HDMI was connected before power-on. Fixed in firmware
  v2.02.70; on older firmware, plug HDMI in after boot.
- **inferred:** no primary source shows a HERO12 or 13 giving clean HDMI with the Max Lens Mod and
  zero vignette. That claim rests on the mechanism. "Labs is required for persistent clean HDMI" is
  consistent with UMI's README, not stated there.

## Multi-camera sync

For UMI-style recordings from several GoPros (mp4 with GPMF):

- **Primary: clap cross-correlation.** One sharp clap on set, then cross-correlate the 48 kHz audio
  tracks with an FFT. This gives sub-millisecond relative offsets, with no special firmware.
- **Optional absolute anchor:** the GoPro Labs time and date QR code, to pair footage with robot or
  host logs.
  - The QR encodes absolute UTC, so the cameras don't need to scan it at the same moment.
  - What remains is each camera's scan-to-set latency: ms-level but unverified, which is why the
    clap stays primary.
  - Without Labs, filming an on-screen millisecond clock works; outdoors, Labs GPS time sync is an
    alternative.
- **Live webcam streams into one host** are timestamped on arrival by one clock. There is no
  cross-camera offset, and jitter is about one frame period.
- **BLE record start and stop** gets cameras rolling roughly together, but not frame-accurately.
- **Also useful from Labs:** Max Shutter Angle (crisp corners), White Balance Lock, and Wake on
  Power (the camera comes up with the robot).

**Rehearse once on real hardware.** Two cameras film the time QR, then a clap, and record for 2–3
minutes. Cross-correlating the audio gives the offset and drift, and that number validates the
recipe. It has a row in [measurements.md](measurements.md#pending-video-vs-webcam-comparison).

## Sources

- UMI paper: https://arxiv.org/html/2402.10329v3 and https://umi-gripper.github.io/umi.pdf
- UMI code and intrinsics: https://github.com/real-stanford/universal_manipulation_interface and https://github.com/real-stanford/universal_manipulation_interface/blob/main/example/calibration/gopro_intrinsics_2_7k.json
- ORB-SLAM3 fork: https://github.com/cheng-chi/ORB_SLAM3
- FastUMI: https://arxiv.org/html/2409.19499v1
- OpenImuCameraCalibrator: https://github.com/urbste/OpenImuCameraCalibrator
- GoPro Labs (HDMI, time QR, shutter, WB lock): https://gopro.github.io/labs/ and https://gopro.github.io/labs/control/extensions
- MS2109 on Linux: https://mjt.me.uk/posts/macrosilicon-ms2109/
- Cam Link 4K workaround: https://github.com/AdamGleave/elgato-camlink-workaround
