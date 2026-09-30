# The OpenICC backend (Double Sphere and Kannala–Brandt)

Two models are solved by
[OpenImuCameraCalibrator](https://github.com/urbste/OpenImuCameraCalibrator) (OpenICC), the tool
UMI calibrates with:
- **`double_sphere`** fits the Max Lens Mod's ~167° fisheye over the whole circle. OpenCV has no
  Double Sphere. It is the reference model and the recommended row whenever it solves.
- **`kannala_brandt`** is OpenICC's `FISHEYE` model, the Kannala–Brandt file UMI loads. It is
  solved on the same detections as Double Sphere and checked against it.

Terms are explained in the [glossary](README.md#glossary).

## Setup

```bash
uv run gopro-charuco setup-openicc
```

This fetches OpenICC at the pinned commit `d75dda5` into `~/.cache/gopro-charuco-calibrator/openicc`,
applies the source patch below, and builds the Docker image `gopro-charuco-openicc:d75dda5-p1`
(Ubuntu 22.04, apt OpenCV-contrib, Ceres 2.1, pyTheiaSfM). It takes about 10 minutes from scratch,
needs no GPU, and makes a 3.5 GB image. Your user must be able to run `docker` without sudo.

**If you built the older `openicc` image, run `setup-openicc` again.** The app now looks for
`gopro-charuco-openicc:d75dda5-p1` only. Until that image exists, the Double Sphere and
Kannala–Brandt rows fail with a message that names the setup command, and the other models still
solve. Once the new image is built, the old one can be removed with `docker rmi openicc` if
nothing else uses it.

The pin is deliberate: every machine should build the same solver. Bump `OPENICC_COMMIT` in
`openicc.py` on purpose, check that the patch still applies (the build stops if it does not), and
rerun the synthetic tests when you do. The image name carries the commit and the patch level
(`-p1`), so an image built before a patch was added is never silently reused.

### The source patch

**Why.** At `d75dda5`, `calibrate_camera` fits the distortion only in its first bundle adjustment,
while the principal point is still held at the image centre. The later adjustments free the
principal point, but the final one refines the distortion only for `PINHOLE`. For `DOUBLE_SPHERE`
and `FISHEYE` the distortion therefore stays fitted around the image centre, not the real principal
point. The HERO13 + Max Lens Mod principal point sits about 10 px off centre, which on synthetic Max
Lens Mod views put the unpatched solver 0.6–4.2 px off the true lens at the same noise-level RMS.
With the patch it is 0.1–0.5 px (5 runs per scene, 2026-09-29; the table is in
[measurements.md](measurements.md#synthetic-max-lens-mod-through-openicc-2026-09-29)). **verified**

**What.** `OPENICC_PATCHES` in `openicc.py` lists the edit: one line in
`src/core/camera_calibrator.cc`, so the final adjustment refines the distortion for every model
except `PINHOLE_RADIAL_TANGENTIAL`, which keeps its own branch. `setup-openicc` applies it between
the checkout and `docker build`. It stops with a message if the source does not match, instead of
building an unpatched solver under the patched name, and tells you to delete the checkout and
run `setup-openicc` again.

The patch touches only `calibrate_camera`. The app never uses OpenICC's board extractor.

## How the app uses it

1. **The app writes its own detections as OpenICC's UBJSON corners file**
   (`<run>/openicc/corners.uson`, and the same file in `<run>/openicc_kannala_brandt/`):
   - each detected ArUco marker corner is one point, with scene point ID
     `marker_id * 4 + corner_index`;
   - views are keyed by synthetic microsecond timestamps.

   `calibrate_camera` treats these as arbitrary 3D↔2D correspondences and assumes no board
   structure. OpenICC's extractor, board spec and marker ratio never come into it, so no board
   reprint or extractor patch is needed. **verified**
2. **It runs `calibrate_camera` in Docker, once per model:**
   `--camera_model_to_calibrate=DOUBLE_SPHERE` in `<run>/openicc/`, and `FISHEYE` in
   `<run>/openicc_kannala_brandt/`. Each run is:
   - as your uid:gid, under a unique container name;
   - with a timeout of `OPENICC_TIMEOUT_S`, after which the container is killed;
   - after deleting old `out*.json` files, so a re-solve can never read a stale result;
   - run once more if `calibrate_camera` crashes (it segfaulted once in about 165 runs on
     2026-09-29, in the `FISHEYE` start-up, and the same input solved on every other run).

   One solve takes about 4–20 s per model on synthetic 1080p views (workstation CPU, 2026-09-29).
3. **It scales coordinates, not images.** Inputs taller than 1080 px are divided down for the solve,
   because OpenICC drops views at an absolute ~2 px. Focal length, principal point and error are
   scaled back afterwards; xi, alpha and k1–k4 have no scale and stay as they are.
4. **It writes these files:**
   - `<camera>_double_sphere.json`, in OpenICC's layout at native resolution: `intrinsic_type`,
     `image_width`, `image_height`, `intrinsics` (`focal_length`, `aspect_ratio`,
     `principal_pt_x/y`, `xi`, `alpha`, and `skew` if OpenICC reports it), `final_reproj_error`,
     `nr_calib_images`, `solve_downsample_factor`;
   - `<camera>_kannala_brandt.json`, OpenICC's `FISHEYE` output in the same layout, with
     `radial_distortion_1`–`4` in place of xi and alpha. That is the layout of UMI's
     `gopro_intrinsics_2_7k.json`, so UMI's loader reads it unchanged; it ignores the extra
     `solve_downsample_factor`. **verified** by a test that runs a copy of UMI's
     `parse_fisheye_intrinsics` on our file;
   - `<camera>_kannala_brandt_orbslam3.yaml`, the ORB-SLAM3 `KannalaBrandt8` camera block
     (`Camera1.fx/fy/cx/cy/k1–k4`, width, height, fps, with fx = fy = `focal_length`). Its header
     says to merge it into an existing settings file, whose IMU block is not calibrated here;
   - `calibrate_camera.log` in each model's folder, with the full command and output.
5. **Every failure becomes a result row.** No Docker, image not built, no permission, timeout, no
   convergence, or fewer than 10 usable views: each one gives a failed row labelled with that
   model. The message says what went wrong and, for setup problems, the fix. The other models
   still solve.

The UI shows the camera matrix `[[f, skew, cx], [0, f·aspect, cy], [0, 0, 1]]` and "distortion"
`[xi, alpha]` for Double Sphere, or `[k1, k2, k3, k4]` for Kannala–Brandt.

**License.** OpenICC is AGPL-3.0. The app only runs it as a subprocess and exchanges files with
it. Nothing is imported, linked or vendored, and each user builds the image themselves.

### Settings

| Environment variable | Default | Meaning |
|---|---|---|
| `OPENICC_DOCKER_IMAGE` | `gopro-charuco-openicc:d75dda5-p1` | Image to run, and the tag `setup-openicc` builds |
| `OPENICC_BINARY` | unset | Path to a native `calibrate_camera`; skips Docker |
| `OPENICC_GRID_SIZE` | `0.1` | OpenICC's pose voxel filter |
| `OPENICC_TIMEOUT_S` | `360` | Solve timeout |
| `OPENICC_DOCKER_ROOT` | unset | `1` runs the container as root instead of your uid:gid |

## Reading a result

- **Error.** On our real webcam frames, OpenICC's own extractor reached 0.617 px and the app
  1.11 px, both in June with the **unpatched** solver. Those are the only real-frame reference
  points; the June reports called under ~1 px usable, and there is no validated threshold beyond
  that. The gap is most likely the input: the app sends ArUco marker corners, which are less
  precise than the ChArUco chessboard corners OpenICC's extractor uses (**inferred**). A low RMS
  does not show the solver found the right lens: the unpatched solver was off by up to 4 px at the
  same RMS as the patched one (see [measurements.md](measurements.md#synthetic-max-lens-mod-through-openicc-2026-09-29)).
- **alpha near 1.0** (its range is 0–1) means the board missed the edge of the circle. Capture more
  edge views. The app flags alpha ≥ 0.98.
- **Don't compare f, xi or alpha between two solves.** They trade off: the same data has given f
  598–626 px at identical RMS with the patched solver (573–626 px unpatched). Compare models by
  projecting rays instead (see [footguns.md](footguns.md#double-sphere-parameters-are-not-unique)).
- **The For UMI row** checks the Kannala–Brandt file against Double Sphere:
  - the **board reach** is the widest off-axis angle any detected corner reached, read through
    the Double Sphere model;
  - the **match** is the largest pixel distance between the two models over rays out to that
    angle, counting only rays that land on the sensor, with Kannala–Brandt evaluated as UMI loads
    it (fy = fx);
  - above 1 px (at 1080p and below; the limit grows with the image height above 1080 rows), both rows get a warning to
    solve again. Double Sphere is the recommended model, so its warning turns the result RETAKE.
    On the USB route, click **Solve** again first; add views near the edge of the circle only if
    the gap stays. On the recording route, add another clip with views near the edge.
    The reference is Double Sphere as solved, not the true lens, so either one can be the one
    that is off;
  - an `aspect_ratio` more than 0.5 % from 1 gets a note on the For UMI row only,
    because UMI drops it. A PASS then says "See the note on the For UMI row".

  On synthetic views, the For UMI comparison counted over every ray out to the reach (not only
  the rays on the sensor) was 0.13–0.28 px with the patched solver and 2.5–5.2 px unpatched (rim
  scene, 5 runs each, 2026-09-29). Without rim views the unpatched gap stayed under 1 px in those
  5 runs even though both models were off (one of 3 reruns on 2026-09-30 reached 2.79 px), so no
  warning does not rule out the old image (see
  [measurements.md](measurements.md#synthetic-max-lens-mod-through-openicc-2026-09-29)).

## On-camera recordings

**Use the app's From a recording route** for a clip recorded on the camera: it checks the clip,
picks sharp, still, varied views and solves them with the models below
([recording-route.md](recording-route.md)). The two manual ways here predate it. They are still
useful for older recordings in another mode or on another board, such as `GX010005.MP4` (4K 16:9
Max SuperView, 4X4 board). Calibrate in the mode the data is recorded in either way.

### In-app solver on extracted frames

Extract distinct, sharp frames, then solve them with the Max Lens Mod preset's models. **Pass the
board the video was shot with.** `GX010005.MP4` used the 10×7 4X4 board, not the preset's 5X5:

```bash
mkdir -p rec/frames
ffmpeg -i GX010005.MP4 -vf fps=2 -q:v 2 rec/frames/capture_%04d.jpg
uv run gopro-charuco solve-frames --frames-dir rec/frames --output-dir rec \
  --config gopro_charuco_calibrator/presets/gopro13_mlm2_adwal002.yaml \
  --camera-name gopro13_recording_4k \
  --cols 10 --rows 7 --square-mm 21 --marker-mm 15 \
  --aruco-dict DICT_4X4_50 --start-id 0 --marker-count 35
```

The coordinate rescale for 4K is automatic, and the board's mirrored layout is detected by itself.
The in-app solver measured 2.16 px native (≈1.1 px at 1080p) on this video by 06-16. How the frames
were extracted then was not recorded, so the `ffmpeg` line above is a suggestion: choose a rate
that keeps the poses distinct, and drop blurred frames.

### Manual OpenICC on a recording

This runs OpenICC's own extractor and is how the 06-12 results were produced. Its extractor builds
a stock ChArUco board: IDs from 0, marker = ½ square, and the dictionary set by `--aruco_dict`
(0 = `DICT_4X4_50`).
- **The 5X5 board cannot be read** this way, because it starts at ID 2.
- **The 4X4 board can**, but its markers are 15/21 of a square, so build a separate image with
  the extractor's marker ratio patched. Keep the app's `gopro-charuco-openicc:d75dda5-p1` image as
  it is.

The recipe copies the source that `setup-openicc` fetched, which it has already patched
(`OPENICC_PATCHES`), so this image's `calibrate_camera` carries the same fix as the app's. A
checkout fetched before the patch existed is patched the next time you run `setup-openicc`. The
06-12 results below were made with an image that had only the marker-ratio edit, so their
`calibrate_camera` was unpatched.

```bash
# needs `uv run gopro-charuco setup-openicc` to have fetched (and patched) the source first
rm -rf /tmp/openicc-extractor   # cp -r would nest into an existing copy, and the sed would miss
cp -r ~/.cache/gopro-charuco-calibrator/openicc /tmp/openicc-extractor
sed -i 's|FLAGS_checker_square_length_m / 2.0f|FLAGS_checker_square_length_m * (15.0f / 21.0f)|' \
  /tmp/openicc-extractor/applications/extract_board_to_json.cc
docker build -t openicc-extractor /tmp/openicc-extractor
```

**From a recording** (Max SuperView, 4K), this gave 0.82 px at 1080p coordinates on 06-12
(unpatched `calibrate_camera`):

```bash
docker run --rm -v "$PWD":/data openicc-extractor bash -lc '
  cd /OpenImuCameraCalibrator
  timeout 360 ./build/applications/extract_board_to_json --input_path=/data/GX010005.MP4 \
    --aruco_detector_params=resource/charuco_detector_params.yml --board_type=charuco --aruco_dict=0 \
    --num_squares_x=10 --num_squares_y=7 --checker_square_length_m=0.021 --downsample_factor=2 \
    --save_corners_json_path=/data/rec_corners.uson --logtostderr=1
  timeout 360 ./build/applications/calibrate_camera --input_corners=/data/rec_corners.uson \
    --camera_model_to_calibrate=DOUBLE_SPHERE --save_path_calib_dataset=/data/rec_ds \
    --grid_size=0.1 --optimize_board_points=false --verbose=true --logtostderr=1
  cat /data/rec_ds.json'
```

`--downsample_factor=2` shrinks the image before detection ("I_new = 1/factor · I"). That keeps
OpenICC's 2 px view filter at a sane scale, but costs corner precision. The app instead detects at
full resolution and only rescales coordinates.

**From webcam frames** (`capture_###.jpg`), this gave the 0.617 px reference on 06-12
(unpatched `calibrate_camera`); with the marker-ratio edit the extractor found corners on every
frame. The extractor
reads a video, or a folder of `*.png` named by nanosecond timestamp. Hardlinking the JPGs works,
because `cv::imread` decodes by content:

```bash
cd <run folder>
mkdir -p wcframes_png
for f in frames/capture_*.jpg; do n=$(basename "$f" .jpg); n=${n#capture_}; \
  ln "$f" "wcframes_png/$((10#$n * 33333333)).png"; done
docker run --rm -v "$PWD":/data openicc-extractor bash -lc '
  cd /OpenImuCameraCalibrator
  timeout 300 ./build/applications/extract_board_to_json --input_path=/data/wcframes_png \
    --aruco_detector_params=resource/charuco_detector_params.yml --board_type=charuco --aruco_dict=0 \
    --num_squares_x=10 --num_squares_y=7 --checker_square_length_m=0.021 --downsample_factor=1 \
    --save_corners_json_path=/data/wc_corners.uson --logtostderr=1
  timeout 360 ./build/applications/calibrate_camera --input_corners=/data/wc_corners.uson \
    --camera_model_to_calibrate=DOUBLE_SPHERE --save_path_calib_dataset=/data/wc_ds \
    --grid_size=0.1 --optimize_board_points=false --verbose=true --logtostderr=1
  cat /data/wc_ds.json'
```

Instead of hardlinks you can assemble a video:
`ffmpeg -framerate 30 -i frames/capture_%03d.jpg -c:v libx264 -pix_fmt yuv420p wc.mp4`.

**Other options:**
- `--board_type=radon` is OpenICC's plain-checkerboard mode.
- `--camera_model_to_calibrate=FISHEYE` gives the Kannala–Brandt file UMI loads, as the app's
  `kannala_brandt` model does (see
  [umi-and-deployment.md](umi-and-deployment.md#loading-our-calibration-in-umi)).
- It also accepts `EXTENDED_UNIFIED` (EUCM) and `DIVISION_UNDISTORTION`. The patch was measured
  only for `DOUBLE_SPHERE` and `FISHEYE`.

## Checking the integration

```bash
uv run pytest tests/test_hero13_readiness.py -k "double_sphere or kannala_brandt or patch"
```

These render synthetic views through the 06-12 webcam Double Sphere result (f 629.19, cx 949.64,
cy 538.89, xi 0.00166, alpha 0.708) with 0.15 px corner noise, solve them through the real image,
and check the ray-to-pixel map against that true lens (not f):
- the base scene, 60 views reaching 64.4° off-axis, for both models;
- the rim scene, the same plus 30 views out to the 83.5° edge of the circle, for both models;
- that the patch applies to the pinned checkout and that `setup-openicc` applies it before
  `docker build` (these need no Docker).

The Docker tests are skipped when the image is not built. The four took 22–42 s together on a
workstation CPU (2026-09-30); OpenICC's own run time varies from run to run.

`tests/test_openicc_integration.py` solves Double Sphere (only) on real frames when `FRAMES_DIR`
points at a run's `frames/` folder. It **defaults to the 10×7 4X4 board**, so for a 5X5 run set the board too:

```bash
FRAMES_DIR=runs/<run>/frames BOARD_COLS=11 BOARD_ROWS=8 BOARD_SQUARE_M=0.034 BOARD_MARKER_M=0.025 \
  BOARD_DICT=DICT_5X5_100 BOARD_START_ID=2 BOARD_MARKER_COUNT=44 \
  uv run pytest tests/test_openicc_integration.py
```

## Sources

- OpenImuCameraCalibrator: https://github.com/urbste/OpenImuCameraCalibrator
- Double Sphere model: https://arxiv.org/abs/1807.08957
