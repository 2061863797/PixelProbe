"""确定性帧变换算子的规范与颜色数学。"""

import numpy as np

from pixelprobe.operators.base import HaloSpec, OperatorSpec


FRAME_DIFFERENCE_OPERATOR_SPEC = OperatorSpec(
    name="feature.frame_difference",
    version="1.0.0",
    category="transform",
    deterministic="bit_exact",
    stateful=True,
    chunkable=True,
    cacheable=True,
    temporal_halo=HaloSpec(before=1),
    supported_dtypes=("uint8", "float64"),
    config_schema_id="pixelprobe.operator.frame_difference.v1",
)

COLOR_CONVERSION_OPERATOR_SPEC = OperatorSpec(
    name="feature.color_conversion",
    version="1.0.0",
    category="transform",
    deterministic="tolerance",
    stateful=False,
    chunkable=True,
    cacheable=True,
    supported_dtypes=("uint8", "float32"),
    config_schema_id="pixelprobe.operator.color_conversion.v1",
)


def rgb_to_grayscale(frame: np.ndarray, weights: tuple[float, float, float]) -> np.ndarray:
    values = frame.astype(np.float32)
    return np.tensordot(values, np.asarray(weights, dtype=np.float32), axes=([-1], [0])).astype(np.float32)


def rgb_to_hsv(frame: np.ndarray) -> np.ndarray:
    rgb = frame.astype(np.float32) / 255.0
    maximum = rgb.max(axis=-1)
    minimum = rgb.min(axis=-1)
    delta = maximum - minimum
    hue = np.zeros_like(maximum)
    nonzero = delta > 0
    red = nonzero & (maximum == rgb[..., 0])
    green = nonzero & (maximum == rgb[..., 1])
    blue = nonzero & (maximum == rgb[..., 2])
    hue[red] = np.mod((rgb[..., 1][red] - rgb[..., 2][red]) / delta[red], 6.0)
    hue[green] = (rgb[..., 2][green] - rgb[..., 0][green]) / delta[green] + 2.0
    hue[blue] = (rgb[..., 0][blue] - rgb[..., 1][blue]) / delta[blue] + 4.0
    hue *= 60.0
    saturation = np.divide(
        delta, maximum, out=np.zeros_like(delta), where=maximum > 0,
    )
    return np.stack((hue, saturation, maximum), axis=-1).astype(np.float32)


def hsv_to_rgb(values: np.ndarray) -> np.ndarray:
    hue = np.mod(values[..., 0], 360.0) / 60.0
    saturation = np.clip(values[..., 1], 0.0, 1.0)
    brightness = np.clip(values[..., 2], 0.0, 1.0)
    chroma = brightness * saturation
    x = chroma * (1.0 - np.abs(np.mod(hue, 2.0) - 1.0))
    zero = np.zeros_like(chroma)
    sectors = np.floor(hue).astype(np.int8) % 6
    choices = (
        (chroma, x, zero), (x, chroma, zero), (zero, chroma, x),
        (zero, x, chroma), (x, zero, chroma), (chroma, zero, x),
    )
    rgb = np.empty((*values.shape[:-1], 3), dtype=np.float32)
    for sector, components in enumerate(choices):
        mask = sectors == sector
        for channel, component in enumerate(components):
            rgb[..., channel][mask] = component[mask]
    rgb += (brightness - chroma)[..., None]
    return np.clip(np.rint(rgb * 255.0), 0, 255).astype(np.uint8)


def rgb_to_lab(frame: np.ndarray) -> np.ndarray:
    srgb = frame.astype(np.float32) / 255.0
    linear = np.where(
        srgb <= 0.04045, srgb / 12.92,
        np.power((srgb + 0.055) / 1.055, 2.4),
    )
    xyz = linear @ np.asarray([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ], dtype=np.float32).T
    xyz /= np.asarray((0.95047, 1.0, 1.08883), dtype=np.float32)
    epsilon = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    transformed = np.where(
        xyz > epsilon, np.cbrt(xyz), (kappa * xyz + 16.0) / 116.0,
    )
    return np.stack((
        116.0 * transformed[..., 1] - 16.0,
        500.0 * (transformed[..., 0] - transformed[..., 1]),
        200.0 * (transformed[..., 1] - transformed[..., 2]),
    ), axis=-1).astype(np.float32)


def lab_to_rgb(values: np.ndarray) -> np.ndarray:
    fy = (values[..., 0] + 16.0) / 116.0
    fx = fy + values[..., 1] / 500.0
    fz = fy - values[..., 2] / 200.0
    epsilon = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    f = np.stack((fx, fy, fz), axis=-1)
    cubed = f ** 3
    xyz = np.where(cubed > epsilon, cubed, (116.0 * f - 16.0) / kappa)
    xyz *= np.asarray((0.95047, 1.0, 1.08883), dtype=np.float32)
    linear = xyz @ np.asarray([
        [3.2404542, -1.5371385, -0.4985314],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0556434, -0.2040259, 1.0572252],
    ], dtype=np.float32).T
    srgb = np.where(
        linear <= 0.0031308, 12.92 * linear,
        1.055 * np.power(np.clip(linear, 0, None), 1.0 / 2.4) - 0.055,
    )
    return np.clip(np.rint(srgb * 255.0), 0, 255).astype(np.uint8)
