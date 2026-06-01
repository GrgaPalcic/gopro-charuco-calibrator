from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from .models import GoProSettingsConfig

SETTING_DEFS: dict[str, dict[str, Any]] = {
    "webcam_resolution": {
        "title": "Webcam Resolution",
        "options": {4: "480p", 7: "720p", 12: "1080p"},
    },
    "webcam_fov": {
        "title": "Webcam FOV",
        "options": {0: "Wide", 2: "Narrow", 3: "SuperView", 4: "Linear"},
    },
    "webcam_digital_lens": {
        "setting_id": 43,
        "title": "Webcam Digital Lens",
        "options": {0: "Wide", 2: "Narrow", 3: "SuperView", 4: "Linear"},
    },
    "video_lens": {
        "setting_id": 121,
        "title": "Video Lens",
        "options": {
            0: "Wide",
            2: "Narrow",
            3: "SuperView",
            4: "Linear",
            8: "Linear + Horizon Leveling",
            9: "HyperView",
            10: "Linear + Horizon Lock",
            12: "Ultra SuperView",
            13: "Ultra Wide",
            14: "Ultra Linear",
            104: "Ultra HyperView",
        },
    },
    "video_resolution": {
        "setting_id": 2,
        "title": "Video Resolution",
        "options": {
            1: "4K",
            4: "2.7K",
            9: "1080",
            12: "720",
            35: "5.3K 21:9",
            36: "4K 21:9",
            37: "4K 1:1",
            100: "5.3K",
            107: "5.3K 8:7 V2",
            108: "4K 8:7 V2",
            109: "4K 9:16 V2",
            110: "1080 9:16 V2",
            111: "2.7K 4:3 V2",
            112: "4K 4:3 V2",
            113: "5.3K 4:3 V2",
        },
    },
    "video_fps": {
        "setting_id": 3,
        "title": "Frames Per Second",
        "options": {
            0: "240",
            1: "120",
            2: "100",
            5: "60",
            6: "50",
            8: "30",
            9: "25",
            10: "24",
            13: "200",
        },
    },
    "video_aspect_ratio": {
        "setting_id": 108,
        "title": "Video Aspect Ratio",
        "options": {0: "4:3", 1: "16:9", 3: "8:7", 4: "9:16", 5: "21:9", 6: "1:1"},
    },
    "video_framing": {
        "setting_id": 232,
        "title": "Video Framing",
        "options": {0: "4:3", 1: "16:9", 3: "8:7", 4: "9:16", 5: "21:9", 6: "1:1"},
    },
    "system_video_mode": {
        "setting_id": 180,
        "title": "System Video Mode",
        "options": {
            0: "Highest Quality",
            111: "Standard Quality",
            112: "Basic Quality",
        },
    },
    "video_bit_rate": {
        "setting_id": 182,
        "title": "Video Bit Rate",
        "options": {0: "Standard", 1: "High", 2: "Max"},
    },
    "profile": {
        "setting_id": 184,
        "title": "Profile",
        "options": {0: "Standard", 1: "HDR", 2: "Log", 101: "HLG HDR"},
    },
    "max_lens_mod": {
        "setting_id": 189,
        "title": "Max Lens Mod",
        "options": {
            0: "None",
            2: "Max Lens 2.0",
            3: "Max Lens 2.5",
            4: "Macro",
            5: "Anamorphic",
            6: "ND 4",
            7: "ND 8",
            8: "ND 16",
            9: "ND 32",
            10: "Standard Lens",
            100: "Auto Detect",
        },
    },
}

CAMERA_SETTING_FIELDS = [
    "webcam_digital_lens",
    "max_lens_mod",
    "system_video_mode",
    "video_aspect_ratio",
    "video_framing",
    "video_resolution",
    "video_fps",
    "video_lens",
    "video_bit_rate",
    "profile",
]


@dataclass(frozen=True)
class GoProRequestResult:
    path: str
    ok: bool
    response: Any = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "response": self.response,
            "error": self.error,
        }


class GoProClient:
    def __init__(self, base_url: str, timeout_s: float = 4.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def get(self, path: str, query: dict[str, Any] | None = None) -> GoProRequestResult:
        suffix = path
        if query:
            suffix = f"{suffix}?{urlencode(query)}"
        url = f"{self.base_url}{suffix}"
        try:
            with urlopen(url, timeout=self.timeout_s) as response:
                raw = response.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    payload = raw
                return GoProRequestResult(path=suffix, ok=True, response=payload)
        except (OSError, URLError) as exc:
            return GoProRequestResult(path=suffix, ok=False, error=str(exc))

    def change_setting(self, setting_id: int, option: int) -> GoProRequestResult:
        return self.get(
            "/gopro/camera/setting",
            {"setting": int(setting_id), "option": int(option)},
        )

    def stop_webcam(self) -> GoProRequestResult:
        return self.get("/gopro/webcam/stop")

    def start_webcam(
        self,
        *,
        resolution: int,
        fov: int,
        port: int,
        protocol: str,
    ) -> GoProRequestResult:
        return self.get(
            "/gopro/webcam/start",
            {
                "res": int(resolution),
                "fov": int(fov),
                "port": int(port),
                "protocol": protocol,
            },
        )

    def webcam_status(self) -> GoProRequestResult:
        return self.get("/gopro/webcam/status")


def gopro_options_for_ui() -> dict[str, Any]:
    return SETTING_DEFS


def apply_gopro_settings(config: GoProSettingsConfig) -> dict[str, Any]:
    if not config.enabled:
        return {"enabled": False, "ok": True, "steps": []}
    if not config.base_url:
        return {
            "enabled": True,
            "ok": False,
            "steps": [],
            "error": "GoPro base URL is required when GoPro control is enabled.",
        }

    client = GoProClient(config.base_url)
    steps: list[dict[str, Any]] = []
    if config.stop_webcam_first:
        steps.append(client.stop_webcam().as_dict())

    for field in CAMERA_SETTING_FIELDS:
        value = getattr(config, field)
        if value is None:
            continue
        setting_id = SETTING_DEFS[field].get("setting_id")
        if setting_id is None:
            continue
        steps.append(client.change_setting(int(setting_id), int(value)).as_dict())

    if config.start_webcam:
        steps.append(
            client.start_webcam(
                resolution=config.webcam_resolution,
                fov=config.webcam_fov,
                port=config.webcam_port,
                protocol=config.webcam_protocol,
            ).as_dict()
        )
        steps.append(client.webcam_status().as_dict())

    return {
        "enabled": True,
        "base_url": config.base_url,
        "ok": all(step["ok"] for step in steps),
        "steps": steps,
    }
