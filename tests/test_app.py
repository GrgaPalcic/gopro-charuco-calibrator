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


def test_page_offers_both_routes_with_their_steps():
    # The words the plan fixes for step 1 and the recording route, as served. The
    # screenshot script (scripts/screenshot.py) checks how they render.
    client = TestClient(app)
    page = client.get("/").text
    for text in (
        "How will you calibrate?",
        "Live over USB",
        "Calibrates the webcam stream only. Not valid for footage recorded on the camera.",
        "From a recording",
        "For footage recorded on the camera, like UMI.",
        "Check the code printed on the lens mod: ADWAL-002 = Max Lens Mod 2.0, "
        "AEWAL-001 = Ultra Wide Lens Mod.",
        "Scan before the calibration clip",
        "Scan after, to go back to dataset settings",
        "Switch the shutter back after the calibration clip.",
    ):
        assert text in page, text
    for step in ("stepConnect", "stepCapture", "stepSolve"):
        assert f'class="step live-only" id="{step}"' in page
    for step in ("stepSettings", "stepRecord", "stepDrop"):
        assert f'class="step rec-only" id="{step}"' in page
    script = client.get("/static/app.js").text
    for text in (
        "Record 60–90 s. Move slowly and hold each position for about a second.",
        "Push the board right to the edges of the frame.",
        "This clip was not recorded with the preset's settings",
        "The calibration below is only valid for footage recorded exactly like",
    ):
        assert text in script, text
    assert "/api/recording/clips?name=" in script
