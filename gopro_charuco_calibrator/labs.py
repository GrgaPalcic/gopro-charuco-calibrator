"""GoPro Labs QR codes and the settings checklist for recording the calibration clip.

Every code comes from the GoPro Labs docs (gopro.github.io/labs: control/settings, the
custom QR creator's source, and the release notes). The QR creator builds a command in
this order: mode, resolution, frame rate, HyperSmooth, "!N", lens, "t" (Protune), then
the Protune fields, with the ISO cap and shutter as ``i<max>S<angle>``.

- ``mV`` video mode; ``r4T`` 4K 4:3; ``p60`` 60 fps; ``e0`` HyperSmooth Off;
- ``oX2`` Max Lens Mod 2.0; ``oX10`` auto-detect lens mods (Labs 1.12.70). The Labs
  notes also list ``oX3`` for Max Lens Mod 2.5 (HERO13 setting 189 = 3, almost
  certainly the Ultra Wide Lens Mod), so ``oX3fX`` may be the direct counterpart of
  ``oX2fX``; ``oX10`` stays the default for AEWAL-001 until a camera has scanned both;
- ``fX`` is the creator's "Enable MSV" (Max SuperView) with ``oX2``, for HERO12-13.
  On a HERO13 at 4:3 the mod's widest lens is called Ultra Wide; that ``fX`` selects
  it is not documented, so it is marked unverified;
- ``i16`` ISO max 1600; ``S45`` a 45 degree shutter angle (1/480 s at 60 fps); ``S0``
  automatic shutter.

The calibration code locks the shutter; the dataset code is the same with the shutter
back on Auto.
"""

from __future__ import annotations

import base64
from typing import Any

import cv2

from .models import RecordingConfig

# Labs resolution codes for the sizes this route supports.
RESOLUTION_CODES = {(4000, 3000): "r4T"}
HYPERSMOOTH_CODES = {"off": "e0"}
# The frame rates a HERO13 offers at 4K 4:3 with a lens mod (the creator's p codes).
FPS_CODES = (24, 25, 30, 50, 60)
# The creator's "Lock Shutter" angles in degrees, as it labels them; S0 is Auto.
SHUTTER_ANGLE_CODES = (360, 180, 90, 45, 22, 10, 5, 2)


def _labs_int(value: float, allowed: tuple[int, ...], what: str) -> int:
    code = round(value)
    if abs(value - code) > 1e-6 or code not in allowed:
        raise ValueError(
            f"No Labs code for a {what} of {value:g}; use one of "
            + ", ".join(str(a) for a in allowed)
        )
    return code


def labs_command(rec: RecordingConfig, *, calibration: bool) -> str:
    """The Labs QR command for the calibration clip, or for the dataset (shutter Auto)."""
    resolution = RESOLUTION_CODES.get((rec.width, rec.height))
    if resolution is None:
        raise ValueError(f"No Labs code for {rec.width}x{rec.height}; only 4000x3000 (4K 4:3)")
    fps = f"p{_labs_int(rec.fps, FPS_CODES, 'frame rate')}"
    iso = f"i{rec.iso_max // 100}"
    angle = _labs_int(rec.shutter_angle_deg, SHUTTER_ANGLE_CODES, "shutter angle")
    shutter = f"S{angle}" if calibration else "S0"
    return (
        f"mV{resolution}{fps}{HYPERSMOOTH_CODES[rec.hypersmooth]}!N"
        f"{rec.labs_lens_mod_code}{rec.labs_lens_code}t{iso}{shutter}"
    )


def unverified_codes(rec: RecordingConfig, *, calibration: bool) -> list[dict[str, str]]:
    """Codes the docs do not confirm for this camera; the UI shows them as unverified."""
    notes = [
        {
            "code": rec.labs_lens_code,
            "note": f"{rec.labs_lens_code} is the Labs code for Max SuperView (listed for "
            f"HERO12-13). That it gives the {rec.lens} lens at 4K 4:3 on a HERO13 is not "
            "documented. Check the lens on the camera screen.",
        }
    ]
    if rec.labs_lens_mod_code == "oX10":
        notes.append(
            {
                "code": "oX10",
                "note": "Turns on lens-mod auto detection. How it combines with the lens "
                "code is not documented. Check the camera shows the lens mod.",
            }
        )
        notes.append(
            {
                "code": "oX3",
                "note": "An alternative, also unverified: the HERO13 Labs notes list oX3 "
                "for Max Lens Mod 2.5, which is probably this lens mod, so oX3fX may be the "
                "direct counterpart of the Max Lens Mod 2.0 code oX2fX. This code uses "
                "oX10 until one of them is scanned on a camera.",
            }
        )
    if calibration:
        notes.append(
            {
                "code": f"S{round(rec.shutter_angle_deg)}",
                "note": f"Should lock the shutter at {rec.calibration_shutter} s. Not yet "
                "confirmed on a HERO13 screen. Check the shutter shows "
                f"{rec.calibration_shutter}.",
            }
        )
    return notes


def qr_png_data_uri(text: str, scale: int = 8, border: int = 4) -> str:
    """A QR code of ``text`` as a PNG data URI (cv2.QRCodeEncoder, no extra dependency)."""
    matrix = cv2.QRCodeEncoder.create().encode(text)
    size = matrix.shape[0]
    image = cv2.resize(matrix, (size * scale, size * scale), interpolation=cv2.INTER_NEAREST)
    image = cv2.copyMakeBorder(
        image, border * scale, border * scale, border * scale, border * scale,
        cv2.BORDER_CONSTANT, value=255,
    )
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("Could not encode the QR code image")
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def checklist(rec: RecordingConfig) -> list[dict[str, str]]:
    """The settings to check on the camera, by GoPro's setting names."""
    if rec.lens_mod == "ADWAL-002":
        mod_how = (
            f"Select {rec.lens_mod_name} on the camera. It is not detected automatically. "
            "The QR code sets it too."
        )
    else:
        mod_how = (
            f"Detected automatically when fitted. Check the camera shows the "
            f"{rec.lens_mod_name}."
        )
    return [
        {"setting": "Lens mod", "value": rec.lens_mod_name, "how": mod_how},
        {"setting": "Mode", "value": "Video", "how": "Use a video preset."},
        {
            "setting": "Resolution",
            "value": "4K, aspect ratio 4:3",
            "how": "With the lens mod, 4:3 is only available at 4K.",
        },
        {
            "setting": "Frame rate",
            "value": f"{rec.fps:g} fps",
            "how": "Set it in the video preset.",
        },
        {"setting": "Lens", "value": rec.lens, "how": "The widest lens with the mod at 4:3."},
        {
            "setting": "HyperSmooth",
            "value": "Off",
            "how": "Stabilisation moves the picture differently in every frame.",
        },
        {"setting": "Protune", "value": "On", "how": "Needed to set the shutter and ISO."},
        {
            "setting": "Shutter",
            "value": f"{rec.calibration_shutter} for the calibration clip, Auto for the dataset",
            "how": "A fast shutter keeps the board sharp while it moves. Set it back to Auto "
            "after the calibration clip (scan the dataset QR code).",
        },
        {
            "setting": "ISO max",
            "value": str(rec.iso_max),
            "how": "The highest ISO the camera may use: higher is brighter but noisier.",
        },
    ]


def labs_payload(rec: RecordingConfig) -> dict[str, Any]:
    """Both QR codes, their unverified parts, and the checklist."""
    payload: dict[str, Any] = {}
    for kind, calibration in (("calibration", True), ("dataset", False)):
        code = labs_command(rec, calibration=calibration)
        payload[kind] = {
            "code": code,
            "png": qr_png_data_uri(code),
            "unverified": unverified_codes(rec, calibration=calibration),
        }
    payload["checklist"] = checklist(rec)
    payload["lens_mod"] = {"code": rec.lens_mod, "name": rec.lens_mod_name}
    return payload
