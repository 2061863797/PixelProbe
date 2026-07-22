"""图片读取器（基于 Pillow）。

返回帧统一为 [height, width, 3] uint8 RGB。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError

from pixelprobe.models.errors import DecodeError, UnsupportedMediaError
from pixelprobe.models.media_info import MediaInfo
from pixelprobe.utils.paths import file_size
from pixelprobe.utils.validation import ensure_file_exists


class ImageReader:
    """单张图片读取器。"""

    def __init__(self) -> None:
        self._img: Image.Image | None = None
        self._path: Path | None = None

    def open(self, path: Path) -> None:
        self._path = ensure_file_exists(Path(path))
        try:
            img = Image.open(self._path)
            img.load()
        except UnidentifiedImageError as exc:
            raise UnsupportedMediaError(
                f"无法识别的图片格式：{self._path}",
                hint="支持 PNG / JPEG / BMP / WebP 等常见格式",
            ) from exc
        except OSError as exc:
            raise DecodeError(f"图片解码失败：{self._path}（{exc}）") from exc
        self._img = img

    def get_info(self) -> MediaInfo:
        assert self._img is not None and self._path is not None
        return MediaInfo(
            path=str(self._path),
            media_type="image",
            width=self._img.width,
            height=self._img.height,
            channels=len(self._img.getbands()),
            color_mode=self._img.mode,
            codec=(self._img.format or "").lower() or None,
            file_size_bytes=file_size(self._path),
        )

    def get_frame(self) -> np.ndarray:
        """返回 RGB uint8 数组，形状 [height, width, 3]。"""
        assert self._img is not None
        return np.asarray(self._img.convert("RGB"), dtype=np.uint8)

    def close(self) -> None:
        if self._img is not None:
            self._img.close()
            self._img = None

    def __enter__(self) -> "ImageReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
