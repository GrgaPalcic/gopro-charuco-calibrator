# UMI, deployment and multi-camera recording

How the reference pipeline (UMI, the Universal Manipulation Interface) uses a GoPro, how to load
our calibration into it, which live paths exist for a robot, and how to sync several cameras.
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

## Loading our calibration in UMI

The Ludis dataset was recorded from the **HERO13 USB webcam stream: 1080p, Wide, with the Max Lens
Mod 2.0 fitted**. That gives a circular fisheye with black corners. It is nominally the 167° lens;
the webcam image's exact field of view has not been measured. The app's reference model for it is
**Double Sphere**, the only one that is valid over the whole circle.

**UMI loads Kannala–Brandt only.** **verified** 2026-09-29:
- UMI's loader `parse_fisheye_intrinsics` in `umi/common/cv_util.py` asserts
  `intrinsic_type == 'FISHEYE'` and reads four KB coefficients. It evaluates the model with
  `cv2.fisheye`, the same theta polynomial as OpenICC's `FISHEYE`.
- The `cheng-chi/ORB_SLAM3` fork ships only `Pinhole` and `KannalaBrandt8` camera models. UMI
  runs it with a fixed settings YAML baked into its Docker image; nothing generates that file from
  the json.

So the Max Lens Mod preset also solves **`kannala_brandt`** with OpenICC `FISHEYE` on the same
detections as Double Sphere, and writes two files for UMI:

- **`<camera>_kannala_brandt.json` drops straight into `parse_fisheye_intrinsics`.** It is
  OpenICC's own output, the layout of UMI's `gopro_intrinsics_2_7k.json`, plus a
  `solve_downsample_factor` field that UMI ignores. **verified** by a test that runs a copy of
  UMI's loader on our file, and by a test that `cv2.fisheye.projectPoints` agrees with the app's
  own Kannala–Brandt projection.
- **`aspect_ratio` is ignored.** UMI reads `focal_length` for both fx and fy. The app therefore
  evaluates the KB model with fy = fx in its checks, and warns when the solved aspect ratio is more
  than 0.5 % from 1.
- **Calibrate at the resolution you record.** UMI's `convert_fisheye_intrinsics_resolution`
  rescales the intrinsics by image height and assumes the width is only cropped or padded
  symmetrically. Calibrating the exact stream you record (here 1920×1080 webcam) keeps that
  conversion a no-op.
- **`<camera>_kannala_brandt_orbslam3.yaml` is only the camera block.** It holds
  `Camera.type: "KannalaBrandt8"`, `Camera1.fx/fy/cx/cy`, `Camera1.k1`–`k4`, width, height and fps,
  with fx = fy. Merge it into your existing ORB-SLAM3 settings file in place of its camera lines,
  and keep that file's IMU block, which this app does not calibrate. How you get the edited file
  into UMI's SLAM container depends on your setup; we have not run UMI's SLAM with it yet.

**How well Kannala–Brandt holds at 167°.** On synthetic Max Lens Mod views, OpenICC's KB (with the
[patched solver](double-sphere-backend.md#the-source-patch)) landed within 0.49 px of the true
lens out to the widest angle the board reached, and within 0.32 px out to the 83.5° rim when the
board reached it. Past the board it extrapolates: up to 1.12 px about 6° beyond it (2026-09-29, see
[measurements.md](measurements.md#synthetic-max-lens-mod-through-openicc-2026-09-29)). It has not
been solved on real frames yet. So sweep the board right into the edge of the circle.

**The For UMI row** in the result checks each real solve: "Kannala–Brandt for UMI: matches Double
Sphere within X px out to Y°", where Y is the widest angle the board reached and only rays that
land on the sensor count. Over 1 px at 1080p, it warns: solve again, and add edge views if the gap
stays. The comparison is with Double Sphere as solved, not the true lens, so either can be the one
that is off.

OpenCV's own Kannala–Brandt (`fisheye`) is not a substitute for the UMI file. It solved the webcam
frames at 1.19 px only over centre-weighted views, failed on the 4K recording once edge views were
included, and leaves the far periphery unchecked, which is where UMI's side mirrors sit. The preset
keeps it for its ROS camera_info YAML.

## Live deployment

**Our live path is the USB webcam.** It is what the app captures and what the Ludis dataset was
recorded from, so a robot reading the same webcam stream sees the same image the policy was
trained on, with the same intrinsics. Timestamps come from one host clock. The webcam image
calibrated at 1.11 px in the app (0.617 px with OpenICC's own extractor), both in June with the
unpatched solver.

**HDMI capture is UMI's path.** It is the robust option for a live feed: a stable latency, and no
overlays once Labs clean HDMI is on. But it is a **different image** from the webcam stream. It
needs its own calibration and, for a learned policy, training data captured the same way. Don't
mix the two.

| Piece | What it does | Without it |
|---|---|---|
| GoPro **Media Mod** | micro-HDMI output | no HDMI |
| GoPro **Labs** firmware, extension `HDMI=1` (QR command `oMHDMI=1` for one session, `!MHDMI=1` to keep it; the short `*HDMI=1` needs HERO10+) | clean output with no overlays | overlays and resets, unusable as a feed |
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
