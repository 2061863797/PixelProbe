"""生成 16×16 确定性测试图片。

规定：R = x*16，G = y*16，B = (x+y)*8。
写入后立即回读校验，保证素材可用于精确断言。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

SIZE = 16


def make_image_array() -> np.ndarray:
    """构造 [16, 16, 3] uint8 测试图案。"""
    coords = np.arange(SIZE)
    xx, yy = np.meshgrid(coords, coords)  # xx[y, x] = x, yy[y, x] = y
    return np.stack(
        [xx * 16, yy * 16, (xx + yy) * 8], axis=-1
    ).astype(np.uint8)


def generate_test_image(path: Path) -> Path:
    """生成测试图片并回读校验，校验失败抛 RuntimeError。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = make_image_array()
    Image.fromarray(expected).save(path, format="PNG")
    back = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
    if not np.array_equal(back, expected):
        raise RuntimeError(f"测试图片回读校验失败：{path}")
    return path


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("test_image.png")
    generate_test_image(target)
    print(f"已生成测试图片：{target}")
