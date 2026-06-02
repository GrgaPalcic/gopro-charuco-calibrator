from gopro_charuco_calibrator.bridge import (
    decode_command,
    firewall_hint,
    frame_nbytes,
    stream_disabled_result,
    stream_error_result,
    stream_ok_result,
)
from gopro_charuco_calibrator.models import AppConfig


def test_decode_command_reads_udp_and_writes_rawvideo_pipe():
    config = AppConfig()
    config.gopro.webcam_port = 8554
    config.camera.width = 1280
    config.camera.height = 720
    cmd = decode_command(config)

    assert cmd[0] == "ffmpeg"
    assert "udp://@0.0.0.0:8554?overrun_nonfatal=1&fifo_size=5000000" in cmd
    assert "rawvideo" in cmd
    assert "pipe:1" in cmd
    assert "bgr24" in cmd
    assert "scale=1280:720" in cmd
    # passthrough avoids the 90kHz-clock runaway duplication.
    assert "passthrough" in cmd


def test_frame_nbytes_is_bgr():
    config = AppConfig()
    config.camera.width = 1280
    config.camera.height = 720
    assert frame_nbytes(config) == 1280 * 720 * 3


def test_stream_disabled_and_ok_results(tmp_path):
    disabled = stream_disabled_result()
    assert disabled["enabled"] is False
    assert disabled["ok"] is True

    ok = stream_ok_result(tmp_path / "log.txt")
    assert ok["enabled"] is True
    assert ok["ok"] is True


def test_firewall_hint_builds_exact_ufw_command(monkeypatch):
    config = AppConfig()
    config.gopro.enabled = True
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._ufw_active", lambda: True)
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._route_iface", lambda _host: "enx0")

    fw = firewall_hint(config, {"base_url": "http://172.20.144.51:8080"})

    assert fw["ufw_active"] is True
    assert fw["iface"] == "enx0"
    assert fw["gopro_host"] == "172.20.144.51"
    assert fw["ufw_command"] == (
        "sudo ufw allow in on enx0 from 172.20.144.51 to any port 8554 proto udp"
    )
    assert "sudo ufw allow in on enx0 from 172.20.144.51" in fw["firewall_hint"]


def test_stream_error_result_surfaces_ufw_hint(monkeypatch, tmp_path):
    config = AppConfig()
    config.gopro.enabled = True
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._ufw_active", lambda: True)
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._route_iface", lambda _host: "enx0")
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._tail", lambda *_a, **_k: "")

    result = stream_error_result(
        config,
        {"base_url": "http://172.20.144.51:8080"},
        tmp_path / "log.txt",
        "No video frames received.",
    )

    assert result["ok"] is False
    assert result["ufw_command"].startswith("sudo ufw allow in on enx0 from 172.20.144.51")
    assert "ufw is active" in result["error"]
    assert "sudo ufw allow in on enx0 from 172.20.144.51" in result["error"]
