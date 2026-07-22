"""pytest 公共设施：程序生成的确定性测试素材 + CLI 调用助手。

素材目录和文件名故意使用中文，持续验证 Windows 中文路径支持。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from generate_test_image import generate_test_image, make_image_array  # noqa: E402
from generate_test_video import (  # noqa: E402
    COUNTER_POS,
    FLASH_FRAME,
    FRAME_COUNT,
    GREEN_POS,
    RED_Y,
    VFR_FRAME_COUNT,
    generate_compat_video,
    generate_test_video,
    generate_vfr_video,
    make_frame,
    vfr_frame,
)

__all__ = [
    "make_image_array",
    "make_frame",
    "FRAME_COUNT",
    "FLASH_FRAME",
    "RED_Y",
    "GREEN_POS",
    "COUNTER_POS",
]


@pytest.fixture(scope="session")
def assets_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("测试素材")


@pytest.fixture(scope="session")
def test_image(assets_dir: Path) -> Path:
    return generate_test_image(assets_dir / "测试图片.png")


@pytest.fixture(scope="session")
def test_video(assets_dir: Path) -> Path:
    return generate_test_video(assets_dir / "测试视频.mkv")


@pytest.fixture(scope="session")
def compat_video(assets_dir: Path) -> Path:
    return generate_compat_video(assets_dir / "兼容视频.mp4")


@pytest.fixture(scope="session")
def vfr_video(assets_dir: Path) -> Path:
    return generate_vfr_video(assets_dir / "变帧率.mkv")


@pytest.fixture(scope="session")
def offset_vfr_video(assets_dir: Path) -> Path:
    """起始 PTS 为 5 秒的 VFR 视频，用于验证公开时间从 0 开始。"""
    return generate_vfr_video(
        assets_dir / "偏移变帧率.mkv", pts_offset_ms=5000
    )


def run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    """以子进程方式运行 pixelprobe，返回 CompletedProcess（UTF-8 文本）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "pixelprobe", *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def run_json(*args: object) -> dict:
    """运行 CLI 并断言：退出码 0、stdout 是单个合法 JSON、success=true。"""
    proc = run_cli(*args)
    assert proc.returncode == 0, f"退出码 {proc.returncode}，stderr={proc.stderr}"
    data = json.loads(proc.stdout)
    assert data["success"] is True
    return data


def run_json_error(*args: object) -> tuple[int, dict]:
    """运行 CLI（预期失败），返回 (退出码, JSON 错误对象)。"""
    proc = run_cli(*args)
    assert proc.returncode != 0
    data = json.loads(proc.stdout)
    assert data["success"] is False
    assert "code" in data["error"] and "message" in data["error"]
    return proc.returncode, data
