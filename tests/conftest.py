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


def engine_tensors(request):
    """经统一引擎在内存中执行单个请求，返回该请求的全部输出 Tensor。"""
    import pixelprobe
    return pixelprobe.generate(request).request_tensors[0]


def engine_xt(video: Path, y: int):
    """经引擎生成整段视频在第 y 行的 X-T 切片 Tensor。"""
    from pixelprobe.request_builders import spacetime_request
    return engine_tensors(spacetime_request(video, "xt", y))[0]


def engine_reduce(video: Path, op: str, rect=None):
    """经引擎做时间域合成，返回 (Data Tensor, Preview Tensor)。"""
    from pixelprobe.request_builders import reduce_request
    data, preview = engine_tensors(reduce_request(video, operation=op, rect=rect))
    return data, preview


def engine_slice(video: Path, kind: str, fixed_coordinate: int, **range_options):
    """经引擎生成 X-T/Y-T 切片，返回与 CLI 一致读取的 array/frames/times。"""
    from types import SimpleNamespace
    from pixelprobe.request_builders import spacetime_request
    tensor = engine_tensors(
        spacetime_request(video, kind, fixed_coordinate, **range_options)
    )[0]
    return SimpleNamespace(
        array=tensor.data.materialize(),
        frames=list(tensor.attributes["presentation_indices"]),
        times=list(tensor.attributes["timeline_timestamps_seconds"]),
        tensor=tensor,
    )


def engine_timeline(video: Path, **options):
    """经引擎提取像素时间线，按 CLI 的方式组装 matrix[K,T,3]、点、帧与时间。"""
    from types import SimpleNamespace
    import numpy as np
    from pixelprobe.models.pixel import PixelCoordinate
    from pixelprobe.request_builders import timeline_matrix, timeline_request
    from pixelprobe.utils.coordinates import pixel_id_from_xy
    request, ordered, width, _ = timeline_request(video, **options)
    tensor = engine_tensors(request)[0]
    matrix = timeline_matrix(tensor)
    block_size = options.get("block_size")
    if block_size is not None:
        matrix = np.round(matrix).astype(np.uint8)
    return SimpleNamespace(
        matrix=matrix,
        points=[
            PixelCoordinate(x=x, y=y, pixel_id=pixel_id_from_xy(x, y, width))
            for x, y in ordered
        ],
        frames=list(tensor.attributes["presentation_indices"]),
        times=list(tensor.attributes["timeline_timestamps_seconds"]),
        sample_type="point" if block_size is None else "block_mean",
        tensor=tensor,
    )


def expected_xt(y: int):
    """测试视频按无损编码校验，X-T 切片的真值直接由生成函数得到。"""
    import numpy as np
    return np.stack([make_frame(t)[y] for t in range(FRAME_COUNT)])


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
