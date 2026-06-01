from gopro_charuco_calibrator.bridge import ensure_gopro_video_bridge
from gopro_charuco_calibrator.models import AppConfig


def test_bridge_disabled_when_gopro_control_is_off():
    result = ensure_gopro_video_bridge(AppConfig())

    assert result["ok"] is True
    assert result["enabled"] is False


def test_bridge_keeps_healthy_existing_stream(monkeypatch):
    config = AppConfig()
    config.gopro.enabled = True

    monkeypatch.setattr("gopro_charuco_calibrator.bridge._ensure_video_device", lambda _device: "")
    monkeypatch.setattr(
        "gopro_charuco_calibrator.bridge._probe_frame",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "gopro_charuco_calibrator.bridge._matching_bridge_pids",
        lambda *_args: (123,),
    )

    result = ensure_gopro_video_bridge(config)

    assert result["ok"] is True
    assert result["healthy_before_restart"] is True
    assert result["pids"] == [123]


def test_bridge_restarts_when_probe_fails(monkeypatch):
    config = AppConfig()
    config.gopro.enabled = True
    probes = iter([False, True])

    monkeypatch.setattr("gopro_charuco_calibrator.bridge._ensure_video_device", lambda _device: "")
    monkeypatch.setattr(
        "gopro_charuco_calibrator.bridge._probe_frame",
        lambda *_args, **_kwargs: next(probes),
    )
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._stop_bridge", lambda *_args: (10,))
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._start_bridge", lambda *_args: 20)
    monkeypatch.setattr("gopro_charuco_calibrator.bridge.time.sleep", lambda _seconds: None)

    result = ensure_gopro_video_bridge(config)

    assert result["ok"] is True
    assert result["started"] is True
    assert result["pids"] == [20]
    assert result["stopped_pids"] == [10]


def test_bridge_reports_ufw_hint_when_stream_is_blocked(monkeypatch):
    config = AppConfig()
    config.gopro.enabled = True
    probes = iter([False, False])

    monkeypatch.setattr("gopro_charuco_calibrator.bridge._ensure_video_device", lambda _device: "")
    monkeypatch.setattr(
        "gopro_charuco_calibrator.bridge._probe_frame",
        lambda *_args, **_kwargs: next(probes),
    )
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._stop_bridge", lambda *_args: ())
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._start_bridge", lambda *_args: 20)
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._tail", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._ufw_active", lambda: True)
    monkeypatch.setattr("gopro_charuco_calibrator.bridge._route_iface", lambda _host: "enx0")
    monkeypatch.setattr("gopro_charuco_calibrator.bridge.time.sleep", lambda _seconds: None)

    result = ensure_gopro_video_bridge(
        config,
        {"base_url": "http://172.20.144.51:8080"},
    )

    assert result["ok"] is False
    assert "ufw is active" in result["error"]
    assert "sudo ufw allow in on enx0 from 172.20.144.51" in result["error"]
