"""生成确定性无损测试视频。

规格：32×32、30 FPS、30 帧。内容：
- 黑色背景；
- 红色像素 (255,0,0) 每帧向右移动一格，位于 (x=t, y=8)；
- 绿色像素 (0,255,0) 固定在 (x=24, y=16)；
- 第 15 帧整幅画面闪白（覆盖其他内容）；
- 计数像素 (x=2, y=28) 颜色为 (t*8, t*4, t*2)。

编码要求：必须无损。依次尝试 libx264rgb(qp=0) → ffv1，
写入后逐帧回读校验与生成数组完全一致，失败则报错退出，
绝不产出不可靠素材。

另提供 generate_compat_video：生成常见 H.264 + yuv420p 的兼容性测试视频
（纯色帧序列），对应测试只允许容差断言。
"""

from __future__ import annotations

import sys
from pathlib import Path

import av
import numpy as np

WIDTH = 32
HEIGHT = 32
FPS = 30
FRAME_COUNT = 30

RED_Y = 8            # 红色移动像素所在行：(x=t, y=8)
GREEN_POS = (24, 16)  # 绿色固定像素 (x, y)
COUNTER_POS = (2, 28)  # 计数像素 (x, y)
FLASH_FRAME = 15      # 整幅闪白帧

# 兼容性视频：每帧一个纯色灰阶
COMPAT_FRAME_COUNT = 10

# VFR 视频：交替 20ms / 80ms 帧间隔（毫秒 PTS），验证精确 VFR 寻址
VFR_FRAME_COUNT = 20
VFR_PTS_MS: list[int] = []
for _pair in range(VFR_FRAME_COUNT // 2):
    VFR_PTS_MS.append(_pair * 100)
    VFR_PTS_MS.append(_pair * 100 + 20)


def make_frame(t: int) -> np.ndarray:
    """构造第 t 帧内容，[32, 32, 3] uint8 RGB。"""
    arr = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    if t == FLASH_FRAME:
        arr[:] = 255
        return arr
    arr[RED_Y, t] = (255, 0, 0)
    gx, gy = GREEN_POS
    arr[gy, gx] = (0, 255, 0)
    cx, cy = COUNTER_POS
    arr[cy, cx] = (t * 8, t * 4, t * 2)
    return arr


def _encode(
    path: Path, codec: str, pix_fmt: str, options: dict[str, str],
    frames: list[np.ndarray], fps: int,
) -> None:
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream(codec, rate=fps)
        stream.width = frames[0].shape[1]
        stream.height = frames[0].shape[0]
        stream.pix_fmt = pix_fmt
        stream.options = options
        for arr in frames:
            frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def _decode_all(path: Path) -> list[np.ndarray]:
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        return [
            frame.to_ndarray(format="rgb24")
            for frame in container.decode(stream)
        ]


def generate_test_video(path: Path) -> Path:
    """生成无损测试视频并逐帧回读校验，失败抛 RuntimeError。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = [make_frame(t) for t in range(FRAME_COUNT)]
    attempts: list[tuple[str, str, dict[str, str]]] = [
        ("libx264rgb", "rgb24", {"qp": "0", "preset": "veryfast"}),
        ("ffv1", "bgr0", {}),
    ]
    errors: list[str] = []
    for codec, pix_fmt, options in attempts:
        try:
            _encode(path, codec, pix_fmt, options, expected, FPS)
        except (av.FFmpegError, ValueError) as exc:
            errors.append(f"{codec}: 编码失败（{exc}）")
            continue
        decoded = _decode_all(path)
        if len(decoded) == FRAME_COUNT and all(
            np.array_equal(a, b) for a, b in zip(decoded, expected)
        ):
            return path
        errors.append(f"{codec}: 回读校验不一致")
    raise RuntimeError(
        "无法生成无损测试视频，尝试结果：" + "；".join(errors)
    )


def vfr_frame(t: int) -> np.ndarray:
    """VFR 测试视频第 t 帧：纯色，颜色随帧号唯一，便于断言帧身份。"""
    return np.full(
        (HEIGHT, WIDTH, 3), (t * 12, 100, 255 - t * 12), dtype=np.uint8
    )


def generate_vfr_video(path: Path, *, pts_offset_ms: int = 0) -> Path:
    """生成可变帧率（VFR）无损测试视频并逐帧回读校验。

    帧间隔交替 20ms / 80ms（PTS 单位毫秒，MKV time_base 1/1000），
    用于验证 PTS 索引精确寻址。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from fractions import Fraction

    expected = [vfr_frame(t) for t in range(VFR_FRAME_COUNT)]
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx264rgb", rate=30)
        stream.width = WIDTH
        stream.height = HEIGHT
        stream.pix_fmt = "rgb24"
        stream.options = {"qp": "0", "preset": "veryfast"}
        for t, arr in enumerate(expected):
            frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
            frame.time_base = Fraction(1, 1000)
            frame.pts = pts_offset_ms + VFR_PTS_MS[t]
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)

    decoded = _decode_all(path)
    if len(decoded) != VFR_FRAME_COUNT or not all(
        np.array_equal(a, b) for a, b in zip(decoded, expected)
    ):
        raise RuntimeError(f"VFR 测试视频回读校验失败：{path}")
    return path


def generate_compat_video(path: Path) -> Path:
    """生成常见 H.264 + yuv420p 兼容性测试视频（纯色灰阶帧）。

    有损编码，相关断言必须使用容差比较。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for t in range(COMPAT_FRAME_COUNT):
        level = np.uint8(20 + t * 20)
        frames.append(np.full((HEIGHT, WIDTH, 3), level, dtype=np.uint8))
    _encode(path, "libx264", "yuv420p", {"qp": "0", "preset": "veryfast"},
            frames, FPS)
    return path


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("test_video.mkv")
    generate_test_video(target)
    print(f"已生成无损测试视频：{target}")
