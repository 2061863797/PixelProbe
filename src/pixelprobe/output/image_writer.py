"""PNG 输出与最近邻缩放。

放大一律使用最近邻（整数倍用 np.repeat 实现，逐格复制、零插值）；
预览缩小保持宽高比，同样使用 NEAREST，保证确定性。
禁止双线性 / 双三次插值。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from pixelprobe.models.errors import OutputWriteError
from pixelprobe.utils.paths import atomic_output


def scale_nearest(arr: np.ndarray, scale_x: int, scale_y: int) -> np.ndarray:
    """整数倍最近邻放大：每个原始颜色格变为 scale_x × scale_y 方块。"""
    if scale_x == 1 and scale_y == 1:
        return arr
    return np.repeat(np.repeat(arr, scale_y, axis=0), scale_x, axis=1)


def fit_within(
    arr: np.ndarray, max_width: int | None, max_height: int | None
) -> np.ndarray:
    """保持宽高比缩小到 max_width / max_height 之内（NEAREST）。

    只缩小不放大；未指定上限或已在范围内时原样返回。
    """
    h, w = arr.shape[:2]
    ratio = 1.0
    if max_width is not None and w > max_width:
        ratio = min(ratio, max_width / w)
    if max_height is not None and h > max_height:
        ratio = min(ratio, max_height / h)
    if ratio >= 1.0:
        return arr
    new_w = max(1, int(w * ratio))
    new_h = max(1, int(h * ratio))
    img = Image.fromarray(arr).resize(
        (new_w, new_h), Image.Resampling.NEAREST
    )
    return np.asarray(img, dtype=np.uint8)


def save_png(arr: np.ndarray, path: Path) -> None:
    """把 [H, W, 3] uint8 RGB 数组写为 PNG（原子写入）。"""
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise OutputWriteError(
            f"内部错误：期望 [H, W, 3] 数组，实际形状 {arr.shape}"
        )
    with atomic_output(Path(path)) as tmp:
        Image.fromarray(np.ascontiguousarray(arr)).save(
            tmp, format="PNG"
        )
