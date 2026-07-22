"""生成"球在第 46 帧开始移动"的演示视频（无损），用于 AI 工作流演示与评估。

64×64、30 FPS、90 帧：白色 6×6 方块（"球"）位于 (10..15, 30..35)，
第 0～45 帧静止，第 46 帧起每帧右移 1 像素。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_test_video import _decode_all, _encode  # noqa: E402

WIDTH = HEIGHT = 64
FRAME_COUNT = 90
FPS = 30
MOVE_START = 46  # 第一个发生位移的帧
BALL_X0, BALL_Y0, BALL_SIZE = 10, 30, 6


def ball_frame(t: int) -> np.ndarray:
    """构造第 t 帧：帧 46 起球每帧右移 1 像素。"""
    arr = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    offset = max(0, t - MOVE_START + 1)
    x0 = BALL_X0 + offset
    arr[BALL_Y0 : BALL_Y0 + BALL_SIZE, x0 : x0 + BALL_SIZE] = 255
    return arr


def generate_demo_video(path: Path) -> Path:
    """生成演示视频并逐帧回读校验，失败抛 RuntimeError。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = [ball_frame(t) for t in range(FRAME_COUNT)]
    _encode(path, "libx264rgb", "rgb24", {"qp": "0", "preset": "veryfast"},
            frames, FPS)
    decoded = _decode_all(path)
    if len(decoded) != FRAME_COUNT or not all(
        np.array_equal(a, b) for a, b in zip(decoded, frames)
    ):
        raise RuntimeError(f"演示视频回读校验失败：{path}")
    return path


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("demo_ball.mkv")
    generate_demo_video(target)
    print(f"已生成演示视频：{target}（球在帧 {MOVE_START} 开始移动）")
