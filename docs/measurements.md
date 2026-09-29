# Measurements

Every calibration number we have, with what produced it. Errors are reprojection RMS in pixels
at the image's native resolution unless noted.

A number is only comparable to another taken with the same board model. The June runs on the
10×7 board were first solved with a **mirrored marker layout** (see
[footguns.md](footguns.md#the-caibio-board-layout-can-be-mirrored)). Those rows are marked
*wrong layout* and are kept only as the record of what the bug looked like.

## Boards

| Name | Grid | Square / marker | Dictionary | IDs | Notes |
|---|---|---|---|---|---|
| **5X5 board** (primary) | 11×8 | 34 / 25 mm | `DICT_5X5_100` | 2–45 (44 markers) | A3 calib.io print. Default in every preset. Its layout parity matches the "standard" assumption. Does not cross-decode with the 4X4 gripper markers. |
| 4X4 board | 10×7 | 21 / 15 mm | `DICT_4X4_50` | 0–34 (35 markers) | Mirrored ("flipped") parity. Shares the dictionary family with the gripper markers, so avoid it (see [footguns.md](footguns.md#calibration-board-and-gripper-markers-share-a-dictionary)). |

## Datasets

| Dataset | Camera, lens mode | Frames | Board |
|---|---|---|---|
| `gopro13_umi_gripper_20260610_140106` | HERO13 + Max Lens Mod 2.0, USB webcam Wide, 1920×1080 | 108 `capture_###.jpg` | 4X4 |
| `GX010005.MP4` | HERO13 + Max Lens Mod 2.0, recording Max SuperView 167°, 4K 16:9, 24 fps, 89 s | video | 4X4 |
| `gopro13_hyperview_20260610_113917` | **camera not recorded** (the name came from an old default, not the camera); SuperView, 720p | run | 4X4 |
| narrow Wide capture | stock lens, Wide ~130° | run | 5X5 |
| HERO11 runs | HERO11, USB webcam Wide | runs | 5X5 |
| synthetic (2026-09-29) | Double Sphere camera with the June webcam intrinsics, 1920×1080, 60 random board poses, 0.15 px noise | 60 views | 5X5 geometry |

Software: opencv-contrib 4.13.0 in the app venv. OpenICC runs in Docker, with apt OpenCV 4.5.4
in the June image and commit `d75dda5` since 2026-09-29.

## Results

### HERO13 + Max Lens Mod 2.0, USB webcam Wide 1080p (the Ludis dataset path)

| Date | Solver | Error | Views used | Double Sphere params | Notes |
|---|---|---|---|---|---|
| 06-10 | pinhole, *wrong layout* | 52 px | | | |
| 06-10 | `cv2.fisheye`, *wrong layout* | no convergence | | | |
| 06-10 | `cv2.omnidir`, *wrong layout* | 48 px / diverges | ~10 of 103 | | |
| 06-12 | **OpenICC Double Sphere, OpenICC's own extractor** | **0.617 px** | 37 | f 629.19, cx 949.64, cy 538.89, aspect 0.9998, xi 0.00166, **alpha 0.708** | Layout-independent. The best result so far. |
| 06-16 | Double Sphere, in-app (our detections) | 1.11 px | 41 | alpha 0.71 | |
| 06-16 | `cv2.fisheye`, in-app | 1.19 px | ~50 | | Centre-weighted views only |
| 06-16 | pinhole, in-app | 1.30 px | ~25 | | Centre-weighted views only |

### HERO13 + Max Lens Mod 2.0, recording Max SuperView 167°, 4K (`GX010005.MP4`)

| Date | Solver | Error | Views | Notes |
|---|---|---|---|---|
| 06-10 | pinhole, *wrong layout* | 72 px | | |
| 06-10 | `cv2.fisheye` / `cv2.omnidir`, *wrong layout* | fails / diverges | | |
| 06-12 | OpenICC Double Sphere, own extractor, `--downsample_factor=2` | 0.82 px (1080p coords) | 136 | **alpha 1.0**: pinned at the bound, meaning weak edge coverage |
| 06-16 | Double Sphere, in-app | 2.16 px native (≈1.1 px at 1080p) | | Auto-downscaled solve |
| 06-16 | `cv2.fisheye`, in-app | fails | | Edge views are past what OpenCV's KB can fit |
| 06-16 | pinhole, in-app | 2.47 px | | Centre-weighted views only |

### Other lens modes

| Date | Capture | Solver | Error | Notes |
|---|---|---|---|---|
| 06-10 | SuperView 720p (`gopro13_hyperview_…`) | plumb_bob | 26.97 px selected / 44.11 px all frames | Anamorphic mode |
| 06-10 | same | rational_polynomial | 26.77 / 43.72 px | |
| 06-10 | same | `cv2.fisheye` | ~49 px | Worse than pinhole |
| 06-10 | narrow Wide ~130°, 5X5 board | `cv2.fisheye` | 1.12 px | In range for KB |
| June | Linear / narrow captures | pinhole | 1–2 px | |
| June | HERO11 webcam Wide, 5X5 board | pinhole | sub-pixel | The HERO11 Wide stream looked flat over webcam |

### Synthetic Double Sphere through OpenICC (2026-09-29)

This checks the app's OpenICC integration end to end:
`tests/test_hero13_readiness.py::test_double_sphere_recovers_measured_hero13_intrinsics`. The
input is 60 synthetic views rendered through the 06-12 webcam intrinsics with 0.15 px noise;
OpenICC keeps 47 to 50 of them.

| Run | RMS | f (true 629.19) | xi (true 0.00166) | alpha (true 0.708) | Max pixel error vs truth, rays ≤70° | rays ≤80° |
|---|---|---|---|---|---|---|
| 0 | 0.186 | 626.1 | −0.002 | 0.705 | 0.68 px | 0.68 px |
| 1 | 0.186 | 573.4 | −0.086 | 0.670 | 0.57 px | 1.40 px |
| 2 | 0.185 | 616.1 | −0.018 | 0.698 | 0.73 px | 0.73 px |
| 3 | 0.186 | 587.9 | −0.063 | 0.680 | 0.57 px | 0.90 px |

The same input gives focal lengths from 573 to 626 px at identical RMS, but every run maps rays to
pixels within 0.73 px of the truth out to 70° off-axis. In Double Sphere, f, xi and alpha trade
off against each other, and OpenICC's view selection is not deterministic. **Compare Double Sphere
calibrations by projecting rays, never by focal length** (see
[footguns.md](footguns.md#double-sphere-parameters-are-not-unique)).

## Pending: video vs webcam comparison

The protocol was agreed in June, and the results are still to be captured on the HERO13 with the
Max Lens Mod 2.0.
- **Board:** the 5X5 board for both captures.
- **Setup:** same scene and lighting, a tripod where possible.
- **Record:** note what the camera itself shows for each setting.

| | Recording | Webcam |
|---|---|---|
| Lens | Max SuperView | Wide (preset `gopro13_umi_gripper_fisheye_1080p`) |
| Resolution / fps | 4K 16:9 @ 24 | 1080p @ 30 |
| Stabilisation | HyperSmooth **off**, horizon lock **off** | not applicable; check the stream is unwarped, and see `reported_hypersmooth` in `config.json` |

**Capture.** One 60–90 s video sweep: slow, full-field, covering edges, corners, near and far,
and tilts. Then the same sweep through the app, targeting 60–100 captures.

Comparisons:

1. **Visual.** Put a video frame and a webcam frame of the same pose side by side, and compare
   crop, field of view, black corners and sharpness. Extract the frame with
   `ffmpeg -i GX*.MP4 -vf "select=eq(n\,100)" -vframes 1 video.png`.
2. **Metadata.** Run `exiftool -a -G1 GX*.MP4 | grep -iE "lens|fov|field|projection|stab"` and
   read the GPMF stream (`exiftool -ee`). Compare with the webcam run's `acquisition_mode`,
   including the `reported_*` fields.
3. **Solvers.** Run plumb_bob, fisheye and double_sphere on both. Compare RMS, views kept and
   alpha. Compare the models by projecting the same rays through each, after scaling pixels by
   the resolution ratio. **Not** by focal length.
4. **Sync rehearsal** for multi-camera recordings. Measure the clap offset and drift (see
   [umi-and-deployment.md](umi-and-deployment.md#multi-camera-sync)).

| Metric | Video 4K Max SuperView | Webcam 1080p Wide | Verdict |
|---|---|---|---|
| Field of view and crop | | | |
| Lens and mode metadata | | | |
| Double Sphere: RMS, views, alpha | | | |
| fisheye: RMS, views | | | |
| pinhole: RMS, views | | | |
| Ray-map agreement (scaled) | | | |
| Clap offset and drift | | | |
