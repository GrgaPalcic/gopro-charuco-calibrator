# The Double Sphere backend (OpenICC)

The `double_sphere` model is the one that fits the Max Lens Mod's ~167° fisheye. OpenCV has no
Double Sphere, so the app drives
[OpenImuCameraCalibrator](https://github.com/urbste/OpenImuCameraCalibrator) (OpenICC), the tool
UMI calibrates with. Terms are explained in the [glossary](README.md#glossary).

## Setup

```bash
uv run gopro-charuco setup-openicc
```

This fetches OpenICC at the pinned commit `d75dda5` into `~/.cache/gopro-charuco-calibrator/openicc`
and builds the `openicc` Docker image (Ubuntu 22.04, apt OpenCV-contrib, Ceres 2.1, pyTheiaSfM). It
takes about 10 minutes from scratch, needs no GPU, and makes a 3.5 GB image. Your user must be able
to run `docker` without sudo.

The pin is deliberate: every machine should build the same solver. Bump `OPENICC_COMMIT` in
`openicc.py` on purpose, and rerun the synthetic test when you do. The image is **unpatched**,
which is correct for the app. The app never uses OpenICC's board extractor.

## How the app uses it

1. **The app writes its own detections as OpenICC's UBJSON corners file**
   (`<run>/openicc/corners.uson`):
   - each detected ArUco marker corner is one point, with scene point ID
     `marker_id * 4 + corner_index`;
   - views are keyed by synthetic microsecond timestamps.

   `calibrate_camera` treats these as arbitrary 3D↔2D correspondences and assumes no board
   structure. OpenICC's extractor, board spec and marker ratio never come into it, so no board
   reprint or source patch is needed. **verified**
2. **It runs `calibrate_camera --camera_model_to_calibrate=DOUBLE_SPHERE` in Docker:**
   - as your uid:gid, under a unique container name;
   - with a timeout of `OPENICC_TIMEOUT_S`, after which the container is killed;
   - after deleting old `out*.json` files, so a re-solve can never read a stale result.
3. **It scales coordinates, not images.** Inputs taller than 1080 px are divided down for the solve,
   because OpenICC drops views at an absolute ~2 px. Focal length, principal point and error are
   scaled back afterwards.
4. **It writes two files:**
   - `<camera>_double_sphere.json`, in OpenICC's layout at native resolution: `intrinsic_type`,
     `intrinsics` (`focal_length`, `aspect_ratio`, `principal_pt_x/y`, `xi`, `alpha`, and `skew`
     if OpenICC reports it), `final_reproj_error`, `nr_calib_images`, `solve_downsample_factor`;
   - `openicc/calibrate_camera.log`, with the full command and output.
5. **Every failure becomes a result row.** No Docker, image not built, no permission, timeout, no
   convergence, or fewer than 10 usable views: each one gives a failed `double_sphere` row whose
   message names the fix. The other models still solve.

The UI shows the camera matrix `[[f, skew, cx], [0, f·aspect, cy], [0, 0, 1]]` and "distortion"
`[xi, alpha]`.

**License.** OpenICC is AGPL-3.0. The app only runs it as a subprocess and exchanges files with
it. Nothing is imported, linked or vendored, and each user builds the image themselves.

### Settings

| Environment variable | Default | Meaning |
|---|---|---|
| `OPENICC_DOCKER_IMAGE` | `openicc` | Image to run |
| `OPENICC_BINARY` | unset | Path to a native `calibrate_camera`; skips Docker |
| `OPENICC_GRID_SIZE` | `0.1` | OpenICC's pose voxel filter |
| `OPENICC_TIMEOUT_S` | `360` | Solve timeout |
| `OPENICC_DOCKER_ROOT` | unset | `1` runs the container as root instead of your uid:gid |

## Reading a result

- **Error.** OpenICC's own extractor reached 0.617 px on our webcam frames; the app reaches
  1.11 px on the same frames. Those are the two reference points; the June reports called under
  ~1 px usable, and there is no validated threshold beyond that. The gap is most likely the input:
  the app sends ArUco marker corners, which are less precise than the ChArUco chessboard corners
  OpenICC's extractor uses (**inferred**).
- **alpha near 1.0** (its range is 0–1) means the board missed the edge of the circle. Capture more
  edge views. The app flags alpha ≥ 0.98.
- **Don't compare f, xi or alpha between two solves.** They trade off: the same data has given f
  573–626 px at identical RMS. Compare models by projecting rays instead
  (see [footguns.md](footguns.md#double-sphere-parameters-are-not-unique)).

## On-camera recordings

The app captures the live webcam stream. For a recording, for example Max SuperView at 4K, there
are two routes. Calibrate in the mode the data is recorded in either way.

### In-app solver on extracted frames

Extract distinct, sharp frames, then solve them with the Max Lens Mod preset's models. **Pass the
board the video was shot with.** `GX010005.MP4` used the 10×7 4X4 board, not the preset's 5X5:

```bash
mkdir -p rec/frames
ffmpeg -i GX010005.MP4 -vf fps=2 -q:v 2 rec/frames/capture_%04d.jpg
uv run gopro-charuco solve-frames --frames-dir rec/frames --output-dir rec \
  --config gopro_charuco_calibrator/presets/gopro13_umi_gripper_fisheye_1080p.yaml \
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
- **The 4X4 board can**, but its markers are 15/21 of a square, so build a separate, patched
  image. Keep the `openicc` image the app uses as it is.

```bash
# needs `uv run gopro-charuco setup-openicc` to have fetched the source first
rm -rf /tmp/openicc-extractor   # cp -r would nest into an existing copy, and the sed would miss
cp -r ~/.cache/gopro-charuco-calibrator/openicc /tmp/openicc-extractor
sed -i 's|FLAGS_checker_square_length_m / 2.0f|FLAGS_checker_square_length_m * (15.0f / 21.0f)|' \
  /tmp/openicc-extractor/applications/extract_board_to_json.cc
docker build -t openicc-extractor /tmp/openicc-extractor
```

**From a recording** (Max SuperView, 4K), this gave 0.82 px at 1080p coordinates on 06-12:

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

**From webcam frames** (`capture_###.jpg`), this gave the 0.617 px reference on 06-12; with the
patched image the extractor found corners on every frame. The extractor
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
- `--camera_model_to_calibrate` also accepts `EXTENDED_UNIFIED` (EUCM), `DIVISION_UNDISTORTION` and
  `FISHEYE` (Kannala–Brandt). `FISHEYE` is the type UMI's pipeline accepts
  (see [umi-and-deployment.md](umi-and-deployment.md#where-our-calibration-differs)).

## Checking the integration

```bash
uv run pytest tests/test_hero13_readiness.py -k double_sphere
```

This renders 60 synthetic views through the `tests/test_openicc.py` fixture intrinsics (consistent
with the 06-12 webcam result), solves them through the real
image, and checks the ray-to-pixel map (not f). It is skipped when the image is not built.

`tests/test_openicc_integration.py` runs the same on real frames when `FRAMES_DIR` points at a run's
`frames/` folder. It **defaults to the 10×7 4X4 board**, so for a 5X5 run set the board too:

```bash
FRAMES_DIR=runs/<run>/frames BOARD_COLS=11 BOARD_ROWS=8 BOARD_SQUARE_M=0.034 BOARD_MARKER_M=0.025 \
  BOARD_DICT=DICT_5X5_100 BOARD_START_ID=2 BOARD_MARKER_COUNT=44 \
  uv run pytest tests/test_openicc_integration.py
```

## Sources

- OpenImuCameraCalibrator: https://github.com/urbste/OpenImuCameraCalibrator
- Double Sphere model: https://arxiv.org/abs/1807.08957
