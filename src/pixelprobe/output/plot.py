"""纯 numpy/PIL 的曲线与伪彩渲染，供变化曲线、频谱图和差异热力图共用。

与 image_writer 一样只做确定性绘制：不依赖 matplotlib，
输出一律为 [H, W, 3] uint8 RGB 数组，由调用方决定内联返回或落盘。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from PIL import Image, ImageDraw

# 曲线图配色（深底浅线，便于模型和人眼读图）
_BG = (16, 16, 20)
_AXIS = (90, 90, 100)
_LINE = (80, 220, 120)
_SPAN = (90, 40, 40)
_MARKER = (240, 120, 80)


def render_curve(
    values: Sequence[float],
    width: int = 768,
    height: int = 256,
    *,
    markers: Sequence[int] | None = None,
    spans: Sequence[tuple[int, int]] | None = None,
    y_min: float | None = None,
    y_max: float | None = None,
) -> np.ndarray:
    """把一维数值序列渲染为折线图。

    横轴为序列下标（0 起，等距铺满宽度），纵轴为数值（下小上大）。
    markers 为需要竖线标注的下标；spans 为需要背景描色的下标闭区间
    （如事件区间）。y_min/y_max 缺省取数据最值（全平序列自动扩展量程）。
    """
    n = len(values)
    if n == 0:
        raise ValueError("render_curve 需要至少一个数值")
    data = np.asarray(values, dtype=np.float64)
    lo = float(np.min(data)) if y_min is None else float(y_min)
    hi = float(np.max(data)) if y_max is None else float(y_max)
    if hi - lo < 1e-12:
        lo, hi = lo - 0.5, hi + 0.5

    img = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(img)

    def x_at(i: int) -> float:
        return 0.0 if n == 1 else i * (width - 1) / (n - 1)

    def y_at(v: float) -> float:
        frac = (v - lo) / (hi - lo)
        return (height - 1) * (1.0 - frac)

    for s, e in spans or []:
        draw.rectangle(
            [x_at(max(0, s)), 0, x_at(min(n - 1, e)), height - 1], fill=_SPAN
        )
    for m in markers or []:
        if 0 <= m < n:
            x = x_at(m)
            draw.line([x, 0, x, height - 1], fill=_MARKER, width=1)
    # 基线（y=0 落在量程内时绘制）
    if lo <= 0.0 <= hi:
        y0 = y_at(0.0)
        draw.line([0, y0, width - 1, y0], fill=_AXIS, width=1)
    if n == 1:
        draw.point([(0, y_at(float(data[0])))], fill=_LINE)
    else:
        points = [(x_at(i), y_at(float(v))) for i, v in enumerate(data)]
        draw.line(points, fill=_LINE, width=1)
    return np.asarray(img, dtype=np.uint8)


def _fire_lut() -> np.ndarray:
    """黑→红→黄→白 的 256 级伪彩查找表 [256, 3] uint8。"""
    x = np.arange(256, dtype=np.float64) / 255.0
    r = np.clip(x * 3.0, 0.0, 1.0)
    g = np.clip(x * 3.0 - 1.0, 0.0, 1.0)
    b = np.clip(x * 3.0 - 2.0, 0.0, 1.0)
    return (np.stack([r, g, b], axis=1) * 255.0 + 0.5).astype(np.uint8)


_LUTS: dict[str, np.ndarray] = {"fire": _fire_lut()}


def apply_colormap(gray: np.ndarray, name: str = "fire") -> np.ndarray:
    """把 [H, W] uint8 灰度图映射为 [H, W, 3] uint8 伪彩图。

    name="gray" 时直接复制三通道（不做映射）。
    """
    if gray.ndim != 2:
        raise ValueError(f"apply_colormap 期望 [H, W] 灰度图，实际形状 {gray.shape}")
    gray = np.ascontiguousarray(gray, dtype=np.uint8)
    if name == "gray":
        return np.repeat(gray[:, :, None], 3, axis=2)
    lut = _LUTS.get(name)
    if lut is None:
        raise ValueError(f"未知伪彩方案：{name}（可选 gray/fire）")
    return lut[gray]
