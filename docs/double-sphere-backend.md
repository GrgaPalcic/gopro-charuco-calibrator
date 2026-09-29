# The Double Sphere backend (OpenICC)

The `double_sphere` model is the one that fits the Max Lens Mod's ~167° fisheye. OpenCV has no
Double Sphere, so the app drives
[OpenImuCameraCalibrator](https://github.com/urbste/OpenImuCameraCalibrator) (OpenICC), the tool
UMI calibrates with.

## Setup

```bash
uv run gopro-charuco setup-openicc
```

This fetches OpenICC at the pinned commit `d75dda5` into `~/.cache/gopro-charuco-calibrator/openicc`
and builds the `openicc` Docker image (Ubuntu 22.04, apt OpenCV-contrib, Ceres, pyTheiaSfM). It
takes about 10 minutes from scratch, needs no GPU, and makes a 3.5 GB image. Your user must be
able to run `docker` without sudo.

The pin is deliberate: every machine should build the same solver. Bump
`OPENICC_COMMIT` in `openicc.py` on purpose, and rerun the synthetic test when you do.

## How the app uses it

1. **The app detects the markers itself** and writes OpenICC's UBJSON corners file:
   - scene point ID = `marker_id * 4 + corner_index`;
   - views are keyed by synthetic microsecond timestamps;
   - the file goes to `<run>/openicc/corners.uson`.

   `calibrate_camera` treats these as arbitrary 3D↔2D correspondences and assumes no board
   structure. So OpenICC's extractor, board spec and marker ratio never come into it, and no
   board reprint or source patch is needed. **verified**
2. **It runs `calibrate_camera --camera_model_to_calibrate=DOUBLE_SPHERE`** in Docker:
   - as your uid:gid;
   - under a unique container name;
   - with a timeout of `OPENICC_TIMEOUT_S`, after which the container is killed.

   Old `out*.json` files are deleted first, so a re-solve can never read a stale result.
3. **It scales for resolution.** Inputs taller than 1080 px are divided down before the solve
   (OpenICC drops views at an absolute ~2 px), and focal length, principal point and error are
   scaled back afterwards.
4. **It writes the results:**
   - `<camera>_double_sphere.json`, in OpenICC's own layout at native resolution: `intrinsic_type`,
     `intrinsics.focal_length`, `aspect_ratio`, `principal_pt_x/y`, `xi`, `alpha`, `skew`,
     `final_reproj_error`, `nr_calib_images` and `solve_downsample_factor`;
   - `openicc/calibrate_camera.log`, the full command and output.
5. **It reports failure as a result row.** Any failure (no Docker, image not built, no permission,
   timeout, no convergence, fewer than 10 usable views) becomes a failed `double_sphere` row whose
   message names the fix. The other models still solve.

The camera matrix shown in the UI is `[[f, 0, cx], [0, f·aspect, cy], [0, 0, 1]]`, and
"distortion" is `[xi, alpha]`.

**License.** OpenICC is AGPL-3.0. The app only runs it as a subprocess and exchanges files with
it: nothing is imported, linked or vendored, and each user builds the image themselves.

### Settings

| Environment variable | Default | Meaning |
|---|---|---|
| `OPENICC_DOCKER_IMAGE` | `openicc` | Image to run |
| `OPENICC_BINARY` | unset | Path to a native `calibrate_camera`; skips Docker |
| `OPENICC_GRID_SIZE` | `0.1` | OpenICC's pose voxel filter |
| `OPENICC_TIMEOUT_S` | `360` | Solve timeout |
| `OPENICC_DOCKER_ROOT` | unset | `1` runs the container as root instead of your uid:gid |

## Reading a result

- **Error:** below ~0.7 px is good and below ~1 px is usable. Our webcam Max Lens Mod runs gave
  0.62 px (OpenICC's extractor) and 1.11 px (in-app).
- **alpha near 1.0** means the board missed the edge of the circle. Capture more edge poses.
- **Do not compare f, xi or alpha between two solves.** They trade off: the same data has given
  f 573–626 px at identical RMS. Compare models by projecting rays (see
  [footguns.md](footguns.md#double-sphere-parameters-are-not-unique)).

## On-camera recordings

The app captures the live webcam stream. For a recording (for example Max SuperView at 4K), use
one of two routes, and always calibrate in the mode the data is recorded in.

### In-app solver on extracted frames

Extract sharp frames, then solve with the gripper preset's models:

```bash
mkdir -p rec/frames
ffmpeg -i GX010005.MP4 -vf fps=2 -q:v 2 rec/frames/capture_%04d.jpg
uv run gopro-charuco solve-frames --frames-dir rec/frames --output-dir rec \
  --config gopro_charuco_calibrator/presets/gopro13_umi_gripper_fisheye_1080p.yaml \
  --camera-name gopro13_recording_4k
```

The 4K rescale is automatic. Measured on `GX010005.MP4`: 2.16 px native, about 1.1 px at 1080p
scale. The `ffmpeg` line is a suggestion: pick a frame rate that keeps the poses distinct, and
drop blurred frames.

### Manual OpenICC on a recording

This is OpenICC's own extractor on the video, which is how the 0.82 px recording result was
produced. Its extractor hardcodes marker = ½ square. For a board with another ratio, patch one
line in `applications/extract_board_to_json.cc` before building your own image, for example for
21/15 mm:

```cpp
const float aruco_marker_length = FLAGS_checker_square_length_m * (15.0f / 21.0f);  // was / 2.0f
```

Don't apply that patch to the image the app uses: the app never uses the extractor, so the patch
is irrelevant there. The dictionary is a flag (`--aruco_dict=0` = `DICT_4X4_50`). The commands,
as run on 2026-06-12:

```bash
docker run --rm -v "$PWD":/data openicc bash -lc '
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

`--downsample_factor=2` is the same 2 px-threshold fix the app applies automatically.

To run OpenICC's extractor on still frames instead of a video, you need a directory of `*.png`
named by nanosecond timestamp. Hardlinking JPGs works, because `cv::imread` decodes by content:

```bash
mkdir -p png && for f in frames/capture_*.jpg; do n=$(basename "$f" .jpg); n=${n#capture_}; \
  ln "$f" "png/$((10#$n * 33333333)).png"; done
```

OpenICC also calibrates `EXTENDED_UNIFIED` (EUCM), `DIVISION_UNDISTORTION` and `FISHEYE`
(Kannala–Brandt) with `--camera_model_to_calibrate`. `FISHEYE` is the type UMI's pipeline
accepts (see [umi-and-deployment.md](umi-and-deployment.md)).

## Checking the integration

```bash
uv run pytest tests/test_hero13_readiness.py -k double_sphere
```

This renders 60 synthetic views through the 06-12 webcam intrinsics and solves them through the
real image. It checks the ray-to-pixel map, not f. The test is skipped when the image is not built.
`tests/test_openicc_integration.py` does the same on real frames when `FRAMES_DIR` points at a run's
`frames/` folder.

## Sources

- OpenImuCameraCalibrator: https://github.com/urbste/OpenImuCameraCalibrator
- Double Sphere model: https://arxiv.org/abs/1807.08957
