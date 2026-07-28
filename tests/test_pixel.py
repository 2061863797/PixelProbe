"""pixelprobe pixel 测试：四角、越界、pixel_id、视频帧、HSV/亮度。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from conftest import COUNTER_POS, GREEN_POS, run_json, run_json_error


def test_image_corner_pixels(test_image: Path) -> None:
    # 四角：R=x*16, G=y*16, B=(x+y)*8
    data = run_json(
        "pixel", test_image,
        "--point", "0,0", "--point", "15,0",
        "--point", "0,15", "--point", "15,15",
        "--json",
    )["data"]
    expected = {
        (0, 0): (0, 0, 0),
        (15, 0): (240, 0, 120),
        (0, 15): (0, 240, 120),
        (15, 15): (240, 240, 240),
    }
    assert len(data["pixels"]) == 4
    for sample in data["pixels"]:
        r, g, b = expected[(sample["x"], sample["y"])]
        assert (sample["rgb"]["r"], sample["rgb"]["g"], sample["rgb"]["b"]) == (r, g, b)
        assert sample["hex"] == f"#{r:02X}{g:02X}{b:02X}"
        assert sample["pixel_id"] == sample["y"] * 16 + sample["x"]


def test_pixel_out_of_range(test_image: Path) -> None:
    for point in ("-1,0", "16,0", "0,16"):
        code, data = run_json_error(
            "pixel", test_image, "--point", point, "--json"
        )
        assert code == 5
        assert data["error"]["code"] == "COORDINATE_OUT_OF_RANGE"


def test_pixel_id_equivalent_to_point(test_image: Path) -> None:
    pid = 5 * 16 + 3  # (x=3, y=5)
    by_id = run_json("pixel", test_image, "--pixel-id", pid, "--json")["data"]
    by_pt = run_json("pixel", test_image, "--point", "3,5", "--json")["data"]
    assert by_id["pixels"][0]["rgb"] == by_pt["pixels"][0]["rgb"]
    assert by_id["pixels"][0]["x"] == 3 and by_id["pixels"][0]["y"] == 5


def test_video_pixels_at_frame(test_video: Path) -> None:
    gx, gy = GREEN_POS
    cx, cy = COUNTER_POS
    data = run_json(
        "pixel", test_video, "--frame", 12,
        "--point", "12,8",            # 移动红点在第 12 帧位于 x=12
        "--point", f"{gx},{gy}",      # 固定绿点
        "--point", f"{cx},{cy}",      # 计数像素 (t*8, t*4, t*2)
        "--json",
    )["data"]
    assert data["frame"] == 12
    rgbs = {
        (p["x"], p["y"]): (p["rgb"]["r"], p["rgb"]["g"], p["rgb"]["b"])
        for p in data["pixels"]
    }
    assert rgbs[(12, 8)] == (255, 0, 0)
    assert rgbs[(gx, gy)] == (0, 255, 0)
    assert rgbs[(cx, cy)] == (96, 48, 24)


def test_video_pixel_by_time(test_video: Path) -> None:
    data = run_json(
        "pixel", test_video, "--time", 0.5, "--point", "0,0", "--json"
    )["data"]
    # 0.5s = 第 15 帧闪白
    assert data["pixels"][0]["rgb"] == {"r": 255, "g": 255, "b": 255}
    assert data["time_ms"] == data["time_seconds"] * 1000


def test_hsv_and_luminance_fields(test_video: Path) -> None:
    data = run_json(
        "pixel", test_video, "--frame", 3, "--point", "3,8", "--json"
    )["data"]
    sample = data["pixels"][0]
    # 纯红：H=0, S=100, V=100；亮度 = 0.2126*255
    assert sample["hsv"] == {"h": 0.0, "s": 100.0, "v": 100.0}
    assert abs(sample["luminance"] - 0.2126 * 255) < 0.01


def test_no_point_is_param_error(test_image: Path) -> None:
    code, data = run_json_error("pixel", test_image, "--json")
    assert code == 2
    assert data["error"]["code"] == "INVALID_RANGE"


def test_native_pixel_preserves_rgba_samples(tmp_path: Path) -> None:
    path = tmp_path / "rgba.png"
    source = np.array([[[1, 2, 3, 4]]], dtype=np.uint8)
    Image.fromarray(source).save(path)

    data = run_json(
        "pixel", path, "--point", "0,0", "--sample", "native", "--json",
    )["data"]
    assert data["sample_semantics"] == "stored_sample"
    assert data["native_image"] == {
        "mode": "RGBA",
        "source_format": "png",
        "dtype": "uint8",
        "shape": [1, 1, 4],
        "bands": ["R", "G", "B", "A"],
        "bits_per_sample": 8,
        "has_alpha": True,
        "alpha_representation": "straight",
    }
    assert data["pixels"] == [{
        "x": 0, "y": 0, "pixel_id": 0,
        "channels": ["R", "G", "B", "A"],
        "values": [1, 2, 3, 4], "dtype": "uint8",
        "sample_semantics": "stored_sample",
    }]


def test_native_pixel_preserves_high_bit_samples(tmp_path: Path) -> None:
    path = tmp_path / "high-bit.png"
    Image.fromarray(np.array([[65535]], dtype=np.uint16)).save(path)

    data = run_json(
        "pixel", path, "--point", "0,0", "--sample", "native", "--json",
    )["data"]
    assert data["sample_semantics"] == "stored_sample"
    assert data["native_image"]["mode"] == "I;16"
    assert data["native_image"]["dtype"] == "uint16"
    assert data["native_image"]["bits_per_sample"] == 16
    assert data["pixels"][0]["values"] == [65535]


def test_native_pixel_rejects_video(test_video: Path) -> None:
    code, data = run_json_error(
        "pixel", test_video, "--point", "0,0", "--sample", "native", "--json",
    )
    assert code == 2
    assert data["error"]["code"] == "INVALID_RANGE"
