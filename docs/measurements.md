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
| `gopro13_umi_gripper_20260610_140106` | HERO13 + Max Lens Mod 2.0, USB webcam Wide, 1920×1080 | 108 `capture_###.jpg` | 4X4 |
| `GX010005.MP4` | HERO13 + Max Lens Mod 2.0, recording Max SuperView 167°, 4K 16:9, 24 fps, 89 s | video | 4X4 |
| `gopro13_hyperview_20260610_113917` | **camera not recorded** (the name came from an old default); SuperView, 720p | run | 4X4 |
| narrow Wide capture | **camera not recorded**; described as a flat or narrow Wide capture, possibly the HERO11 webcam stream, which looked flat | run | 5X5 |
| HERO11 runs | HERO11, USB webcam Wide | runs | 5X5 |
| synthetic (2026-09-29) | a Double Sphere camera with the 06-12 webcam result as the true lens (f 629.19, cx 949.64, cy 538.89, xi 0.00166, alpha 0.708), 1920×1080, 0.15 px corner noise. **Base scene:** 60 random board poses, reaching 64.4° off-axis. **Rim scene:** the same plus 30 views out to the 83.5° edge of the circle. Rendered by `_hero13_scene` in `tests/test_hero13_readiness.py` | 60 or 90 views | 5X5 geometry |

Software:
- **App:** opencv-contrib 4.13.0 in the app venv.
- **OpenICC:** Docker (Ubuntu 22.04, apt OpenCV 4.5.4, Ceres 2.1).
  - The June image had only the extractor's marker ratio patched to 15/21, so its
    `calibrate_camera` was **unpatched**.
  - Early on 2026-09-29 the app used an unpatched build at commit `d75dda5`, the image `openicc`.
  - Since 2026-09-29 the app builds `gopro-charuco-openicc:d75dda5-p1`: the same commit with the
    final-adjustment patch (`OPENICC_PATCHES`, see
    [double-sphere-backend.md](double-sphere-backend.md#the-source-patch)).

  Every real-frame OpenICC result below was made with an unpatched `calibrate_camera` and has not
  been re-solved since.

## Results

### HERO13 + Max Lens Mod 2.0, USB webcam Wide 1080p (the Ludis dataset path)

| Date | Solver | Error | Views used | Double Sphere params | Notes |
|---|---|---|---|---|---|
| 06-10 | pinhole, *wrong layout* | 52 px | | | |
| 06-10 | `cv2.fisheye`, *wrong layout* | no convergence | | | |
| 06-10 | `cv2.omnidir`, *wrong layout* | 48 px / diverges | ~10 of 103 | | |
| 06-12 | **OpenICC Double Sphere, OpenICC's own extractor** (ChArUco chessboard corners), unpatched solver | **0.617 px** | 37 | f 629.19, cx 949.64, cy 538.89, xi 0.00166, **alpha 0.708** | Layout-independent. The best real-frame result so far. The params are those in the `tests/test_openicc.py` fixture, which matches the report's 0.617 px, 37 views and alpha 0.708. |
| by 06-16 | Double Sphere, in-app (our ArUco marker corners), unpatched solver | 1.11 px | 41 | alpha 0.71 | See the note below this table. |
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
| 06-12 | OpenICC Double Sphere, own extractor, `--downsample_factor=2`, unpatched solver | 0.82 px, **at 1080p coordinates** | 136 | **alpha 1.0**, pinned at its bound: weak edge coverage |
| by 06-16 | Double Sphere, in-app, unpatched solver | 2.16 px native (≈1.1 px at 1080p) | | Coordinates scaled down for the solve |
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

### Synthetic Max Lens Mod through OpenICC (2026-09-29)

This checks the app's OpenICC integration end to end, and answers "does OpenICC's Kannala–Brandt
hold at 167°?". The scenes are the synthetic dataset above. Each figure is the **worst pixel
distance to the true lens** over rays out to the stated angle (36 rings from the axis to that
angle, 48 directions on each), with Kannala–Brandt evaluated as UMI loads it (fy = fx). RMS says how well a model fits the
views; this says whether it found the right lens, which only a synthetic test can.

**Patched vs unpatched solver.** Solved on a workstation CPU on 2026-09-29, 5 runs of each
model per scene with each image, one OpenICC run per model (4–27 s each), with a one-off script
over the test scenes. The patched image is `gopro-charuco-openicc:d75dda5-p1`, the unpatched one
the old `openicc` build of the same commit.

| Scene | What | Patched | Unpatched |
|---|---|---|---|
| Rim (90 views, reach 83.5°) | Double Sphere, out to the rim | 0.09–0.32 px | 0.91–4.24 px |
| Rim | Kannala–Brandt, out to the rim | 0.11–0.32 px | 1.93–3.93 px |
| Rim | Kannala–Brandt vs Double Sphere, as the **For UMI** row measures it | 0.13–0.28 px | 2.49–5.16 px |
| Base (60 views, reach 64.4°) | Double Sphere, out to 70° | 0.17–0.24 px | 0.64–0.79 px |
| Base | Kannala–Brandt, out to the reach | 0.23–0.44 px | 0.60–0.81 px |

The RMS hardly moves: 0.184–0.186 px patched, 0.185–0.201 px unpatched. OpenICC kept 46–53 views
per solve. So a low RMS did not reveal the unpatched solver's error. What the patch changes is in
[double-sphere-backend.md](double-sphere-backend.md#the-source-patch).

**Kannala–Brandt over 13 patched runs** (these 5, plus 8 more from the test runs that day):
- base scene: 0.23–0.49 px out to the reach, 0.43–1.12 px out to 70°. Past the board the model
  extrapolates, so sweep the board to the rim;
- rim scene: 0.11–0.32 px out to the rim.

The four Kannala–Brandt coefficients are not the limit: fitted straight to the true lens curve,
they match it to 0.0006 px out to 83.5° (`test_kannala_brandt_can_represent_hero13_lens`). **So
OpenICC's Kannala–Brandt holds at 167° on synthetic data, with the patched solver, out to where the
board reached.** It has not been measured on real frames yet.

The Docker tests in `tests/test_hero13_readiness.py` assert these bounds with margin: under 1 px
for both models on both scenes (Double Sphere on the base scene out to 70°, the others out to the
reach), and for Kannala–Brandt on the base scene under 2 px out to 70°. The **For UMI** check on real runs counts only rays that land on
the sensor; the figures above count every ray out to the angle.

**Double Sphere parameters still spread with the patch.** Five patched runs on the base scene
(2026-09-30): f 597.6–625.5 px (true 629.19), xi −0.049 to −0.004, alpha 0.687–0.706, at 0.185 px
RMS every time with 46–48 views kept. The principal point landed at cx 949.8, cy 538.8–539.0 (true
949.64, 538.89).

The earlier runs with the **unpatched** image (2026-09-29), kept as a record:

| Run | f (true 629.19) | xi (true 0.00166) | alpha (true 0.708) | Max pixel error vs truth, rays ≤70° | rays ≤80° |
|---|---|---|---|---|---|
| a | 626.1 | −0.002 | 0.705 | 0.68 px | 0.68 px |
| b | 573.4 | −0.086 | 0.670 | 0.57 px | 1.40 px |
| c | 616.1 | −0.018 | 0.698 | 0.73 px | 0.73 px |
| d | 587.9 | −0.063 | 0.680 | 0.57 px | 0.90 px |

Six further unpatched runs gave f 581.9–616.9 px, xi −0.072 to −0.017 and alpha 0.675–0.699, with
RMS 0.185–0.186 px every time and 47–50 views kept.

Two things cause the parameter spread, patched or not: in Double Sphere, f, xi and alpha trade off
against each other, and OpenICC's view selection is not deterministic. **Compare Double Sphere
calibrations by projecting rays, never by focal length** (see
[footguns.md](footguns.md#double-sphere-parameters-are-not-unique)).

## Pending: video vs webcam comparison

The protocol was agreed in June; the results are still to be captured on the HERO13 with the Max
Lens Mod 2.0.
- **Board:** the 5X5 board for both captures.
- **Setup:** the same scene and lighting, a tripod where possible.
- **Record** what the camera itself shows for every setting, including ISO, shutter and white
  balance where visible.

| | Recording | Webcam |
|---|---|---|
| Lens | Max SuperView | Wide (preset `gopro13_umi_gripper_fisheye_1080p`) |
| Resolution / fps | 4K 16:9 @ 24 | 1080p @ 30 |
| Stabilisation | HyperSmooth **off**, horizon lock **off** | not applicable; check the stream is unwarped |
| Setting 189 | Max Lens 2.0; note what the camera shows | set by the preset; confirm the **Mod** chip reads Max Lens 2.0 (2) |

**Capture.** One 60–90 s video sweep: slow, full-field, covering edges, corners, near and far, and
tilts. Then the same sweep through the app, targeting 60–100 captures.

Comparisons:

1. **Visual.** Put frames of the same pose side by side: video and webcam. Compare
   crop, field of view, black corners and sharpness. Extract the video frame with
   `ffmpeg -i GX*.MP4 -vf "select=eq(n\,100)" -vframes 1 video.png`.
2. **Metadata.** Run `exiftool -a -G1 GX*.MP4 | grep -iE "lens|fov|field|projection|stab"` and read
   the GPMF stream (`exiftool -ee`); the HERO13 records lens and mode metadata. Compare with each
   the webcam run's `acquisition_mode`, including the `reported_*` fields.
3. **Solvers.** Run plumb_bob, fisheye, double_sphere and kannala_brandt on both. Compare RMS,
   views kept and alpha. Compare the models by projecting the same rays through each, after scaling pixels by the
   resolution ratio. **Not** by focal length.
4. **Sync rehearsal** for multi-camera recordings: the clap offset and drift (see
   [umi-and-deployment.md](umi-and-deployment.md#multi-camera-sync)).

| Metric | Video 4K Max SuperView | Webcam 1080p Wide | Verdict |
|---|---|---|---|
| Field of view and crop | | | |
| Lens and mode metadata | | | |
| Double Sphere: RMS, views, alpha | | | |
| Kannala–Brandt (OpenICC): RMS, views, For UMI match | | | |
| fisheye: RMS, views | | | |
| pinhole: RMS, views | | | |
| Ray-map agreement (scaled) | | | |
| Clap offset and drift | | | |
