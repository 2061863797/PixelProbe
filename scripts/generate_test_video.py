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

# 噪声视频：全画面随机噪声，中央区域噪声幅度减半（时间 std 显著偏低）
NOISE_SIZE = 64
NOISE_FRAME_COUNT = 40
NOISE_QUIET_RECT = (24, 24, 16, 16)  # x, y, w, h
NOISE_SEED = 20260722

# 闪烁视频：中央区域每 BLINK_PERIOD 帧亮一次（30fps → 5Hz）
BLINK_FRAME_COUNT = 60
BLINK_PERIOD = 6
BLINK_RECT = (12, 12, 8, 8)  # x, y, w, h

# 运动视频：8×8 白块每帧右移 2 像素
MOTION_SIZE = 64
MOTION_FRAME_COUNT = 20
MOTION_BLOCK = 8
MOTION_STEP = 2
MOTION_Y = 28
MOTION_X0 = 4

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


def _encode_lossless_verified(
    path: Path, expected: list[np.ndarray], fps: int, label: str,
) -> Path:
    """按 libx264rgb(qp=0) → ffv1 依次尝试无损编码并逐帧回读校验。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    attempts: list[tuple[str, str, dict[str, str]]] = [
        ("libx264rgb", "rgb24", {"qp": "0", "preset": "veryfast"}),
        ("ffv1", "bgr0", {}),
    ]
    errors: list[str] = []
    for codec, pix_fmt, options in attempts:
        try:
            _encode(path, codec, pix_fmt, options, expected, fps)
        except (av.FFmpegError, ValueError) as exc:
            errors.append(f"{codec}: 编码失败（{exc}）")
            continue
        decoded = _decode_all(path)
        if len(decoded) == len(expected) and all(
            np.array_equal(a, b) for a, b in zip(decoded, expected)
        ):
            return path
        errors.append(f"{codec}: 回读校验不一致")
    raise RuntimeError(
        f"无法生成无损{label}测试视频，尝试结果：" + "；".join(errors)
    )


def generate_test_video(path: Path) -> Path:
    """生成无损测试视频并逐帧回读校验，失败抛 RuntimeError。"""
    expected = [make_frame(t) for t in range(FRAME_COUNT)]
    return _encode_lossless_verified(path, expected, FPS, "")


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


def make_noise_frames() -> list[np.ndarray]:
    """构造噪声视频全部帧：固定种子均匀噪声，中央区域幅度减半。"""
    rng = np.random.default_rng(NOISE_SEED)
    x, y, w, h = NOISE_QUIET_RECT
    frames = []
    for _t in range(NOISE_FRAME_COUNT):
        arr = rng.integers(
            0, 256, size=(NOISE_SIZE, NOISE_SIZE, 3), dtype=np.uint8
        )
        # 中央区域围绕 128 减半波动：时间标准差约为外围的一半
        quiet = arr[y : y + h, x : x + w].astype(np.int16)
        arr[y : y + h, x : x + w] = (128 + (quiet - 128) // 2).astype(np.uint8)
        frames.append(arr)
    return frames


def generate_noise_video(path: Path) -> Path:
    """生成"噪点藏区域"无损测试视频：temporal_reduce(std) 应显出中央区域。"""
    return _encode_lossless_verified(
        path, make_noise_frames(), FPS, "噪声"
    )


def make_blink_frame(t: int) -> np.ndarray:
    """闪烁视频第 t 帧：暗背景，中央区域按周期 6 帧、占空比 1/2 闪烁。

    方波（而非单帧脉冲）保证基波幅度显著大于谐波，主频检测无歧义。
    """
    arr = np.full((HEIGHT, WIDTH, 3), 30, dtype=np.uint8)
    if t % BLINK_PERIOD < BLINK_PERIOD // 2:
        x, y, w, h = BLINK_RECT
        arr[y : y + h, x : x + w] = 230
    return arr


def generate_blink_video(path: Path) -> Path:
    """生成周期闪烁无损测试视频：30fps、周期 6 帧方波 → 主频 5Hz。"""
    expected = [make_blink_frame(t) for t in range(BLINK_FRAME_COUNT)]
    return _encode_lossless_verified(path, expected, FPS, "闪烁")


def make_motion_frame(t: int) -> np.ndarray:
    """运动视频第 t 帧：8×8 白块位于 (x=MOTION_X0+2t, y=MOTION_Y)。"""
    arr = np.zeros((MOTION_SIZE, MOTION_SIZE, 3), dtype=np.uint8)
    x = MOTION_X0 + MOTION_STEP * t
    arr[MOTION_Y : MOTION_Y + MOTION_BLOCK, x : x + MOTION_BLOCK] = 255
    return arr


def generate_motion_video(path: Path) -> Path:
    """生成匀速右移白块无损测试视频：光流主方向应约为 0°（向右）。"""
    expected = [make_motion_frame(t) for t in range(MOTION_FRAME_COUNT)]
    return _encode_lossless_verified(path, expected, FPS, "运动")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("test_video.mkv")
    generate_test_video(target)
    print(f"已生成无损测试视频：{target}")
