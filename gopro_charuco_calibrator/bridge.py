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
class VideoStreamResult:
    """Status of the GoPro UDP -> raw-frame decode used by the live preview."""

    enabled: bool
    ok: bool
    message: str = ""
    error: str = ""
    log_path: str = ""
    # Firewall diagnostics, populated only when no UDP frame arrives.
    ufw_active: bool = False
    iface: str = ""
    gopro_host: str = ""
    ufw_command: str = ""
    firewalld_command: str = ""
    iptables_command: str = ""
    firewall_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ok": self.ok,
            "message": self.message,
            "error": self.error,
            "log_path": self.log_path,
            "ufw_active": self.ufw_active,
            "iface": self.iface,
            "gopro_host": self.gopro_host,
            "ufw_command": self.ufw_command,
            "firewalld_command": self.firewalld_command,
            "iptables_command": self.iptables_command,
            "firewall_hint": self.firewall_hint,
        }


def bridge_log_path(device: str, port: int) -> Path:
    safe_device = device.replace("/", "_").strip("_") or "video"
    return Path("/tmp") / f"gopro_charuco_decode_{safe_device}_{int(port)}.log"


def _tail(path: Path, lines: int = 20) -> str:
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


def firewall_hint(config: AppConfig, gopro_result: dict[str, Any] | None) -> dict[str, Any]:
    """Build structured firewall diagnostics for a blocked UDP video stream.

    The webcam HTTP control channel (TCP) can succeed while the incoming UDP video
    is dropped by a host firewall, which is the most common reason no frame arrives.
    This returns the exact command(s) to allow the stream so the UI can surface a
    copy-paste fix without the operator guessing the interface or camera IP.
    """
    host = _gopro_host(gopro_result)
    iface = _route_iface(host)
    port = int(config.gopro.webcam_port)
    ufw_active = _ufw_active()

    ufw_command = ""
    iptables_command = ""
    if iface and host:
        ufw_command = f"sudo ufw allow in on {iface} from {host} to any port {port} proto udp"
        iptables_command = (
            f"sudo iptables -A INPUT -i {iface} -s {host} -p udp --dport {port} -j ACCEPT"
        )
    firewalld_command = ""
    if host:
        firewalld_command = (
            "sudo firewall-cmd --add-rich-rule="
            f"'rule family=ipv4 source address={host} port port={port} protocol=udp accept'"
        )

    if ufw_active and ufw_command:
        hint = (
            "ufw is active; HTTP control can work while incoming GoPro UDP video is blocked. "
            f"Allow this stream once with: {ufw_command}"
        )
    elif ufw_active:
        hint = (
            "ufw is active; HTTP control can work while incoming GoPro UDP video is blocked. "
            f"Allow incoming UDP port {port} on the GoPro USB interface."
        )
    elif ufw_command:
        hint = (
            "No UDP video frames arrived. If a host firewall is blocking incoming UDP, "
            f"allow it once with: {ufw_command}"
        )
    else:
        hint = (
            "No UDP video frames arrived. If a host firewall is blocking incoming UDP, "
            f"allow incoming UDP port {port} on the GoPro USB interface."
        )

    return {
        "ufw_active": ufw_active,
        "iface": iface,
        "gopro_host": host,
        "port": port,
        "ufw_command": ufw_command,
        "firewalld_command": firewalld_command,
        "iptables_command": iptables_command,
        "firewall_hint": hint,
    }


def decode_command(config: AppConfig) -> list[str]:
    """ffmpeg args to decode the GoPro UDP MPEG-TS into raw BGR frames on stdout.

    Notes:
    - No v4l2loopback: frames go straight to a pipe the app reads, removing the
      loopback device (and its sudo/modprobe requirements) plus a mux/demux hop.
    - ``-fps_mode passthrough`` emits one output frame per decoded frame, which
      avoids the 90kHz-clock runaway duplication that CFR output triggers.
    - A modest UDP ``fifo_size`` plus ``nobuffer``/``low_delay`` keeps latency low;
      the app's reader then keeps only the newest frame so nothing accumulates.
    """
    camera = config.camera
    gopro = config.gopro
    return [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "warning",
        "-threads",
        "0",
        "-fflags",
        "nobuffer+discardcorrupt",
        "-flags",
        "low_delay",
        "-i",
        f"udp://@0.0.0.0:{int(gopro.webcam_port)}?overrun_nonfatal=1&fifo_size=5000000",
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        f"scale={int(camera.width)}:{int(camera.height)}",
        "-pix_fmt",
        "bgr24",
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "pipe:1",
    ]


def frame_nbytes(config: AppConfig) -> int:
    return int(config.camera.width) * int(config.camera.height) * 3


def start_decode(config: AppConfig, log_path: Path) -> subprocess.Popen[bytes] | str:
    """Start the ffmpeg decode process. Returns the Popen or an error string."""
    if shutil.which("ffmpeg") is None:
        return "ffmpeg is required to decode the GoPro UDP stream."
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_file = log_path.open("w", encoding="utf-8")
    except OSError as exc:
        return f"failed to open ffmpeg log: {exc}"
    try:
        proc = subprocess.Popen(
            decode_command(config),
            stdout=subprocess.PIPE,
            stderr=log_file,
            bufsize=0,
            close_fds=True,
        )
    except OSError as exc:
        log_file.close()
        return f"failed to start ffmpeg: {exc}"
    finally:
        log_file.close()
    return proc


def stream_disabled_result() -> dict[str, Any]:
    return VideoStreamResult(enabled=False, ok=True, message="direct V4L2 device").as_dict()


def stream_ok_result(log_path: Path) -> dict[str, Any]:
    return VideoStreamResult(
        enabled=True,
        ok=True,
        message="decoding GoPro UDP stream",
        log_path=str(log_path),
    ).as_dict()


def stream_error_result(
    config: AppConfig,
    gopro_result: dict[str, Any] | None,
    log_path: Path,
    summary: str,
) -> dict[str, Any]:
    fw = firewall_hint(config, gopro_result)
    detail = _tail(log_path)
    message = summary
    if detail:
        message += f" ffmpeg log: {detail}"
    if fw["ufw_active"]:
        message += " " + fw["firewall_hint"]
    return VideoStreamResult(
        enabled=True,
        ok=False,
        error=message,
        log_path=str(log_path),
        ufw_active=fw["ufw_active"],
        iface=fw["iface"],
        gopro_host=fw["gopro_host"],
        ufw_command=fw["ufw_command"],
        firewalld_command=fw["firewalld_command"],
        iptables_command=fw["iptables_command"],
        firewall_hint=fw["firewall_hint"],
    ).as_dict()


def _matching_decode_pids(port: int) -> tuple[int, ...]:
    try:
        output = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True, timeout=2.0)
    except (OSError, subprocess.SubprocessError):
        return ()
    needle = f"udp://@0.0.0.0:{int(port)}"
    pids: list[int] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            pid_text, args = line.split(maxsplit=1)
            pid = int(pid_text)
        except ValueError:
            continue
        if "ffmpeg" in args and needle in args:
            pids.append(pid)
    return tuple(pids)


def stop_gopro_video_bridge(config: AppConfig) -> dict[str, Any]:
    """Kill any ffmpeg decode process feeding our UDP port (idempotent).

    Used to clear orphans on startup and to clean up on Stop/shutdown. Safe to
    call when nothing is running -- it matches only ffmpeg + our exact udp port.
    """
    port = int(config.gopro.webcam_port)
    pids = _matching_decode_pids(port)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if pids:
        time.sleep(0.4)
    for pid in _matching_decode_pids(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return {"stopped_pids": list(pids)}
