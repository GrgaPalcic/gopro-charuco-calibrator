from gopro_charuco_calibrator.gopro import (
    GoProClient,
    apply_gopro_settings,
    discover_gopro_base_url,
)
from gopro_charuco_calibrator.models import GoProSettingsConfig


def test_gopro_client_webcam_start_url(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"status": "ok"}'

    def fake_urlopen(url, timeout):
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr("gopro_charuco_calibrator.gopro.urlopen", fake_urlopen)
    client = GoProClient("http://172.20.0.51:8080")

    result = client.start_webcam(resolution=7, fov=3, port=8554, protocol="RTSP")

    assert result.ok is True
    assert calls[0][0] == (
        "http://172.20.0.51:8080/gopro/webcam/start?res=7&fov=3&port=8554&protocol=RTSP"
    )


def test_apply_gopro_settings_records_steps(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"status": "ok"}'

    def fake_urlopen(url, timeout):
        calls.append(url)
        return Response()

    monkeypatch.setattr("gopro_charuco_calibrator.gopro.urlopen", fake_urlopen)
    result = apply_gopro_settings(
        GoProSettingsConfig(
            enabled=True,
            base_url="http://172.20.0.51:8080",
            start_video_bridge=False,
            webcam_resolution=7,
            webcam_fov=4,
            video_lens=9,
            video_resolution=100,
        )
    )

    assert result["ok"] is True
    assert any("setting=121&option=9" in call for call in calls)
    assert any("setting=2&option=100" in call for call in calls)
    assert any("/gopro/webcam/start?res=7&fov=4" in call for call in calls)


def test_discover_gopro_base_url_uses_usb_51_candidate(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"status": "ok"}'

    def fake_check_output(*_args, **_kwargs):
        return "26: enp6s0f4u2 inet 172.20.144.52/24 brd 172.20.144.255 scope global\n"

    def fake_urlopen(url, timeout):
        calls.append(url)
        if url == "http://172.20.144.51:8080/gopro/webcam/status":
            return Response()
        raise OSError("no route")

    monkeypatch.setattr("gopro_charuco_calibrator.gopro.subprocess.check_output", fake_check_output)
    monkeypatch.setattr("gopro_charuco_calibrator.gopro.urlopen", fake_urlopen)

    result = discover_gopro_base_url()

    assert result["ok"] is True
    assert result["base_url"] == "http://172.20.144.51:8080"
    assert result["api_style"] == "open_gopro"
    assert calls[0] == "http://172.20.144.51:8080/gopro/webcam/status"


def test_apply_gopro_settings_auto_discovers_legacy_endpoint(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"status": "ok"}'

    def fake_check_output(*_args, **_kwargs):
        return "26: enp6s0f4u2 inet 172.20.144.52/24 brd 172.20.144.255 scope global\n"

    def fake_urlopen(url, timeout):
        calls.append(url)
        if url.startswith("http://172.20.144.51:8080/gopro/"):
            raise OSError("new api missing")
        if url.startswith("http://172.20.144.51:8080/gp/gpWebcam/"):
            return Response()
        raise OSError("no route")

    monkeypatch.setattr("gopro_charuco_calibrator.gopro.subprocess.check_output", fake_check_output)
    monkeypatch.setattr("gopro_charuco_calibrator.gopro.urlopen", fake_urlopen)

    result = apply_gopro_settings(
        GoProSettingsConfig(enabled=True, base_url="", webcam_resolution=7, webcam_fov=3)
    )

    assert result["ok"] is True
    assert result["base_url"] == "http://172.20.144.51:8080"
    assert result["api_style"] == "legacy_gpwebcam"
    assert any("/gp/gpWebcam/START?res=720&port=8554" in call for call in calls)


def test_apply_gopro_settings_uses_legacy_webcam_for_video_bridge(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"status": "ok"}'

    def fake_urlopen(url, timeout):
        calls.append(url)
        return Response()

    monkeypatch.setattr("gopro_charuco_calibrator.gopro.urlopen", fake_urlopen)

    result = apply_gopro_settings(
        GoProSettingsConfig(
            enabled=True,
            base_url="http://172.20.144.51:8080",
            webcam_resolution=7,
            video_lens=9,
            webcam_fov=4,
        )
    )

    assert result["ok"] is True
    assert result["setting_api_style"] == "open_gopro"
    assert result["webcam_api_style"] == "legacy_gpwebcam"
    assert any("/gopro/camera/setting?setting=121&option=9" in call for call in calls)
    assert not any("/gopro/camera/setting?setting=43" in call for call in calls)
    assert any("/gp/gpWebcam/START?res=720&port=8554" in call for call in calls)
    assert any("/gp/gpWebcam/SETTINGS?fov=4" in call for call in calls)
