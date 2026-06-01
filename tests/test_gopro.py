from gopro_charuco_calibrator.gopro import GoProClient, apply_gopro_settings
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
            webcam_fov=4,
            video_lens=9,
            video_resolution=100,
        )
    )

    assert result["ok"] is True
    assert any("setting=121&option=9" in call for call in calls)
    assert any("setting=2&option=100" in call for call in calls)
    assert any("/gopro/webcam/start?res=7&fov=4" in call for call in calls)
