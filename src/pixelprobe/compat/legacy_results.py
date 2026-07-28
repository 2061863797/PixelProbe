"""把规范 Tensor 显式转换为 1.0 前的数组方向与 Preview 字段。"""

from __future__ import annotations

import numpy as np

from pixelprobe.domain.tensor import TensorField


def spacetime_array(tensor: TensorField, spatial_axis: str) -> np.ndarray:
    expected = ["time", spatial_axis, "channel"]
    if [axis.name for axis in tensor.axes] != expected:
        raise ValueError(f"时空 Tensor 轴必须为 {expected}")
    return tensor.data.materialize()


def timeline_matrix(tensor: TensorField) -> np.ndarray:
    """规范 `[time,path,channel]` 转为旧 `[path,time,channel]`。"""
    if [axis.name for axis in tensor.axes] != ["time", "path", "channel"]:
        raise ValueError("timeline Tensor 轴必须为 time,path,channel")
    return np.transpose(tensor.data.materialize(), (1, 0, 2))


def preview_image(tensor: TensorField) -> np.ndarray:
    if tensor.attributes.get("artifact_role") != "preview":
        raise ValueError("不能把 Data Tensor 当作 Preview 返回")
    image = tensor.data.materialize()
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("旧图片结果要求 [height,width,3] uint8")
    return image
