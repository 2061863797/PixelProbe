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
    BLINK_FRAME_COUNT,
    BLINK_PERIOD,
    BLINK_RECT,
    COUNTER_POS,
    FLASH_FRAME,
    FRAME_COUNT,
    GREEN_POS,
    MOTION_BLOCK,
    MOTION_FRAME_COUNT,
    MOTION_STEP,
    MOTION_X0,
    MOTION_Y,
    NOISE_FRAME_COUNT,
    NOISE_QUIET_RECT,
    NOISE_SIZE,
    RED_Y,
    VFR_FRAME_COUNT,
    generate_blink_video,
    generate_compat_video,
    generate_motion_video,
    generate_noise_video,
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
    "NOISE_SIZE",
    "NOISE_FRAME_COUNT",
    "NOISE_QUIET_RECT",
    "BLINK_FRAME_COUNT",
    "BLINK_PERIOD",
    "BLINK_RECT",
    "MOTION_FRAME_COUNT",
    "MOTION_BLOCK",
    "MOTION_STEP",
    "MOTION_X0",
    "MOTION_Y",
    "VFR_FRAME_COUNT",
    "vfr_frame",
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


@pytest.fixture(scope="session")
def noise_video(assets_dir: Path) -> Path:
    """噪点藏区域视频：中央 16×16 噪声幅度减半（时间 std 偏低）。"""
    return generate_noise_video(assets_dir / "噪声视频.mkv")


@pytest.fixture(scope="session")
def blink_video(assets_dir: Path) -> Path:
    """周期闪烁视频：30fps、每 6 帧亮一次（5Hz）。"""
    return generate_blink_video(assets_dir / "闪烁视频.mkv")


@pytest.fixture(scope="session")
def motion_video(assets_dir: Path) -> Path:
    """匀速右移白块视频：光流主方向约 0°。"""
    return generate_motion_video(assets_dir / "运动视频.mkv")


def run_cli(
    *args: object,
    python_io_encoding: str = "utf-8",
) -> subprocess.CompletedProcess[str]:
    """以子进程方式运行 pixelprobe，返回 CompletedProcess（UTF-8 文本）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = python_io_encoding
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
