"""pixelprobe frame 测试：帧定位、时间定位、裁剪、缩放、错误处理。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from conftest import FLASH_FRAME, FRAME_COUNT, make_frame, run_json, run_json_error


def _read_png(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)


def test_extract_by_frame_index(test_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "帧7.png"
    data = run_json(
        "frame", test_video, "--frame", 7, "--output", out, "--json"
    )["data"]
    assert data["frame"] == 7
    assert data["output_path"] == str(out)
    assert np.array_equal(_read_png(out), make_frame(7))


def test_extract_first_and_last(test_video: Path, tmp_path: Path) -> None:
    for idx in (0, FRAME_COUNT - 1):
        out = tmp_path / f"帧{idx}.png"
        data = run_json(
            "frame", test_video, "--frame", idx, "--output", out, "--json"
        )["data"]
        assert data["frame"] == idx
        assert np.array_equal(_read_png(out), make_frame(idx))


def test_extract_by_time(test_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "闪白.png"
    data = run_json(
        "frame", test_video, "--time", 0.5, "--output", out, "--json"
    )["data"]
    assert data["frame"] == FLASH_FRAME
    assert (_read_png(out) == 255).all()


def test_time_maps_to_last_frame_not_after(test_video: Path) -> None:
    # 规则：时间戳不大于目标时间的最后一帧
    data = run_json("frame", test_video, "--time", 0.51, "--json")["data"]
    assert data["frame"] == FLASH_FRAME
    data = run_json("frame", test_video, "--time", 0.0, "--json")["data"]
    assert data["frame"] == 0


def test_crop(test_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "裁剪.png"
    data = run_json(
        "frame", test_video, "--frame", 10,
        "--crop", "8,6,8,8", "--output", out, "--json",
    )["data"]
    assert data["width"] == 8 and data["height"] == 8
    expected = make_frame(10)[6:14, 8:16, :]
    assert np.array_equal(_read_png(out), expected)


def test_preview_scaling_keeps_aspect(test_image: Path, tmp_path: Path) -> None:
    out = tmp_path / "预览.png"
    data = run_json(
        "frame", test_image, "--max-width", 8, "--output", out, "--json"
    )["data"]
    # 原始尺寸字段不受预览缩放影响
    assert data["width"] == 16 and data["height"] == 16
    assert data["output_width"] == 8 and data["output_height"] == 8


def test_frame_out_of_range(test_video: Path) -> None:
    for bad in (FRAME_COUNT, -1):
        code, data = run_json_error(
            "frame", test_video, "--frame", bad, "--json"
        )
        assert code == 6
        assert data["error"]["code"] == "FRAME_OUT_OF_RANGE"


def test_time_out_of_range(test_video: Path) -> None:
    code, data = run_json_error(
        "frame", test_video, "--time", 99.0, "--json"
    )
    assert code == 6
    assert data["error"]["code"] == "TIME_OUT_OF_RANGE"


def test_frame_time_conflict(test_video: Path) -> None:
    code, data = run_json_error(
        "frame", test_video, "--frame", 1, "--time", 0.5, "--json"
    )
    assert code == 2
    assert data["error"]["code"] == "INVALID_RANGE"


def test_output_write_failure(test_video: Path, test_image: Path) -> None:
    # 输出路径的父级是一个文件，目录创建必然失败 → 退出码 8
    bad_output = Path(str(test_image)) / "子目录" / "x.png"
    code, data = run_json_error(
        "frame", test_video, "--frame", 0, "--output", bad_output, "--json"
    )
    assert code == 8
    assert data["error"]["code"] == "OUTPUT_WRITE_FAILED"
