"""PixelProbe 错误类型定义。

每个错误携带稳定的错误码（供 JSON 输出）和 CLI 退出码。
退出码约定：
0 成功 / 1 一般运行错误 / 2 参数错误 / 3 文件不存在 / 4 媒体格式不支持
5 坐标越界 / 6 帧或时间越界 / 7 解码失败 / 8 输出文件写入失败
"""

from __future__ import annotations


class PixelProbeError(Exception):
    """所有 PixelProbe 业务错误的基类。"""

    code: str = "PIXELPROBE_ERROR"
    exit_code: int = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        """转换为稳定的 JSON 错误对象。"""
        data: dict = {"code": self.code, "message": self.message}
        if self.hint:
            data["hint"] = self.hint
        return data


class MediaNotFoundError(PixelProbeError):
    code = "FILE_NOT_FOUND"
    exit_code = 3


class UnsupportedMediaError(PixelProbeError):
    code = "UNSUPPORTED_MEDIA"
    exit_code = 4


class DecodeError(PixelProbeError):
    code = "DECODE_FAILED"
    exit_code = 7


class CoordinateOutOfRangeError(PixelProbeError):
    code = "COORDINATE_OUT_OF_RANGE"
    exit_code = 5


class FrameOutOfRangeError(PixelProbeError):
    code = "FRAME_OUT_OF_RANGE"
    exit_code = 6


class TimeOutOfRangeError(PixelProbeError):
    code = "TIME_OUT_OF_RANGE"
    exit_code = 6


class InvalidRangeError(PixelProbeError):
    code = "INVALID_RANGE"
    exit_code = 2


class OutputWriteError(PixelProbeError):
    code = "OUTPUT_WRITE_FAILED"
    exit_code = 8
