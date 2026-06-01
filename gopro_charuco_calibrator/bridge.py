from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import AppConfig


@dataclass(frozen=True)
class VideoBridgeResult:
    enabled: bool
    ok: bool
    started: bool = False
    healthy_before_restart: bool = False
    pids: tuple[int, ...] = ()
    stopped_pids: tuple[int, ...] = ()
    log_path: str = ""
    probe_path: str = ""
    message: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ok": self.ok,
            "started": self.started,
            "healthy_before_restart": self.healthy_before_restart,
            "pids": list(self.pids),
            "stopped_pids": list(self.stopped_pids),
            "log_path": self.log_path,
            "probe_path": self.probe_path,
            "message": self.message,
            "error": self.error,
        }


def bridge_log_path(device: str, port: int) -> Path:
    safe_device = device.replace("/", "_").strip("_") or "video"
    return Path("/tmp") / f"gopro_charuco_bridge_{safe_device}_{int(port)}.log"


def bridge_probe_path(device: str) -> Path:
    safe_device = device.replace("/", "_").strip("_") or "video"
    return Path("/tmp") / f"gopro_charuco_probe_{safe_device}.jpg"


def _tail(path: Path, lines: int = 30) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _gopro_host(gopro_result: dict[str, Any] | None) -> str:
    if not gopro_result:
        return ""
    for key in ("legacy_base_url", "open_base_url", "base_url"):
        value = str(gopro_result.get(key) or "")
        if not value:
            continue
        host = urlparse(value).hostname or ""
        if host:
            return host
    return ""


def _route_iface(host: str) -> str:
    if not host:
        return ""
    try:
        output = subprocess.check_output(
            ["ip", "route", "get", host],
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    parts = output.split()
    if "dev" not in parts:
        return ""
    index = parts.index("dev")
    return parts[index + 1] if index + 1 < len(parts) else ""


def _ufw_active() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "ufw"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() == "active"


def _stream_blocked_hint(config: AppConfig, gopro_result: dict[str, Any] | None) -> str:
    if not _ufw_active():
        return ""
    host = _gopro_host(gopro_result)
    iface = _route_iface(host)
    if iface and host:
        return (
            " ufw is active; HTTP control can work while incoming GoPro UDP video is blocked. "
            "Allow this stream once with: "
            f"sudo ufw allow in on {iface} from {host} "
            f"to any port {int(config.gopro.webcam_port)} proto udp"
        )
    return (
        " ufw is active; HTTP control can work while incoming GoPro UDP video is blocked. "
        f"Allow incoming UDP port {int(config.gopro.webcam_port)} on the GoPro USB interface."
    )


def _video_number(device: str) -> str:
    prefix = "/dev/video"
    if not device.startswith(prefix):
        return ""
    suffix = device.removeprefix(prefix)
    return suffix if suffix.isdigit() else ""


def _ensure_video_device(device: str) -> str:
    if Path(device).exists():
        return ""
    video_nr = _video_number(device)
    command = (
        f"sudo modprobe v4l2loopback video_nr={video_nr} "
        "card_label=GoPro exclusive_caps=1"
    )
    if not video_nr:
        return f"{device} does not exist."
    if shutil.which("sudo") is None or shutil.which("modprobe") is None:
        return f"{device} does not exist. Create it with: {command}"
    result = subprocess.run(
        [
            "sudo",
            "-n",
            "modprobe",
            "v4l2loopback",
            f"video_nr={video_nr}",
            "card_label=GoPro",
            "exclusive_caps=1",
        ],
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )
    if result.returncode == 0 and Path(device).exists():
        return ""
    detail = (result.stderr or result.stdout).strip()
    if detail:
        detail = f" ({detail})"
    return f"{device} does not exist and automatic loopback creation failed{detail}. Run: {command}"


def _matching_bridge_pids(device: str, port: int) -> tuple[int, ...]:
    try:
        output = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True, timeout=2.0)
    except (OSError, subprocess.SubprocessError):
        return ()

    input_needle = f"udp://@0.0.0.0:{int(port)}"
    pids: list[int] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_text, args = line.split(maxsplit=1)
            pid = int(pid_text)
        except ValueError:
            continue
        if "ffmpeg" in args and input_needle in args and device in args:
            pids.append(pid)
    return tuple(pids)


def _stop_bridge(device: str, port: int) -> tuple[int, ...]:
    pids = _matching_bridge_pids(device, port)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if pids:
        time.sleep(0.8)
    for pid in _matching_bridge_pids(device, port):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return pids


def _probe_frame(config: AppConfig, timeout_s: float) -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    camera = config.camera
    probe_path = bridge_probe_path(camera.device)
    probe_path.unlink(missing_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "v4l2",
        "-input_format",
        "yuyv422",
        "-video_size",
        f"{int(camera.width)}x{int(camera.height)}",
        "-i",
        camera.device,
        "-frames:v",
        "1",
        "-update",
        "1",
        "-y",
        str(probe_path),
    ]
    try:
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    try:
        return probe_path.stat().st_size > 0
    except OSError:
        return False


def _start_bridge(config: AppConfig, log_path: Path) -> int | str:
    if shutil.which("ffmpeg") is None:
        return "ffmpeg is required to bridge the GoPro UDP stream into the V4L2 device."

    camera = config.camera
    gopro = config.gopro
    command = [
        "ffmpeg",
        "-nostdin",
        "-threads",
        "1",
        "-i",
        f"udp://@0.0.0.0:{int(gopro.webcam_port)}?overrun_nonfatal=1&fifo_size=50000000",
        "-fflags",
        "nobuffer",
        "-vf",
        f"scale={int(camera.width)}:{int(camera.height)},format=yuyv422",
        "-f",
        "v4l2",
        camera.device,
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_file = log_path.open("w", encoding="utf-8")
    except OSError as exc:
        return f"failed to open ffmpeg bridge log: {exc}"

    try:
        proc = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        log_file.close()
        return f"failed to start ffmpeg bridge: {exc}"
    finally:
        log_file.close()

    time.sleep(0.8)
    if proc.poll() is not None:
        detail = _tail(log_path)
        if detail:
            detail = f" ffmpeg log: {detail}"
        return f"ffmpeg bridge exited early with code {proc.returncode}.{detail}"
    return proc.pid


def ensure_gopro_video_bridge(
    config: AppConfig,
    gopro_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gopro = config.gopro
    if not gopro.enabled or not gopro.start_video_bridge:
        return VideoBridgeResult(
            enabled=False,
            ok=True,
            message="GoPro video bridge disabled",
        ).as_dict()

    camera = config.camera
    log_path = bridge_log_path(camera.device, gopro.webcam_port)
    probe_path = bridge_probe_path(camera.device)
    device_error = _ensure_video_device(camera.device)
    if device_error:
        return VideoBridgeResult(
            enabled=True,
            ok=False,
            log_path=str(log_path),
            probe_path=str(probe_path),
            error=device_error,
        ).as_dict()

    if _probe_frame(config, timeout_s=4.0):
        return VideoBridgeResult(
            enabled=True,
            ok=True,
            healthy_before_restart=True,
            pids=_matching_bridge_pids(camera.device, gopro.webcam_port),
            log_path=str(log_path),
            probe_path=str(probe_path),
            message="GoPro V4L2 stream already healthy",
        ).as_dict()

    stopped_pids = _stop_bridge(camera.device, gopro.webcam_port)
    started = _start_bridge(config, log_path)
    if isinstance(started, str):
        return VideoBridgeResult(
            enabled=True,
            ok=False,
            stopped_pids=stopped_pids,
            log_path=str(log_path),
            probe_path=str(probe_path),
            error=started,
        ).as_dict()

    time.sleep(5.0)
    if _probe_frame(config, timeout_s=8.0):
        return VideoBridgeResult(
            enabled=True,
            ok=True,
            started=True,
            pids=(started,),
            stopped_pids=stopped_pids,
            log_path=str(log_path),
            probe_path=str(probe_path),
            message="GoPro V4L2 stream healed",
        ).as_dict()

    detail = _tail(log_path)
    if detail:
        detail = f" ffmpeg log: {detail}"
    detail += _stream_blocked_hint(config, gopro_result)
    return VideoBridgeResult(
        enabled=True,
        ok=False,
        started=True,
        pids=(started,),
        stopped_pids=stopped_pids,
        log_path=str(log_path),
        probe_path=str(probe_path),
        error=f"GoPro HTTP API started, but no frame arrived on {camera.device}.{detail}",
    ).as_dict()
