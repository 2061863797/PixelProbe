"""生成 README 示例用的模拟录屏视频，结果可逐帧复现。

画面 640×360、30 帧/秒、共 120 帧（4 秒），无损编码，像素值与下面的定义完全一致：

- 浅灰背景 (200,200,200)；
- 第 30 帧起，矩形 rect(240,150,160,60) 出现蓝色按钮 (40,90,220)；
- 第 60 帧整帧闪白 (255,255,255)，只持续 1 帧；
- 一个 20×20 的深色方块从左向右每帧移动 4 像素，第 80～109 帧停住不动（画面冻结 1 秒）。

用法：python examples/make_demo_video.py demo.mkv
"""
from __future__ import annotations

import sys
from pathlib import Path

import av
import numpy as np

WIDTH, HEIGHT, FPS, FRAMES = 640, 360, 30, 120


def frame_at(index: int) -> np.ndarray:
    image = np.full((HEIGHT, WIDTH, 3), 200, dtype=np.uint8)
    if index >= 30:
        image[150:210, 240:400] = (40, 90, 220)
    # 第 80～109 帧方块停在第 79 帧的位置
    step = index if index < 80 else (79 if index < 110 else index - 30)
    x = 10 + step * 4
    image[300:320, x:x + 20] = (30, 30, 30)
    if index == 60:
        image[:] = 255
    return image


def main(target: Path) -> None:
    with av.open(str(target), "w") as container:
        stream = container.add_stream("ffv1", rate=FPS)
        stream.width, stream.height, stream.pix_fmt = WIDTH, HEIGHT, "bgr0"
        for index in range(FRAMES):
            frame = av.VideoFrame.from_ndarray(frame_at(index), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    print(target)


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "demo.mkv"))
