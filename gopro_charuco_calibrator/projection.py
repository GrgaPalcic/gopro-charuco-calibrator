"""Camera models evaluated in numpy, to compare calibrations by the rays they map.

Double Sphere parameters are not unique (f, xi and alpha trade off at the same
error), so two calibrations are compared by projecting the same 3D rays through
both and measuring the pixel distance, never by their numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np


def project_double_sphere(
    points: np.ndarray, fx: float, fy: float, cx: float, cy: float, xi: float, alpha: float
) -> tuple[np.ndarray, np.ndarray]:
    """Double Sphere projection (Usenko et al. 2018, eq. 40-45). Returns pixels, valid."""
    x, y, z = points[:, 0], points[:, 1], points[:, 2]
    d1 = np.sqrt(x * x + y * y + z * z)
    zeta = xi * d1 + z
    d2 = np.sqrt(x * x + y * y + zeta * zeta)
    denom = alpha * d2 + (1 - alpha) * zeta
    w1 = alpha / (1 - alpha) if alpha <= 0.5 else (1 - alpha) / alpha
    w2 = (w1 + xi) / np.sqrt(2 * w1 * xi + xi * xi + 1)
    valid = z > -w2 * d1
    with np.errstate(divide="ignore", invalid="ignore"):
        pixels = np.stack([fx * x / denom + cx, fy * y / denom + cy], axis=1)
    return pixels, valid


def unproject_double_sphere(
    pixels: np.ndarray, fx: float, fy: float, cx: float, cy: float, xi: float, alpha: float
) -> tuple[np.ndarray, np.ndarray]:
    """Double Sphere unprojection (Usenko et al. 2018, eq. 46-51). Returns unit rays, valid."""
    mx = (pixels[:, 0] - cx) / fx
    my = (pixels[:, 1] - cy) / fy
    r2 = mx * mx + my * my
    valid = np.ones(len(pixels), dtype=bool) if alpha <= 0.5 else r2 <= 1 / (2 * alpha - 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        mz = (1 - alpha * alpha * r2) / (alpha * np.sqrt(1 - (2 * alpha - 1) * r2) + 1 - alpha)
        scale = (mz * xi + np.sqrt(mz * mz + (1 - xi * xi) * r2)) / (mz * mz + r2)
        rays = np.stack([scale * mx, scale * my, scale * mz - xi], axis=1)
        rays /= np.linalg.norm(rays, axis=1, keepdims=True)
    valid &= np.isfinite(rays).all(axis=1)
    return rays, valid


def project_kannala_brandt(
    points: np.ndarray, fx: float, fy: float, cx: float, cy: float, k: Sequence[float]
) -> np.ndarray:
    """Kannala-Brandt with four coefficients on theta, as cv2.fisheye and OpenICC's FISHEYE."""
    x, y, z = points[:, 0], points[:, 1], points[:, 2]
    r = np.sqrt(x * x + y * y)
    theta = np.arctan2(r, z)
    t2 = theta * theta
    theta_d = theta * (1 + k[0] * t2 + k[1] * t2**2 + k[2] * t2**3 + k[3] * t2**4)
    with np.errstate(invalid="ignore", divide="ignore"):
        scale = np.where(r > 1e-12, theta_d / r, 1.0)
    return np.stack([fx * x * scale + cx, fy * y * scale + cy], axis=1)


def rays_to_angle(max_angle_deg: float, rings: int = 36, spokes: int = 48) -> np.ndarray:
    """Unit rays on rings from the optical axis out to ``max_angle_deg``."""
    theta = np.radians(np.linspace(0.0, max_angle_deg, rings))
    phi = np.radians(np.linspace(0.0, 360.0, spokes, endpoint=False))
    t_grid, p_grid = np.meshgrid(theta, phi)
    return np.stack(
        [np.sin(t_grid) * np.cos(p_grid), np.sin(t_grid) * np.sin(p_grid), np.cos(t_grid)], -1
    ).reshape(-1, 3)


def _ds_params(result: dict[str, Any]) -> tuple[float, ...]:
    matrix = result["camera_matrix"]
    xi, alpha = result["distortion"]
    return matrix[0][0], matrix[1][1], matrix[0][2], matrix[1][2], xi, alpha


def board_reach_deg(ds_result: dict[str, Any], pixels: np.ndarray) -> float | None:
    """Largest off-axis angle any detected corner reached, read through the Double Sphere model."""
    if len(pixels) == 0:
        return None
    rays, valid = unproject_double_sphere(pixels, *_ds_params(ds_result))
    if not valid.any():
        return None
    return float(np.degrees(np.arccos(np.clip(rays[valid, 2], -1.0, 1.0))).max())


def kb_vs_double_sphere_px(
    kb_result: dict[str, Any], ds_result: dict[str, Any], max_angle_deg: float
) -> float:
    """Worst pixel distance between the two models over rays out to ``max_angle_deg``.

    UMI loads Kannala-Brandt with fy = fx (it ignores aspect_ratio), so the KB side
    is evaluated the way UMI will use it.
    """
    rays = rays_to_angle(max_angle_deg)
    ds_pixels, valid = project_double_sphere(rays, *_ds_params(ds_result))
    matrix = kb_result["camera_matrix"]
    kb_pixels = project_kannala_brandt(
        rays, matrix[0][0], matrix[0][0], matrix[0][2], matrix[1][2], kb_result["distortion"]
    )
    return float(np.linalg.norm(kb_pixels[valid] - ds_pixels[valid], axis=1).max())
