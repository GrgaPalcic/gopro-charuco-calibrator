# Measurements

Every calibration number we have, and what produced it. Errors are reprojection RMS in pixels, at
the image's native resolution unless noted.

A number is only comparable to another taken with the same board model. The June runs on the 10×7
board were first solved with a **mirrored marker layout** (see
[footguns.md](footguns.md#the-caibio-board-layout-can-be-mirrored)). Those rows are marked
*wrong layout* and are kept only as a record of what the bug looked like.

## Boards

| Name | Grid | Square / marker | Dictionary | IDs | Notes |
|---|---|---|---|---|---|
| **5X5 board** (primary) | 11×8 | 34 / 25 mm | `DICT_5X5_100` | 2–45 (44 markers) | A3 calib.io print, the default in every preset. Its layout parity matches the "standard" assumption. It does not cross-decode with the 4X4 gripper markers. OpenICC's own extractor cannot read it: that extractor assumes IDs from 0. |
| 4X4 board | 10×7 | 21 / 15 mm | `DICT_4X4_50` | 0–34 (35 markers) | Mirrored ("flipped") parity. Shares the dictionary family with the gripper markers, so avoid it (see [footguns.md](footguns.md#calibration-board-and-gripper-markers-share-a-dictionary)). |

## Datasets

| Dataset | Camera, lens mode | Frames | Board |
|---|---|---|---|
| `gopro13_umi_gripper_20260610_140106` | HERO13 + Max Lens Mod 2.0, USB webcam Wide, 1920×1080, setting 189 **not** set by the app | 108 `capture_###.jpg` | 4X4 |
| `GX010005.MP4` | HERO13 + Max Lens Mod 2.0, recording Max SuperView 167°, 4K 16:9, 24 fps, 89 s | video | 4X4 |
| `gopro13_hyperview_20260610_113917` | **camera not recorded** (the name came from an old default); SuperView, 720p | run | 4X4 |
| narrow Wide capture | **camera not recorded**; described as a flat or narrow Wide capture, possibly the HERO11 webcam stream, which looked flat | run | 5X5 |
| HERO11 runs | HERO11, USB webcam Wide | runs | 5X5 |
| synthetic (2026-09-29) | a Double Sphere camera with the 06-12 webcam intrinsics, 1920×1080, 60 random board poses, 0.15 px noise | 60 views | 5X5 geometry |

Software:
- **App:** opencv-contrib 4.13.0 in the app venv.
- **OpenICC:** Docker (Ubuntu 22.04, apt OpenCV 4.5.4, Ceres 2.1). The June image had the
  extractor's marker ratio patched to 15/21. Since 2026-09-29 the build is unpatched, at commit
  `d75dda5`.

## Results

### HERO13 + Max Lens Mod 2.0, USB webcam Wide 1080p (the Ludis dataset path)

| Date | Solver | Error | Views used | Double Sphere params | Notes |
|---|---|---|---|---|---|
| 06-10 | pinhole, *wrong layout* | 52 px | | | |
| 06-10 | `cv2.fisheye`, *wrong layout* | no convergence | | | |
| 06-10 | `cv2.omnidir`, *wrong layout* | 48 px / diverges | ~10 of 103 | | |
| 06-12 | **OpenICC Double Sphere, OpenICC's own extractor** (ChArUco chessboard corners) | **0.617 px** | 37 | f 629.19, cx 949.64, cy 538.89, xi 0.00166, **alpha 0.708** | Layout-independent. The best result so far. The params are those in the `tests/test_openicc.py` fixture, which matches the report's 0.617 px, 37 views and alpha 0.708. |
| by 06-16 | Double Sphere, in-app (our ArUco marker corners) | 1.11 px | 41 | alpha 0.71 | See the note below this table. |
| by 06-16 | `cv2.fisheye`, in-app | 1.19 px | ~50 | | Centre-weighted views only |
| by 06-16 | pinhole, in-app | 1.30 px | ~25 | | Centre-weighted views only |

**Why in-app is worse than OpenICC's extractor on the same frames (1.11 vs 0.617 px): not
measured.** The likely cause is the input. The app gives OpenICC ArUco marker corners, which sit
less precisely than the ChArUco chessboard corners OpenICC's extractor uses. **inferred**

### HERO13 + Max Lens Mod 2.0, recording Max SuperView 167°, 4K (`GX010005.MP4`)

| Date | Solver | Error | Views | Notes |
|---|---|---|---|---|
| 06-10 | pinhole, *wrong layout* | 72 px | | |
| 06-10 | `cv2.fisheye` / `cv2.omnidir`, *wrong layout* | fails / diverges | | |
| 06-12 | OpenICC Double Sphere, own extractor, `--downsample_factor=2` | 0.82 px, **at 1080p coordinates** | 136 | **alpha 1.0**, pinned at its bound: weak edge coverage |
| by 06-16 | Double Sphere, in-app | 2.16 px native (≈1.1 px at 1080p) | | Coordinates scaled down for the solve |
| by 06-16 | `cv2.fisheye`, in-app | fails | | The edge views are past what OpenCV's KB can fit |
| by 06-16 | pinhole, in-app | 2.47 px | | Centre-weighted views only |

### Other lens modes

| Date | Capture | Solver | Error | Notes |
|---|---|---|---|---|
| 06-10 | SuperView 720p (`gopro13_hyperview_…`), *wrong layout* | plumb_bob | 26.97 px selected / 44.11 px all frames | Anamorphic mode. Solved on the mirrored 4X4 layout and never re-solved. The June report calls it layout-independent, but shows no re-solve. |
| 06-10 | same, *wrong layout* | rational_polynomial | 26.77 / 43.72 px | |
| 06-10 | same, *wrong layout* | `cv2.fisheye` | ~49 px | Worse than pinhole |
| 06-10 | narrow Wide capture, 5X5 board | `cv2.fisheye` | 1.12 px | The camera was not recorded, so this is weak evidence for KB at ~130°. |
| June | Linear and narrow captures | pinhole | 1–2 px | |
| June | HERO11 webcam Wide, 5X5 board | pinhole | sub-pixel | The HERO11 Wide stream looked flat over webcam |

The 06-10 board check that later proved wrong: a geometry sweep fitted `cols=10, rows=7` at 52 px,
against 200–1000 px for every other geometry, and the highest detected ID was 34. The geometry was
right; the parity was not.

### Synthetic Double Sphere through OpenICC (2026-09-29)

This checks the app's OpenICC integration end to end:
`tests/test_hero13_readiness.py::test_double_sphere_recovers_measured_hero13_intrinsics`. All 60
synthetic views go to OpenICC, which keeps 47 to 50 of them itself.

Four runs on identical input:

| Run | f (true 629.19) | xi (true 0.00166) | alpha (true 0.708) | Max pixel error vs truth, rays ≤70° | rays ≤80° |
|---|---|---|---|---|---|
| a | 626.1 | −0.002 | 0.705 | 0.68 px | 0.68 px |
| b | 573.4 | −0.086 | 0.670 | 0.57 px | 1.40 px |
| c | 616.1 | −0.018 | 0.698 | 0.73 px | 0.73 px |
| d | 587.9 | −0.063 | 0.680 | 0.57 px | 0.90 px |

Six further runs gave f 581.9–616.9 px, xi −0.072 to −0.017 and alpha 0.675–0.699, with RMS
0.185–0.186 px every time and 47–50 views kept.

Across all ten runs, f spans 573–626 px at the same error. Rays map to within 0.73 px of the truth
out to 70° off-axis, and within 1.40 px out to 80°. The lens half-angle is ~83.5°, so the last
degrees are where the board data is thinnest.

Two things cause the spread: in Double Sphere, f, xi and alpha trade off against each other, and
OpenICC's view selection is not deterministic. **Compare Double Sphere calibrations by projecting
rays, never by focal length** (see
[footguns.md](footguns.md#double-sphere-parameters-are-not-unique)).

## Pending: video vs webcam comparison

The protocol was agreed in June; the results are still to be captured on the HERO13 with the Max
Lens Mod 2.0.
- **Board:** the 5X5 board for both captures.
- **Setup:** the same scene and lighting, a tripod where possible.
- **Record** what the camera itself shows for every setting, including ISO, shutter and white
  balance where visible.

| | Recording | Webcam, 189 not set | Webcam, 189 = 2 |
|---|---|---|---|
| Lens | Max SuperView | Wide | Wide (preset `gopro13_umi_gripper_fisheye_1080p`) |
| Resolution / fps | 4K 16:9 @ 24 | 1080p @ 30 | 1080p @ 30 |
| Stabilisation | HyperSmooth **off**, horizon lock **off** | not applicable; check the stream is unwarped | same |

The two webcam columns settle whether telling the camera the mod is fitted changes the webcam
image. The June reference was captured without setting 189.

**Capture.** One 60–90 s video sweep: slow, full-field, covering edges, corners, near and far, and
tilts. Then the same sweep through the app for each webcam column, targeting 60–100 captures.

Comparisons:

1. **Visual.** Put frames of the same pose side by side: video, and both webcam columns. Compare
   crop, field of view, black corners and sharpness. Extract the video frame with
   `ffmpeg -i GX*.MP4 -vf "select=eq(n\,100)" -vframes 1 video.png`.
2. **Metadata.** Run `exiftool -a -G1 GX*.MP4 | grep -iE "lens|fov|field|projection|stab"` and read
   the GPMF stream (`exiftool -ee`); the HERO13 records lens and mode metadata. Compare with each
   webcam run's `acquisition_mode`, including the `reported_*` fields.
3. **Solvers.** Run plumb_bob, fisheye and double_sphere on all three. Compare RMS, views kept and
   alpha. Compare the models by projecting the same rays through each, after scaling pixels by the
   resolution ratio. **Not** by focal length.
4. **Sync rehearsal** for multi-camera recordings: the clap offset and drift (see
   [umi-and-deployment.md](umi-and-deployment.md#multi-camera-sync)).

| Metric | Video 4K Max SuperView | Webcam, 189 not set | Webcam, 189 = 2 | Verdict |
|---|---|---|---|---|
| Field of view and crop | | | | |
| Lens and mode metadata | | | | |
| Double Sphere: RMS, views, alpha | | | | |
| fisheye: RMS, views | | | | |
| pinhole: RMS, views | | | | |
| Ray-map agreement (scaled) | | | | |
| Clap offset and drift | | | | |
