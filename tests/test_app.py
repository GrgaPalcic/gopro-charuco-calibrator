from fastapi.testclient import TestClient

from gopro_charuco_calibrator import presets
from gopro_charuco_calibrator.app import app


def test_ui_files_are_revalidated_and_unversioned():
    client = TestClient(app)
    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-cache"
    assert "?v=" not in index.text
    for path in ("/static/app.js", "/static/style.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"


def test_every_shipped_preset_loads(monkeypatch, tmp_path):
    monkeypatch.setattr(presets, "user_presets_dir", lambda: tmp_path)  # shipped only
    entries = presets.list_presets()
    names = [entry["name"] for entry in entries]
    assert "gopro13_mlm2_adwal002" in names
    assert not any(name.startswith("gopro13_central_") for name in names)
    assert not any("error" in entry for entry in entries)
    for name in names:
        _title, config = presets.get_preset(name)
        assert config.gopro.enabled and config.gopro.webcam_resolution == 12


def test_shipped_preset_titles_are_plain_and_short(monkeypatch, tmp_path):
    # The titles are the choices in step 1 of the UI: words a newcomer reads, short
    # enough to show in full in the dropdown.
    monkeypatch.setattr(presets, "user_presets_dir", lambda: tmp_path)  # shipped only
    for entry in presets.list_presets():
        title = entry["title"]
        assert title != entry["name"] and "_" not in title, title
        assert len(title) <= 42, title
