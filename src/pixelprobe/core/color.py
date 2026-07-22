"""颜色空间换算。

内部统一使用 RGB uint8（0～255）。
- HSV 输出范围固定：H 0～360，S 0～100，V 0～100；
- luminance：加权亮度近似 Y = 0.2126R + 0.7152G + 0.0722B（基于 0～255
  的 sRGB 分量，未线性化，历史字段保持不变）；
- luminance_linear：先做 sRGB 反伽马线性化再加权的相对亮度（×255，
  更符合物理光量）；
- Lab：CIELAB（D65 白点，标准 sRGB→XYZ→Lab 流程），L 0～100。
"""

from __future__ import annotations

import numpy as np

# ITU-R BT.709 亮度权重
_LUMA_WEIGHTS = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

# sRGB(线性) → XYZ 的 D65 矩阵（行分别为 X/Y/Z）
_SRGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
# D65 参考白点
_XYZ_WHITE = np.array([0.95047, 1.0, 1.08883], dtype=np.float64)


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """RGB 转 #RRGGBB 十六进制字符串。"""
    return f"#{r:02X}{g:02X}{b:02X}"


def rgb_to_hsv_array(rgb: np.ndarray) -> np.ndarray:
    """向量化 RGB→HSV。

    输入形状 [..., 3]（uint8），输出同形状 float64：
    H 0～360，S 0～100，V 0～100。灰色（max=min）时 H=0，黑色时 S=0。
    """
    arr = rgb.astype(np.float64) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = arr.max(axis=-1)
    mn = arr.min(axis=-1)
    delta = mx - mn

    h = np.zeros_like(mx)
    # 避免除零：delta==0 的位置 H 保持 0
    safe = delta > 0
    with np.errstate(invalid="ignore", divide="ignore"):
        h_r = np.mod((g - b) / delta, 6.0)
        h_g = (b - r) / delta + 2.0
        h_b = (r - g) / delta + 4.0
    h = np.where(safe & (mx == r), h_r, h)
    h = np.where(safe & (mx == g) & (mx != r), h_g, h)
    h = np.where(safe & (mx == b) & (mx != r) & (mx != g), h_b, h)
    h = h * 60.0

    s = np.where(mx > 0, delta / np.where(mx > 0, mx, 1.0), 0.0) * 100.0
    v = mx * 100.0
    return np.stack([h, s, v], axis=-1)


def luminance_array(rgb: np.ndarray) -> np.ndarray:
    """向量化加权亮度。输入 [..., 3] uint8，输出 [...] float64（0～255）。"""
    return rgb.astype(np.float64) @ _LUMA_WEIGHTS


def srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    """sRGB 反伽马线性化。输入 [..., 3] uint8，输出同形状 float64（0～1）。"""
    c = rgb.astype(np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def luminance_linear_array(rgb: np.ndarray) -> np.ndarray:
    """线性光相对亮度（先线性化再加权），输出 [...] float64（0～255 尺度）。"""
    return (srgb_to_linear(rgb) @ _SRGB_TO_XYZ[1]) * 255.0


def rgb_to_lab_array(rgb: np.ndarray) -> np.ndarray:
    """向量化 RGB→CIELAB（D65）。

    输入 [..., 3] uint8，输出同形状 float64：L 0～100，a/b 约 ±128。
    """
    xyz = srgb_to_linear(rgb) @ _SRGB_TO_XYZ.T
    ratio = xyz / _XYZ_WHITE
    epsilon = (6.0 / 29.0) ** 3
    f = np.where(
        ratio > epsilon,
        np.cbrt(ratio),
        ratio / (3.0 * (6.0 / 29.0) ** 2) + 4.0 / 29.0,
    )
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    return np.stack(
        [116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)], axis=-1
    )
