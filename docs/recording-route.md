# The "From a recording" route

How the app calibrates a clip recorded on the camera: the settings and why, the two GoPro Labs QR
codes and which of their codes are confirmed, how to record, what the clip check reads, how views
are picked, what the run writes, and how UMI loads the files. The step-by-step walkthrough is in
the [README](../README.md#from-a-recording). Terms are explained in the
[glossary](README.md#glossary).

Tags: **verified** means measured by us or read from the primary source. **inferred** means
consistent with the evidence but not directly demonstrated. **unverified** means documented for
another camera or mode, or not documented at all, and not yet tried on our cameras.

**No real HERO13 clip has been through this route yet** (2026-09-30). Everything below about what
a HERO13 writes into its files comes from GoPro's documentation, not from a file we read. The
first real clip's results go into
[measurements.md](measurements.md#recording-route-first-real-clip).

## Why a second route

The Ludis dataset is recorded on the camera to mp4, as UMI records its data. A calibration made
over the USB webcam stream cannot be trusted for that footage
([footguns.md](footguns.md#a-webcam-calibration-is-not-valid-for-on-camera-recordings)): the
webcam stops at 1080p 16:9, has no lens-mod lens, and handles stabilisation its own way. UMI (a
HERO9 with the Max Lens Mod 1.0) calibrates from on-camera clips with OpenICC, as OpenICC's own
GoPro guide does. So this route calibrates a clip recorded in exactly the dataset's mode.

The USB route stays, for data that really is the webcam stream.

## The settings

Decided 2026-09-30: record the dataset as close to UMI as the HERO13 allows, and calibrate in
exactly that mode. Both lens-mod camera setups (`gopro13_mlm2_adwal002`,
`gopro13_uwlm_aewal001`) carry these in their `recording:` section.

| Setting | Value | Why | Status |
|---|---|---|---|
| Lens mod | Max Lens Mod 2.0 (ADWAL-002), selected by hand; or Ultra Wide Lens Mod (AEWAL-001), detected when fitted | GoPro: the Max Lens Mod 2.0 "has the same settings and features" as the Ultra Wide Lens Mod, "except for auto-detection" | verified (GoPro) |
| Mode | Video | | |
| Resolution | 4K, 4:3 (4000×3000) | With a lens mod, 4:3 exists only at 4K. UMI records 4:3 too (`r27T`, 2.7K 4:3, in its QR code). | verified (GoPro) |
| Frame rate | 60 fps | 4K 4:3 with a lens mod offers 60, 50, 30, 25 and 24 fps; 60 is the fastest. | verified (GoPro) |
| Lens | Ultra Wide | The widest lens with a mod at 4:3: 145° H × 113° V × 176° D ([lens-modes-and-models.md](lens-modes-and-models.md#hero13-lens-mods-at-43)). | verified (GoPro) |
| HyperSmooth | Off | Stabilisation warps each frame differently, so no single lens model fits. The field of view is the same on and off. | verified (GoPro, FOV) |
| Protune | On | Needed to set the shutter and ISO. | |
| Shutter | 1/480 s for the calibration clip, Auto for the dataset | A fast shutter keeps the moving board sharp (Kalibr's advice: short shutter). 1/480 s at 60 fps is a 45° shutter angle. | decided 2026-09-30 |
| ISO max | 1600 | The Labs creator's default; the fast shutter needs the light. | |

**Switch the shutter back after the calibration clip.** The dataset is recorded with the shutter
on Auto, which is what QR code 2 sets. The page says so in step 2, and shows QR code 2 again next
to a passed result.

What a HERO13 file is: all HERO13 video is HEVC, at roughly 100–120 Mb/s, so a 90 s clip is about
1.4 GB in one `GX01xxxx.MP4` chapter. The camera splits a recording into chapters at about 4 GB
(cards up to 32 GB) or about 12 GB. **verified** (GoPro)

## The QR codes

The camera must run GoPro Labs firmware. With the camera on, point it at the code on the screen.
**QR code 1** is for the calibration clip; **QR code 2** is the same with the shutter on Auto, for
the dataset. The page keeps QR code 2 folded away while you scan QR code 1, because the camera
reads any code it sees.

The strings, built by `gopro_charuco_calibrator/labs.py` from the camera setup:

| Camera setup | QR code 1 (calibration clip) | QR code 2 (dataset) |
|---|---|---|
| HERO13 + Max Lens Mod 2.0 (ADWAL-002) | `mVr4Tp60e0!NoX2fXti16S45` | `mVr4Tp60e0!NoX2fXti16S0` |
| HERO13 + Ultra Wide Lens Mod (AEWAL-001) | `mVr4Tp60e0!NoX10fXti16S45` | `mVr4Tp60e0!NoX10fXti16S0` |

The order follows the Labs QR creator's source: mode, resolution, frame rate, HyperSmooth, `!N`,
lens, `t` (Protune), then the Protune fields, with the ISO cap and shutter as `i<max>S<angle>`.
For comparison, UMI's own QR code (HERO9, no lens-mod code) is
`!MHDMI=1mV0r27Tp60fWe0hS0sLcFg1dVoW1oD4oCoSv1oL0q0oR1`.

| Code | Meaning | Source | Status |
|---|---|---|---|
| `mV` | video mode | Labs settings page | verified |
| `r4T` | 4K 4:3 | Labs QR creator ("4k 4:3") | verified |
| `p60` | 60 fps | Labs settings page | verified |
| `e0` | HyperSmooth Off | Labs QR creator | verified |
| `!N` | a short pause the creator puts between HyperSmooth and the lens | Labs QR creator source ("delay") | verified |
| `oX2` | Max Lens Mod 2.0 | Labs settings page and release notes | verified |
| `oX10` | auto-detect lens mods | HERO13 Labs release notes (1.12.70) | verified; how it combines with `fX` is **unverified** |
| `fX` | the creator's "Enable MSV" (Max SuperView) with `oX2`, for HERO12–13; the settings page calls it "SuperMax Wide (Max Lens Mod)" | Labs QR creator and settings page | **unverified** that it selects Ultra Wide on a HERO13 at 4:3 |
| `t` | Protune controls on | Labs QR creator | verified |
| `i16` | ISO max 1600 | Labs QR creator | verified |
| `S45` | shutter locked at a 45° angle, 1/480 s at 60 fps | Labs QR creator ("Lock Shutter") | verified as a code; **unverified** that a HERO13 shows 1/480 |
| `S0` | shutter Auto | Labs QR creator | verified |

Other codes, not used:
- **`oX3`** is Max Lens Mod 2.5 in the HERO13 Labs notes (and setting 189 = 3). That is almost
  certainly the Ultra Wide Lens Mod, so `oX3fX` may be the direct counterpart of `oX2fX`
  (**unverified**). The page lists it, folded away, as an alternative to try; the QR codes keep
  `oX10` until a camera has scanned both.
- **`$EXPQ=480`**, a Labs extension, also fixes the shutter at 1/480; the creator's `S45` is used
  instead, so both codes come from one source.

**After scanning, check the camera screen:** lens Ultra Wide, the lens mod set, 4K 4:3, 60 fps,
HyperSmooth Off, shutter 1/480. If a line differs, set it by hand; the page says how for each
unverified code. Report what the screen showed so the unverified codes can be fixed.

## How to record

- **Length:** 60–90 s. OpenICC's GoPro advice is "Move SLOWLY" for 20–30 s; the longer clip
  gives time to hold all 23 positions. The clip check calls a clip under 20 s short.
- **Speed:** move slowly, and hold the board still for about a second at each position. The app
  keeps only views where the board was still (see [how views are picked](#how-views-are-picked)).
- **Positions:** follow the animation in step 3. It is the camera's view of the 4:3 frame, with
  the board moving through the 23 guide positions in order: the centre, a far ring of eight plus
  the far centre, a near ring of eight plus the near centre, then four tilted positions. At the
  far positions the board centre reaches the dashed line; at the near ones the board's edges touch
  the frame edges. One loop takes about 64 s.
- **Coverage:** push the board right to the edges and corners of the frame, near and far, with
  tilt. This follows Kalibr's advice (whole field, tilts, distances). The fisheye's edge is where
  Double Sphere's alpha and the For UMI check are decided.
- **Shutter:** the calibration clip at 1/480 s (QR code 1). Even light and a matte print help.
- Record every clip of one run in the same mode, with the same camera.

## The clip check

Before picking views, the app reads the clip and compares it with the camera setup
(`gopro_charuco_calibrator/clipcheck.py`). It never blocks the solve. It reads:
- **ffprobe:** the first video stream's width, height, frame rate, codec, length and rotation
  flag;
- **the GoPro metadata (GPMF)** inside the mp4, read by `gopro_charuco_calibrator/gpmf.py`
  without loading the file: the global camera metadata in the `moov/udta/GPMF` box, and the first
  sample of the `gpmd` telemetry track (about a second of data).

GPMF is KLV: a 4-byte FourCC key, a type byte, a structure-size byte, a 2-byte big-endian repeat
count, then the payload padded to 4 bytes; type 0 means the payload is more KLV. The tags read, as
the gpmf-parser README documents them: `DVNM`/`MINF` model name, `CASN` serial, `FIRM` firmware,
`VFOV` lens letter, `ZFOV` diagonal field of view in degrees, `EISE` and `EISA` stabilisation,
`VRES` resolution, `VFPS` frame rate, `SHUT` exposure time, `ISOE` ISO, and `ACCL`/`GYRO` (the
IMU). **No lens-mod tag is documented**, nor which `VFOV` letter a lens mod writes, nor the
HERO13's exact tag shapes. So every value is read defensively: an odd shape gives "unknown", never
an error.

Each row has a status:
- **ok:** the file shows the expected value;
- **mismatch:** both the expected value and the value in the file are known, and they differ.
  Any mismatch turns the page's box red, above the result, and the headline repeats it;
- **unknown:** the file does not say, so the row tells you what to check on the camera screen.

| Row | From | Expected | Mismatch when |
|---|---|---|---|
| Camera | `MINF` (or `DVNM` when it names a HERO) | HERO13 Black | the model is not a HERO13 |
| Resolution | ffprobe | 4000x3000 | another size |
| Frame rate | ffprobe (else `VFPS`) | 60 fps | more than 0.2 % off (59.94 fps passes) |
| HyperSmooth | `EISE`, `EISA` | Off | `EISE` is "Y", or `EISA` is anything but "N/A" |
| Lens | `VFOV`, `ZFOV` | Ultra Wide, with the lens mod | never: always **unknown**, because the file cannot confirm the lens or the mod. Check the camera screen. |
| Shutter | `SHUT`, median over the first telemetry sample | 1/480 s | more than 15 % off; **unknown** when the file has no `SHUT` |
| Rotation flag | ffprobe | none | the file tells players to turn the picture. Frames are read as stored. |
| Video codec | ffprobe | — | never (a fact about the file) |
| Length | ffprobe | — | never; under 20 s adds "Short clip" advice |
| Motion sensor data | `ACCL`/`GYRO` present | — | never |
| Camera serial | `CASN` | the run's earlier clips' serial | a clip from another camera; it then gets a run of its own |
| Whole clip read | ffmpeg while picking views | the probed length | ffmpeg stopped with an error, or read under 90 % of the clip (a copy that stopped early). What was read is still used. |

The red box reads, for example:

> This clip was not recorded with the preset's settings: Frame rate is 29.97 fps, expected
> 60 fps; HyperSmooth is On (HS Boost), expected Off. Check every setting on the camera again,
> and record the clip again if any differ. The calibration below is only valid for footage
> recorded exactly like this clip.

What to do then: fix the setting, click **Start this camera again** (the flagged run keeps its
files), and record a new clip. Do not add the new clip to the flagged run, or its views mix with
the wrong ones. To keep the result anyway, click **Next camera**.

**A clip that is not used.** Its file is removed from the run folder and the page says why:
- it is not a video ffmpeg can read;
- it is a different size from the run's first clip (record every clip of a run in one mode);
- ffmpeg failed while reading it for views (a clip with too few usable views is kept, and the
  page asks for another clip instead);
- it is from another camera, and a folder for that camera's run could not be made.

A clip from another camera is otherwise fine: it starts a new run for its camera, and the
previous run stays as it was.

## How views are picked

`gopro_charuco_calibrator/recording.py` reads the clip through ffmpeg at 12 frames a second
(`recording.sample_hz`) and judges each one. A frame left out gets one reason, counted in the
summary and shown on the page:

| Reason | Shown as | When |
|---|---|---|
| `no_board` | no board found | fewer than 8 markers (`capture.min_markers`) found on a half-size copy |
| `moving` | moving too fast | the markers moved more than the motion limit since the frame before, or the frame before had no board to compare with. The limit is the live route's `capture.max_motion_px` scaled to the clip's width and rate: at 4000 px wide and 12 Hz, about 7.8 px between frames. |
| `blurred` | blurred | sharper frames exist of the same held position, or, over the whole clip, its sharpness is under half (`recording.blur_ratio`) of the clip's 90th percentile |
| `duplicate` | too like a view already kept | the same held position as a sharper frame, or a pose too close (`capture.min_param_dist`) to a view already kept, from this clip or an earlier one |
| `over_cap` | over the view limit | past 120 views in the run (`recording.max_views`); views that complete a still-missing guide position are always kept |

Frames where the board is still and in the same place form one held position; its sharpest frame
is the candidate. Sharpness is the variance of the Laplacian inside the board's box, divided by
the variance of the pixels there, so a darker or greyer board does not score lower. The blur rule
was measured on synthetic clips only (1600×1200, 2026-09-30); real HERO13 footage has not been
measured yet. The full rule is in the module docstring of `recording.py`.

Kept views are saved at full resolution as `frames/capture_###.jpg`, with `overlays/`, and solved
by the same `solve_from_frames` as the USB route. The guide's 23 positions are judged on the kept
views, and the missing ones are listed by their animation numbers for a retake.

**A retake** is another clip dropped into the same run (**Add another clip**). Only the new clip
is read; its views are judged against every view already kept, and then everything is solved
again. Numbering continues across clips.

## Outputs

A run writes `runs/<camera_name>_<timestamp>/`, with everything the USB route writes (see the
[README](../README.md#outputs)) plus:

- **`clips/`**: every clip dropped into the run, copied unchanged. A second clip of the same name
  gets `_2`.
- **`config.json`**: `config.camera` is the camera as recorded (the clip's size and frame rate,
  not the preset's webcam settings), and `acquisition_mode` has `route: "recording"`, the lens
  and lens mod as set on the camera (the file cannot confirm them), the codec, camera model,
  serial and firmware.
- **The summary's `recording` block** (`caib_marker_board_calibration_summary.json`):
  - `clips`: per clip, every clip-check row (`field`, `label`, `expected`, `found`, `status`,
    `advice`), the ffprobe facts, the metadata read, the drop `counts` and the views kept;
  - `counts`: over the run, `samples` read, `kept`, and each drop reason;
  - `mismatch_count`, `mismatch_fields`, `mismatch_clips`: the settings that differ and where;
  - `serial`, `camera_named_from_serial`, `previous_run_id`, `views`;
  - `orbslam3`: the ORB-SLAM3 block's path, size and a note.
- **The run name.** If the camera name is still the one the camera setup filled in, the run is
  renamed after the clip's serial, `gopro13_<last 4 characters>`, so each file maps to one
  physical camera.

## Loading the files in UMI

- **`<camera>_kannala_brandt.json` stays at the clip's size, 4000×3000.** UMI's
  `parse_fisheye_intrinsics` reads `focal_length` for both fx and fy, and
  `convert_fisheye_intrinsics_resolution` rescales the intrinsics to the video by its height.
  **verified** from UMI's code. Copy it as `gopro_intrinsics_2_7k.json`, as the
  [README](../README.md#using-the-files-in-umi) says.
- **`<camera>_kannala_brandt_orbslam3.yaml` is written at 960×720**, the size UMI's SLAM runs at
  (its `gopro10_maxlens_fisheye_setting_v1_720.yaml`). 4000×3000 × 0.24 is exactly 960×720, so
  fx, fy, cx and cy are scaled by 0.24 and k1–k4 stay as they are. A clip that is not 4:3 keeps
  the block at its own size, and the note says so.
- **The IMU is in the clip.** On-camera clips carry the GPMF `ACCL` and `GYRO` streams UMI's
  inertial SLAM reads. What this app does not calibrate is the IMU-to-camera transform and UMI's
  IMU noise settings; see
  [umi-and-deployment.md](umi-and-deployment.md#loading-our-calibration-in-umi), with UMI's SLAM
  mask.

## Sources

- GoPro Labs settings (`mV`, `p60`, `fX`, `oX2`): https://gopro.github.io/labs/control/settings/
- GoPro Labs QR creator (`r4T`, `e0`, `!N`, `oX2fX` "Enable MSV", `t`, `i16`, `S45`, `S0`): https://gopro.github.io/labs/control/custom/
- GoPro Labs release notes (HERO13 `oX10`, `oX3`): https://gopro.github.io/labs/control/notes/
- GoPro Labs extensions (`EXPQ`): https://gopro.github.io/labs/control/extensions/
- Open GoPro HTTP API spec (setting 189): https://gopro.github.io/OpenGoPro/http
- GPMF format and tags: https://github.com/gopro/gpmf-parser
- UMI code (the loader, the SLAM settings file): https://github.com/real-stanford/universal_manipulation_interface
- OpenImuCameraCalibrator (GoPro recording advice): https://github.com/urbste/OpenImuCameraCalibrator
- Kalibr calibration targets and advice: https://github.com/ethz-asl/kalibr/wiki
