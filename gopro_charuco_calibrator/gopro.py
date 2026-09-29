from __future__ import annotations

import json
import os
import subprocess
import time
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
    # SuperView/HyperView modes are an anamorphic 16:9 stretch that no camera
    # model fits (~30px RMS measured); the labels say so to stop them being picked.
    "webcam_fov": {
        "title": "Webcam FOV",
        "options": {
            0: "Wide",
            2: "Narrow",
            3: "SuperView (anamorphic, uncalibratable)",
            4: "Linear",
        },
    },
    "webcam_digital_lens": {
        "setting_id": 43,
        "title": "Webcam Digital Lens",
        "options": {
            0: "Wide",
            2: "Narrow",
            3: "SuperView (anamorphic, uncalibratable)",
            4: "Linear",
        },
    },
    "video_lens": {
        "setting_id": 121,
        "title": "Video Lens",
        "options": {
            0: "Wide",
            2: "Narrow",
            3: "SuperView (anamorphic, uncalibratable)",
            4: "Linear",
            7: "Max SuperView",
            8: "Linear + Horizon Leveling",
            9: "HyperView (anamorphic, uncalibratable)",
            10: "Linear + Horizon Lock",
            11: "Max HyperView (anamorphic, uncalibratable)",
            12: "Ultra SuperView",
            13: "Ultra Wide",
            14: "Ultra Linear",
            104: "Ultra HyperView (anamorphic, uncalibratable)",
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
            1: "Max Lens 1.0",
            2: "Max Lens 2.0",
            3: "Max Lens 2.5",
            4: "Macro",
            5: "Anamorphic",
            6: "ND 4",
            7: "ND 8",
            8: "ND 16",
            9: "ND 32",
            10: "Standard Lens",
            11: "ND 64",
            100: "Auto Detect",
        },
    },
    "hypersmooth": {
        "setting_id": 135,
        "title": "HyperSmooth",
        "options": {0: "Off", 1: "Low", 2: "High", 3: "Boost", 4: "Auto Boost", 100: "Standard"},
    },
}

# Settings read back from /gopro/camera/state after the webcam starts, so a run
# records what the camera actually applied rather than only what was requested.
REPORTED_SETTING_FIELDS = ["webcam_digital_lens", "max_lens_mod", "hypersmooth"]

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


class LegacyGoProClient(GoProClient):
    def start_webcam(
        self,
        *,
        resolution: int,
        fov: int,
        port: int,
        protocol: str,
    ) -> GoProRequestResult:
        resolution_value = {4: 480, 7: 720, 12: 1080}.get(int(resolution), int(resolution))
        return self.get(
            "/gp/gpWebcam/START",
            {
                "res": int(resolution_value),
                "port": int(port),
            },
        )

    def set_webcam_fov(self, fov: int) -> GoProRequestResult:
        return self.get("/gp/gpWebcam/SETTINGS", {"fov": int(fov)})

    def stop_webcam(self) -> GoProRequestResult:
        return self.get("/gp/gpWebcam/STOP")

    def webcam_status(self) -> GoProRequestResult:
        return self.get("/gp/gpWebcam/STATUS")


def gopro_options_for_ui() -> dict[str, Any]:
    return SETTING_DEFS


def _setting_label(field: str, value: Any) -> Any:
    if value is None:
        return None
    options = SETTING_DEFS.get(field, {}).get("options", {})
    label = options.get(value)
    return f"{label} ({value})" if label else value


def read_camera_state(client: GoProClient) -> dict[str, Any]:
    """Read the lens-defining settings the camera reports, for the run record.

    Never raises and never fails the preview: an unreachable or unexpected
    /gopro/camera/state (e.g. a legacy-only camera) just yields ok=False.
    """
    result = client.get("/gopro/camera/state")
    settings = result.response.get("settings") if isinstance(result.response, dict) else None
    if not result.ok or not isinstance(settings, dict):
        return {"ok": False, "error": result.error or "no settings in /gopro/camera/state"}
    reported: dict[str, int] = {}
    for field in REPORTED_SETTING_FIELDS:
        value = settings.get(str(SETTING_DEFS[field]["setting_id"]))
        if isinstance(value, int) and not isinstance(value, bool):
            reported[field] = value
    labels = {field: str(_setting_label(field, value)) for field, value in reported.items()}
    return {"ok": True, "settings": reported, "labels": labels}


def camera_state_warnings(config: GoProSettingsConfig, camera_state: dict[str, Any]) -> list[str]:
    """Mismatches between the requested lens mode and what the camera reports."""
    reported = camera_state.get("settings") or {}
    warnings: list[str] = []
    lens = reported.get("webcam_digital_lens")
    if lens is not None and lens != config.webcam_fov:
        warnings.append(
            f"camera reports webcam lens {_setting_label('webcam_digital_lens', lens)}, "
            f"requested {_setting_label('webcam_fov', config.webcam_fov)}; check the preview"
        )
    mod = reported.get("max_lens_mod")
    if config.max_lens_mod is not None and mod is not None and mod != config.max_lens_mod:
        warnings.append(
            f"camera reports lens mod {_setting_label('max_lens_mod', mod)}, "
            f"requested {_setting_label('max_lens_mod', config.max_lens_mod)}"
        )
    return warnings


def describe_acquisition_mode(
    config: Any, gopro_result: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Human-readable record of the exact capture mode, for reproducible datasets.

    Intrinsics are only valid for the mode used during calibration, so this is
    saved alongside the run config and the solve summary, and shown on solve, so
    later dataset recording can use the identical lens, resolution, and stream.
    With ``gopro_result``, the settings the camera reported back are added as
    ``reported_<field>``.
    """
    camera = config.camera
    gopro = config.gopro
    mode: dict[str, Any] = {
        "camera_name": camera.camera_name,
        "frame_size": f"{int(camera.width)}x{int(camera.height)}",
        "fps": camera.fps,
        "fourcc": camera.fourcc,
        "source": "gopro_webcam" if gopro.enabled else "v4l2_device",
    }
    if gopro.enabled:
        mode["lens_fov"] = _setting_label("webcam_fov", gopro.webcam_fov)
        mode["webcam_resolution"] = _setting_label("webcam_resolution", gopro.webcam_resolution)
        mode["protocol"] = gopro.webcam_protocol
        mode["port"] = gopro.webcam_port
        for field in (
            "webcam_digital_lens",
            "video_lens",
            "video_resolution",
            "video_fps",
            "video_framing",
            "max_lens_mod",
        ):
            value = getattr(gopro, field)
            if value is not None:
                mode[field] = _setting_label(field, value)
        camera_state = (gopro_result or {}).get("camera_state") or {}
        for field, value in (camera_state.get("settings") or {}).items():
            mode[f"reported_{field}"] = _setting_label(field, value)
    else:
        mode["device"] = camera.device
    return mode


def _candidate_gopro_ips() -> list[str]:
    ips: list[str] = []
    env_ip = os.environ.get("GOPRO_IP", "").strip()
    if env_ip:
        ips.append(env_ip)
    try:
        output = subprocess.check_output(
            ["ip", "-o", "-4", "addr", "show"],
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        output = ""
    priority_ips: list[str] = []
    fallback_ips: list[str] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface = parts[1]
        if iface.startswith(("br-", "docker", "virbr", "waydroid")):
            continue
        local_ip = parts[3].split("/", 1)[0]
        octets = local_ip.split(".")
        if len(octets) != 4:
            continue
        if octets[0] != "172":
            continue
        # GoPro USB networking convention: host gets .52, camera answers on .51.
        candidate = ".".join([octets[0], octets[1], octets[2], "51"])
        if octets[3] == "52" or iface.startswith(("enx", "enp", "usb")):
            priority_ips.append(candidate)
        else:
            fallback_ips.append(candidate)
    ips.extend(priority_ips)
    ips.extend(fallback_ips)
    ips.append("172.20.144.51")

    unique: list[str] = []
    for ip in ips:
        if ip and ip not in unique:
            unique.append(ip)
    return unique


def discover_gopro_base_url(timeout_s: float = 0.8) -> dict[str, Any]:
    for ip in _candidate_gopro_ips():
        probes: list[dict[str, Any]] = []
        open_base_url = ""
        legacy_base_url = ""
        for base_url, api_style, path in [
            (f"http://{ip}:8080", "open_gopro", "/gopro/webcam/status"),
            (f"http://{ip}:8080", "legacy_gpwebcam", "/gp/gpWebcam/STATUS"),
            (f"http://{ip}", "open_gopro", "/gopro/webcam/status"),
            (f"http://{ip}", "legacy_gpwebcam", "/gp/gpWebcam/STATUS"),
        ]:
            client = GoProClient(base_url, timeout_s=timeout_s)
            result = client.get(path)
            probe = {
                "base_url": base_url,
                "api_style": api_style,
                **result.as_dict(),
            }
            probes.append(probe)
            if result.ok and api_style == "open_gopro" and not open_base_url:
                open_base_url = base_url
            if result.ok and api_style == "legacy_gpwebcam" and not legacy_base_url:
                legacy_base_url = base_url
        if open_base_url or legacy_base_url:
            base_url = open_base_url or legacy_base_url
            return {
                "ok": True,
                "base_url": base_url,
                "api_style": "open_gopro" if open_base_url else "legacy_gpwebcam",
                "open_base_url": open_base_url,
                "legacy_base_url": legacy_base_url,
                "probes": probes,
            }
    return {
        "ok": False,
        "base_url": "",
        "api_style": "",
        "open_base_url": "",
        "legacy_base_url": "",
        "probes": probes,
        "error": "Could not discover GoPro HTTP API on local 172.x.x.51 USB candidates.",
    }


def apply_gopro_settings(config: GoProSettingsConfig) -> dict[str, Any]:
    if not config.enabled:
        return {"enabled": False, "ok": True, "steps": []}
    discovery: dict[str, Any] | None = None
    base_url = config.base_url
    open_base_url = base_url
    legacy_base_url = base_url
    setting_api_style = "open_gopro"
    webcam_api_style = "legacy_gpwebcam" if config.start_video_bridge else "open_gopro"
    if not base_url:
        # A freshly connected GoPro may not answer the first probe, so retry the
        # discovery a few times before giving up (avoids a spurious first-click
        # failure).
        for attempt in range(4):
            discovery = discover_gopro_base_url()
            if discovery["ok"]:
                base_url = discovery["base_url"]
                open_base_url = discovery.get("open_base_url") or ""
                legacy_base_url = discovery.get("legacy_base_url") or ""
                if not open_base_url and discovery.get("api_style") == "open_gopro":
                    open_base_url = base_url
                if not legacy_base_url and discovery.get("api_style") == "legacy_gpwebcam":
                    legacy_base_url = base_url
                break
            if attempt < 3:
                time.sleep(0.5)
    if not base_url:
        return {
            "enabled": True,
            "ok": False,
            "steps": [],
            "discovery": discovery,
            "error": "GoPro HTTP API was not auto-discovered.",
        }

    setting_client: GoProClient | None
    if open_base_url:
        setting_client = GoProClient(open_base_url)
        setting_api_style = "open_gopro"
    else:
        setting_client = None
        setting_api_style = ""

    webcam_client: GoProClient
    if config.start_video_bridge and legacy_base_url:
        webcam_client = LegacyGoProClient(legacy_base_url)
        webcam_api_style = "legacy_gpwebcam"
    elif open_base_url:
        webcam_client = GoProClient(open_base_url)
        webcam_api_style = "open_gopro"
    else:
        webcam_client = LegacyGoProClient(legacy_base_url or base_url)
        webcam_api_style = "legacy_gpwebcam"

    steps: list[dict[str, Any]] = []
    if config.stop_webcam_first:
        steps.append(webcam_client.stop_webcam().as_dict())

    skipped_settings: list[dict[str, Any]] = []
    if setting_client is not None:
        for field in CAMERA_SETTING_FIELDS:
            if field == "webcam_digital_lens" and config.start_video_bridge:
                skipped_settings.append(
                    {
                        "field": field,
                        "reason": "webcam FOV is applied through the webcam stream endpoint",
                    }
                )
                continue
            value = getattr(config, field)
            if value is None:
                continue
            setting_id = SETTING_DEFS[field].get("setting_id")
            if setting_id is None:
                continue
            steps.append(setting_client.change_setting(int(setting_id), int(value)).as_dict())

    if config.start_webcam:
        steps.append(
            webcam_client.start_webcam(
                resolution=config.webcam_resolution,
                fov=config.webcam_fov,
                port=config.webcam_port,
                protocol=config.webcam_protocol,
            ).as_dict()
        )
        if isinstance(webcam_client, LegacyGoProClient):
            steps.append(webcam_client.set_webcam_fov(config.webcam_fov).as_dict())
        steps.append(webcam_client.webcam_status().as_dict())

    # Read-only and outside `steps`, so it can never fail the preview.
    camera_state = (
        read_camera_state(setting_client)
        if setting_client is not None
        else {"ok": False, "error": "no Open GoPro settings API"}
    )

    return {
        "enabled": True,
        "camera_state": camera_state,
        "warnings": camera_state_warnings(config, camera_state),
        "base_url": base_url,
        "api_style": setting_api_style or webcam_api_style,
        "setting_api_style": setting_api_style,
        "webcam_api_style": webcam_api_style,
        "open_base_url": open_base_url,
        "legacy_base_url": legacy_base_url,
        "discovery": discovery,
        "skipped_settings": skipped_settings,
        "ok": all(step["ok"] for step in steps),
        "steps": steps,
    }


def restart_gopro_webcam(
    config: GoProSettingsConfig,
    gopro_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Re-issue the webcam start to nudge a GoPro that has not begun streaming.

    Used during preview startup when no UDP frame has arrived yet. It does not
    stop the webcam first, so it will not tear down a stream that is coming up.
    """
    if not gopro_result or not gopro_result.get("enabled"):
        return {"ok": False, "skipped": True}
    legacy = gopro_result.get("legacy_base_url") or ""
    open_url = gopro_result.get("open_base_url") or ""
    base = gopro_result.get("base_url") or ""
    if gopro_result.get("webcam_api_style") == "legacy_gpwebcam" and (legacy or base):
        client: GoProClient = LegacyGoProClient(legacy or base)
    elif open_url or base:
        client = GoProClient(open_url or base)
    else:
        return {"ok": False, "error": "no GoPro base URL to start webcam"}
    result = client.start_webcam(
        resolution=config.webcam_resolution,
        fov=config.webcam_fov,
        port=config.webcam_port,
        protocol=config.webcam_protocol,
    )
    if isinstance(client, LegacyGoProClient):
        client.set_webcam_fov(config.webcam_fov)
    return result.as_dict()


def stop_gopro_webcam(gopro_result: dict[str, Any] | None) -> dict[str, Any]:
    """Tell the GoPro to exit webcam mode, using whichever API style answered.

    Called on Stop / shutdown so the camera does not stay stuck in webcam mode.
    """
    if not gopro_result or not gopro_result.get("enabled"):
        return {"ok": True, "skipped": True}
    legacy = gopro_result.get("legacy_base_url") or ""
    open_url = gopro_result.get("open_base_url") or ""
    base = gopro_result.get("base_url") or ""
    if gopro_result.get("webcam_api_style") == "legacy_gpwebcam" and (legacy or base):
        client: GoProClient = LegacyGoProClient(legacy or base, timeout_s=2.0)
    elif open_url or base:
        client = GoProClient(open_url or base, timeout_s=2.0)
    else:
        return {"ok": False, "error": "no GoPro base URL to stop webcam"}
    return client.stop_webcam().as_dict()
