from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class BoardConfig(BaseModel):
    """caib.io marker-board geometry.

    The app treats cols/rows as checkerboard squares, not marker counts.
    """

    source: Literal["caib.io"] = "caib.io"
    cols: int = Field(default=11, ge=2, le=40)
    rows: int = Field(default=8, ge=2, le=40)
    square_m: float = Field(default=0.034, gt=0.0)
    marker_m: float = Field(default=0.025, gt=0.0)
    aruco_dict: str = "DICT_5X5_100"
    start_id: int = Field(default=2, ge=0)
    marker_count: int = Field(default=44, ge=1)

    @field_validator("aruco_dict")
    @classmethod
    def normalize_dictionary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("aruco_dict is required")
        return value if value.startswith("DICT_") else f"DICT_{value}"

    @model_validator(mode="after")
    def validate_lengths(self) -> BoardConfig:
        if self.marker_m >= self.square_m:
            raise ValueError("marker_m must be smaller than square_m")
        expected = caib_marker_count(self.cols, self.rows)
        if self.marker_count > expected:
            raise ValueError(
                f"marker_count {self.marker_count} exceeds caib.io layout capacity {expected}"
            )
        return self

    @property
    def pattern_width_m(self) -> float:
        return float(self.cols * self.square_m)

    @property
    def pattern_height_m(self) -> float:
        return float(self.rows * self.square_m)


class CameraConfig(BaseModel):
    # Neutral placeholder, not a specific model: presets set a real name. A
    # model-specific default (the old "gopro13_hyperview") silently mislabels
    # every un-preset'd run after the camera it was named for.
    camera_name: str = "gopro_camera"
    device: str = "/dev/video0"  # only used when GoPro auto-setup is off
    width: int = Field(default=1280, ge=160)
    height: int = Field(default=720, ge=120)
    fps: float = Field(default=30.0, gt=0.0)
    fourcc: str = Field(default="YUYV", min_length=0, max_length=8)

    @field_validator("camera_name")
    @classmethod
    def clean_camera_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("camera_name is required")
        safe = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in value)
        return safe.strip("_") or "camera"


class GoProSettingsConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    apply_on_preview: bool = True
    stop_webcam_first: bool = True
    start_webcam: bool = True
    start_video_bridge: bool = True
    # Safe defaults for an un-preset'd run: 1080p + Wide. The old defaults were
    # 720p + SuperView(3) - SuperView is an anamorphic stretch no model can fit,
    # which produced ~30px RMS for runs started without loading a preset.
    webcam_resolution: int = Field(default=12, ge=0)  # 12 = 1080p
    webcam_fov: int = Field(default=0, ge=0)  # 0 = Wide (native fisheye)
    webcam_port: int = Field(default=8554, ge=1, le=65535)
    webcam_protocol: Literal["RTSP", "TS"] = "TS"
    webcam_digital_lens: int | None = None
    video_lens: int | None = None
    video_resolution: int | None = None
    video_fps: int | None = None
    video_aspect_ratio: int | None = None
    video_framing: int | None = None
    system_video_mode: int | None = None
    video_bit_rate: int | None = None
    profile: int | None = None
    max_lens_mod: int | None = None

    @field_validator("base_url")
    @classmethod
    def clean_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")


class CaptureConfig(BaseModel):
    target_samples: int = Field(default=90, ge=1, le=500)
    max_samples: int = Field(default=200, ge=1, le=2000)
    min_markers: int = Field(default=8, ge=1)
    max_motion_px: float = Field(default=1.5, ge=0.0)
    min_param_dist: float = Field(default=0.11, ge=0.0)
    capture_cooldown_s: float = Field(default=1.2, ge=0.0)
    warmup_s: float = Field(default=2.0, ge=0.0)
    auto_capture: bool = True


CalibModelName = Literal[
    "plumb_bob", "rational_polynomial", "fisheye", "double_sphere", "kannala_brandt"
]


class SolverConfig(BaseModel):
    min_markers: int = Field(default=8, ge=1)
    min_frames: int = Field(default=25, ge=3)
    rectify_alpha: float = Field(default=0.0, ge=0.0, le=1.0)
    auto_select: bool = True
    max_view_error_px: float = Field(default=2.5, gt=0.0)
    outlier_mad_multiplier: float = Field(default=3.5, gt=0.0)
    min_selected_frames: int | None = None
    max_selected_frames: int = Field(default=50, ge=3)
    selection_passes: int = Field(default=2, ge=1, le=10)
    # Which camera models to solve and emit. plumb_bob/rational_polynomial are
    # pinhole (good only for Linear/narrow lenses); fisheye is Kannala-Brandt for
    # wide GoPro lenses (Wide ~130 deg). double_sphere covers ultra-wide fisheye
    # (Max Lens Mod, ~150-195 deg) and kannala_brandt is OpenICC's FISHEYE, the
    # file UMI loads (gopro_intrinsics_2_7k.json layout). Both are solved by the
    # external OpenICC backend (the Docker image setup-openicc builds, or OPENICC_BINARY) — opt-in
    # via preset/UI, not in the defaults so an un-preset'd run has no external deps.
    # Default emits the three built-ins so a run started without a preset
    # auto-recommends the right model for whatever lens was used.
    models: list[CalibModelName] = Field(
        default_factory=lambda: ["plumb_bob", "rational_polynomial", "fisheye"]
    )

    @field_validator("models")
    @classmethod
    def dedupe_models(cls, value: list[str]) -> list[str]:
        ordered = list(dict.fromkeys(value))
        if not ordered:
            raise ValueError("at least one calibration model is required")
        return ordered


class CoverageTargets(BaseModel):
    x_min: float = Field(default=0.20, ge=0.0, le=1.0)
    x_max: float = Field(default=0.80, ge=0.0, le=1.0)
    y_min: float = Field(default=0.20, ge=0.0, le=1.0)
    y_max: float = Field(default=0.80, ge=0.0, le=1.0)
    size_min: float = Field(default=0.18, ge=0.0, le=1.0)
    size_max: float = Field(default=0.55, ge=0.0, le=1.0)
    skew_max: float = Field(default=0.50, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_ranges(self) -> CoverageTargets:
        if self.x_min >= self.x_max:
            raise ValueError("x_min must be smaller than x_max")
        if self.y_min >= self.y_max:
            raise ValueError("y_min must be smaller than y_max")
        if self.size_min >= self.size_max:
            raise ValueError("size_min must be smaller than size_max")
        return self


class AppConfig(BaseModel):
    camera: CameraConfig = Field(default_factory=CameraConfig)
    gopro: GoProSettingsConfig = Field(default_factory=GoProSettingsConfig)
    board: BoardConfig = Field(default_factory=BoardConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    solver: SolverConfig = Field(default_factory=SolverConfig)
    coverage_targets: CoverageTargets = Field(default_factory=CoverageTargets)


class StartRequest(BaseModel):
    config: AppConfig = Field(default_factory=AppConfig)
    runs_dir: Path | None = None


class SolveFramesRequest(BaseModel):
    frames_dir: Path
    output_dir: Path
    camera: CameraConfig = Field(default_factory=CameraConfig)
    board: BoardConfig = Field(default_factory=BoardConfig)
    solver: SolverConfig = Field(default_factory=SolverConfig)


def caib_marker_count(cols: int, rows: int) -> int:
    total = 0
    for row in range(rows):
        total += len(range(0, cols, 2) if row % 2 == 0 else range(1, cols, 2))
    return total
