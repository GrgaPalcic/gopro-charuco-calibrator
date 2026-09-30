"""A simulated fisheye camera looking at the calib.io board, for tests and screenshots.

It renders the printed board through a Double Sphere camera (Usenko et al. 2018), black
outside the lens circle, so the app's own detector and solver can run on the frames.
The default camera is the HERO13 + Max Lens Mod 2.0 USB-webcam Wide 1080p solve measured
on 2026-06-12. ``recording_camera`` gives a 4:3 camera for clip tests; its intrinsics
are made up for the test (a plausible ultra-wide fisheye), not a measurement.
"""

from __future__ import annotations

import cv2
import cv2.aruco as aruco
import numpy as np

from .boards import make_caib_board, resolve_dictionary
from .detection import detect_markers

SIZE = (1920, 1080)
# HERO13 + Max Lens Mod 2.0, USB webcam Wide 1080p, OpenICC solve of 2026-06-12.
DS = {"f": 629.19, "cx": 949.64, "cy": 538.89, "xi": 0.00166, "alpha": 0.708}
LENS_HALF_ANGLE = np.radians(83.5)  # 167 deg clean fisheye
TEXTURE_PX_PER_M = 4000.0


def ds_unproject(u, v, ds=None):
    """Double Sphere pixel -> unit ray (Usenko et al. 2018, eq. 47-51)."""
    ds = ds or DS
    f, cx, cy, xi, alpha = ds["f"], ds["cx"], ds["cy"], ds["xi"], ds["alpha"]
    mx, my = (u - cx) / f, (v - cy) / f
    r2 = mx * mx + my * my
    valid = r2 <= 1.0 / (2.0 * alpha - 1.0) if alpha > 0.5 else np.ones_like(r2, bool)
    r2c = np.where(valid, r2, 0.0)
    mz = (1 - alpha * alpha * r2c) / (
        alpha * np.sqrt(np.maximum(1 - (2 * alpha - 1) * r2c, 0)) + 1 - alpha
    )
    k = (mz * xi + np.sqrt(mz * mz + (1 - xi * xi) * r2c)) / (mz * mz + r2c)
    ray = np.stack([k * mx, k * my, k * mz - xi], axis=-1)
    ray /= np.linalg.norm(ray, axis=-1, keepdims=True)
    return ray, valid


def board_texture(board_config):
    """The printed board: markers on alternating cells, black squares between."""
    sq = board_config.square_m * TEXTURE_PX_PER_M
    width = int(round(board_config.cols * sq))
    height = int(round(board_config.rows * sq))
    tex = np.full((height, width), 255, np.uint8)
    _board, obj_by_id = make_caib_board(board_config)
    marker_cells = set()
    dictionary = resolve_dictionary(board_config.aruco_dict)
    side = int(round(board_config.marker_m * TEXTURE_PX_PER_M))
    for marker_id, corners in obj_by_id.items():
        x0, y0 = corners[0][0] * TEXTURE_PX_PER_M, corners[0][1] * TEXTURE_PX_PER_M
        marker_cells.add((int(y0 // sq), int(x0 // sq)))
        image = aruco.generateImageMarker(dictionary, int(marker_id), side)
        tex[int(round(y0)) : int(round(y0)) + side, int(round(x0)) : int(round(x0)) + side] = image
    parity = next(iter(marker_cells))
    for row in range(board_config.rows):
        for col in range(board_config.cols):
            if (row, col) in marker_cells or (row + col) % 2 == sum(parity) % 2:
                continue
            tex[int(row * sq) : int((row + 1) * sq), int(col * sq) : int((col + 1) * sq)] = 0
    return tex


class Camera:
    def __init__(self, board_config, size=SIZE, ds=None, half_angle=LENS_HALF_ANGLE):
        self.board_config = board_config
        self.size = (int(size[0]), int(size[1]))
        self.ds = ds or DS
        self.texture = board_texture(board_config)
        width, height = self.size
        u, v = np.meshgrid(
            np.arange(width, dtype=np.float64), np.arange(height, dtype=np.float64)
        )
        self.rays, valid = ds_unproject(u, v, self.ds)
        self.inside = valid & (np.arccos(np.clip(self.rays[..., 2], -1, 1)) < half_angle)
        yy, xx = np.mgrid[0:height, 0:width]
        self.room = (38 + 22 * (yy / height) + 6 * np.sin(xx / 97.0)).astype(np.uint8)
        self.center = np.asarray(
            [board_config.pattern_width_m / 2, board_config.pattern_height_m / 2, 0.0]
        )

    def render_gray(self, rotation, tvec):
        origin_b = rotation.T @ (-tvec) + self.center
        dirs_b = self.rays @ rotation  # (R^T d) for every ray
        with np.errstate(divide="ignore", invalid="ignore"):
            s = -origin_b[2] / dirs_b[..., 2]
        hit = origin_b[:2] + s[..., None] * dirs_b[..., :2]
        map_x = (hit[..., 0] * TEXTURE_PX_PER_M).astype(np.float32)
        map_y = (hit[..., 1] * TEXTURE_PX_PER_M).astype(np.float32)
        map_x[~(s > 0)] = -1
        board = cv2.remap(
            self.texture,
            map_x,
            map_y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        on_board = (
            cv2.remap(
                np.full_like(self.texture, 255),
                map_x,
                map_y,
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            > 127
        )
        gray = np.where(on_board, board, self.room)
        gray = np.where(self.inside, gray, 0).astype(np.uint8)
        return cv2.GaussianBlur(gray, (3, 3), 0.6)

    def render(self, rotation, tvec):
        return cv2.cvtColor(self.render_gray(rotation, tvec), cv2.COLOR_GRAY2BGR)

    def pose_towards(self, u, v, distance, tilt):
        return pose_towards(u, v, distance, tilt, self.ds)


def recording_camera(board_config, size=(1600, 1200)):
    """A 4:3 ultra-wide fisheye filling the frame (made-up intrinsics, for tests)."""
    width, height = size
    ds = {
        "f": 0.30 * width,
        "cx": width / 2 - 0.5,
        "cy": height / 2 - 0.5,
        "xi": 0.0,
        "alpha": 0.6,
    }
    return Camera(board_config, size=size, ds=ds, half_angle=np.radians(90.0))


def rotation_between(a, b):
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3)
    vx = np.asarray([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1 / (1 + c))


def pose_towards(u, v, distance, tilt, ds=None):
    """Board rotation and translation so its centre sits on pixel (u, v)."""
    ray, _ = ds_unproject(np.asarray([u], float), np.asarray([v], float), ds)
    ray = ray[0]
    base = rotation_between(np.asarray([0.0, 0.0, 1.0]), ray)
    tilt_r, _ = cv2.Rodrigues(np.asarray(tilt, dtype=np.float64))
    return base @ tilt_r, distance * ray


def synthetic_views(camera, count, rng):
    """Board poses spread over the whole image, near and far, some tilted."""
    views = []
    width, height = camera.size
    dictionary = resolve_dictionary(camera.board_config.aruco_dict)
    while len(views) < count:
        u = rng.uniform(0.12, 0.88) * width
        v = rng.uniform(0.12, 0.88) * height
        rotation, tvec = camera.pose_towards(
            u, v, rng.uniform(0.2, 1.0), rng.uniform([-0.55, -0.55, -0.4], [0.55, 0.55, 0.4])
        )
        frame = camera.render(rotation, tvec)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        detection = detect_markers(gray, camera.size, camera.board_config, dictionary)
        if detection is not None and detection.marker_count >= 12:
            views.append((frame, detection))
    return views
