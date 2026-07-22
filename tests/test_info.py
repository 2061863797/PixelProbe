"""pixelprobe info 测试。"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FRAME_COUNT, run_cli, run_json, run_json_error


def test_image_info(test_image: Path) -> None:
    data = run_json("info", test_image, "--json")["data"]
    assert data["media_type"] == "image"
    assert data["width"] == 16 and data["height"] == 16
    assert data["channels"] == 3
    assert data["file_size_bytes"] > 0
    assert data["path"].endswith("测试图片.png")


def test_video_info(test_video: Path) -> None:
    data = run_json("info", test_video, "--json")["data"]
    assert data["media_type"] == "video"
    assert data["width"] == 32 and data["height"] == 32
    assert data["fps"] == 30.0
    assert data["frame_count"] == FRAME_COUNT
    assert abs(data["duration_seconds"] - 1.0) < 0.05
    assert data["codec"] == "h264"
    assert data["is_vfr"] is False
    assert data["pixel_format"]
    assert data["time_base"]


def test_info_human_readable(test_image: Path) -> None:
    proc = run_cli("info", test_image)
    assert proc.returncode == 0
    assert "16" in proc.stdout


def test_missing_file_exit_code() -> None:
    code, data = run_json_error("info", "不存在的文件.mp4", "--json")
    assert code == 3
    assert data["error"]["code"] == "FILE_NOT_FOUND"


def test_unsupported_media_exit_code(tmp_path: Path) -> None:
    bad = tmp_path / "不是媒体.xyz"
    bad.write_bytes(b"this is not a media file at all")
    code, data = run_json_error("info", bad, "--json")
    assert code == 4
    assert data["error"]["code"] == "UNSUPPORTED_MEDIA"


def test_json_stdout_purity(test_video: Path) -> None:
    proc = run_cli("info", test_video, "--json")
    assert proc.returncode == 0
    # stdout 必须是单个合法 JSON，无任何额外文字
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["success"] is True and parsed["command"] == "info"
