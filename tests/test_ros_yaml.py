import numpy as np
import yaml

from gopro_charuco_calibrator.ros_yaml import save_camera_info_yaml


def test_save_camera_info_yaml_schema(tmp_path):
    path = tmp_path / "camera.yaml"
    k = np.asarray([[600.0, 0.0, 320.0], [0.0, 610.0, 240.0], [0.0, 0.0, 1.0]])
    d = np.asarray([0.01, -0.02, 0.0, 0.0, 0.001])

    save_camera_info_yaml(
        path,
        camera_name="gopro13_test",
        image_size=(640, 480),
        camera_matrix=k,
        dist_coeffs=d,
        distortion_model="plumb_bob",
    )

    data = yaml.safe_load(path.read_text())
    assert data["image_width"] == 640
    assert data["image_height"] == 480
    assert data["camera_name"] == "gopro13_test"
    assert data["distortion_model"] == "plumb_bob"
    assert data["camera_matrix"]["data"][0] == 600.0
    assert data["distortion_coefficients"]["cols"] == 5
