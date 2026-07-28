"""图片读取器（基于 Pillow）。

同时提供历史兼容的显示 RGB8 帧和可验证的原生样本数组。两者不能混用：
显示帧可为 RGBA、调色板或高位深图片提供旧算子所需的 RGB8 输入；原生样本
保留 Pillow 解码后的模式、通道与位深，供精确像素查询和 Artifact 保存使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError

from pixelprobe.models.errors import DecodeError, UnsupportedMediaError
from pixelprobe.models.media_info import MediaInfo
from pixelprobe.utils.paths import file_size
from pixelprobe.utils.validation import ensure_file_exists


@dataclass(slots=True, frozen=True)
class NativeImageMetadata:
    """图片原生样本的可验证描述。

    ``array`` 的值由 :meth:`ImageReader.get_native_frame` 返回。对于调色板图，
    它是调色板索引而非展开后的 RGB；这正是 ``stored_sample`` 与显示 RGB 必须
    分开保存的原因。
    """

    mode: str
    source_format: str | None
    dtype: str
    shape: tuple[int, ...]
    bands: tuple[str, ...]
    channel_count: int
    bits_per_sample: int | None
    has_alpha: bool
    alpha_representation: str
    sample_semantics: str


@dataclass(slots=True, frozen=True)
class ImageContentAnalysis:
    """供导入概览使用的图片结构统计。

    通道、调色板和 Alpha 统计来自完整图片；规则纹理检测只检查原始分辨率下的
    有界区域，并通过 ``pattern_coverage`` 明确其覆盖范围，不能据此断言压缩或造假。
    """

    total_pixels: int
    stored_mode: str
    stored_bands: tuple[str, ...]
    stored_channel_count: int
    analysis_display_mode: str
    analysis_display_channel_count: int
    visual_output_mode: str
    visual_output_channel_count: int
    is_indexed: bool
    palette_entry_count: int | None
    used_palette_index_count: int | None
    palette_usage_ratio: float | None
    alpha_level_count: int
    transparent_pixel_count: int
    partially_transparent_pixel_count: int
    opaque_pixel_count: int
    transparent_ratio: float
    partially_transparent_ratio: float
    opaque_ratio: float
    transparency_kind: str
    pattern_assessment: str
    pattern_accuracy: str
    pattern_coverage: str
    pattern_sample_rect: tuple[int, int, int, int] | None
    pattern_horizontal_period_pixels: int | None
    pattern_horizontal_correlation: float | None
    pattern_vertical_period_pixels: int | None
    pattern_vertical_correlation: float | None
    pattern_high_frequency_stddev: float | None


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
        """返回历史兼容的 RGB uint8 数组，形状 [height, width, 3]。

        此接口保持旧 CLI 的颜色转换行为。统一表示引擎必须调用
        :meth:`get_engine_frame`，避免把 Alpha、位深或原始颜色模式的丢失
        误称为精确解码。
        """
        return self.get_engine_frame()

    def get_native_frame(self) -> np.ndarray:
        """返回 Pillow 解码后的原生样本数组，不做 RGB8 展开或缩放。

        返回独立、C 连续的副本，因此在 Reader 关闭后依然可读取。调用方必须
        配合 :meth:`get_native_metadata` 解释调色板索引、位深和 Alpha 语义。
        """
        assert self._img is not None
        return np.array(self._img, copy=True, order="C")

    def get_native_metadata(self) -> NativeImageMetadata:
        """返回 :meth:`get_native_frame` 对应的模式、位深与 Alpha 元数据。"""
        assert self._img is not None
        array = np.asarray(self._img)
        dtype = np.dtype(array.dtype)
        if dtype.kind in {"u", "i", "f"}:
            bits_per_sample: int | None = dtype.itemsize * 8
        else:
            bits_per_sample = None
        bands = tuple(self._img.getbands())
        has_alpha = "A" in bands or "transparency" in self._img.info
        if "A" in bands:
            alpha_representation = "straight"
        elif "transparency" in self._img.info:
            alpha_representation = "palette_or_color_key"
        else:
            alpha_representation = "none"
        source_format = (self._img.format or "").lower() or None
        # 只为可明确识别的无损常见格式声明 stored_sample；有损/未知格式的
        # 原生数组仍可精确读取，但它是指定解码器的 decoded_sample。
        sample_semantics = (
            "stored_sample"
            if source_format in {"png", "bmp", "gif", "ppm", "pgm", "pbm"}
            else "decoded_sample"
        )
        return NativeImageMetadata(
            mode=self._img.mode,
            source_format=source_format,
            dtype=str(dtype),
            shape=tuple(int(item) for item in array.shape),
            bands=bands,
            channel_count=len(bands),
            bits_per_sample=bits_per_sample,
            has_alpha=has_alpha,
            alpha_representation=alpha_representation,
            sample_semantics=sample_semantics,
        )

    def get_content_analysis(self) -> ImageContentAnalysis:
        """返回不混淆存储样本与显示像素的导入统计。"""
        assert self._img is not None
        metadata = self.get_native_metadata()
        total_pixels = self._img.width * self._img.height

        is_indexed = self._img.mode == "P"
        palette_entry_count: int | None = None
        used_palette_index_count: int | None = None
        palette_usage_ratio: float | None = None
        if is_indexed:
            palette = self._img.palette
            palette_channels = len(palette.mode) if palette is not None else 0
            palette_bytes = len(palette.palette) if palette is not None else 0
            if palette_channels > 0:
                palette_entry_count = palette_bytes // palette_channels
            histogram = self._img.histogram()
            used_palette_index_count = sum(count > 0 for count in histogram[:256])
            if palette_entry_count:
                palette_usage_ratio = round(
                    used_palette_index_count / palette_entry_count, 6,
                )

        alpha_histogram = self._alpha_histogram(metadata)
        alpha_level_count = sum(count > 0 for count in alpha_histogram)
        transparent_pixel_count = alpha_histogram[0]
        opaque_pixel_count = alpha_histogram[255]
        partially_transparent_pixel_count = (
            total_pixels - transparent_pixel_count - opaque_pixel_count
        )
        if not metadata.has_alpha:
            transparency_kind = "none"
        elif partially_transparent_pixel_count > 0:
            transparency_kind = "continuous"
        elif transparent_pixel_count > 0:
            transparency_kind = "binary"
        else:
            transparency_kind = "explicit_but_opaque"

        pattern = self._assess_regular_pattern()
        ratio_base = total_pixels or 1
        return ImageContentAnalysis(
            total_pixels=total_pixels,
            stored_mode=metadata.mode,
            stored_bands=metadata.bands,
            stored_channel_count=metadata.channel_count,
            analysis_display_mode="RGB",
            analysis_display_channel_count=3,
            visual_output_mode="RGBA" if metadata.has_alpha else "RGB",
            visual_output_channel_count=4 if metadata.has_alpha else 3,
            is_indexed=is_indexed,
            palette_entry_count=palette_entry_count,
            used_palette_index_count=used_palette_index_count,
            palette_usage_ratio=palette_usage_ratio,
            alpha_level_count=alpha_level_count,
            transparent_pixel_count=transparent_pixel_count,
            partially_transparent_pixel_count=partially_transparent_pixel_count,
            opaque_pixel_count=opaque_pixel_count,
            transparent_ratio=round(transparent_pixel_count / ratio_base, 6),
            partially_transparent_ratio=round(
                partially_transparent_pixel_count / ratio_base, 6,
            ),
            opaque_ratio=round(opaque_pixel_count / ratio_base, 6),
            transparency_kind=transparency_kind,
            pattern_assessment=pattern[0],
            pattern_accuracy=pattern[1],
            pattern_coverage=pattern[2],
            pattern_sample_rect=pattern[3],
            pattern_horizontal_period_pixels=pattern[4],
            pattern_horizontal_correlation=pattern[5],
            pattern_vertical_period_pixels=pattern[6],
            pattern_vertical_correlation=pattern[7],
            pattern_high_frequency_stddev=pattern[8],
        )

    def _alpha_histogram(self, metadata: NativeImageMetadata) -> list[int]:
        """把各种 Alpha 表示统一成 8 位直方图，统计完整图片。"""
        assert self._img is not None
        total_pixels = self._img.width * self._img.height
        if not metadata.has_alpha:
            histogram = [0] * 256
            histogram[255] = total_pixels
            return histogram
        rgba = self._img.convert("RGBA")
        alpha = rgba.getchannel("A")
        try:
            histogram = alpha.histogram()
        finally:
            alpha.close()
            rgba.close()
        if len(histogram) != 256:
            raise DecodeError("图片 Alpha 通道无法转换为 8 位直方图")
        return [int(count) for count in histogram]

    def _assess_regular_pattern(
        self,
    ) -> tuple[
        str, str, str, tuple[int, int, int, int] | None,
        int | None, float | None, int | None, float | None, float | None,
    ]:
        """检测二维规则高频纹理候选，不把数学相关性解释成压缩原因。"""
        assert self._img is not None
        sample_width = min(self._img.width, 1024)
        sample_height = min(self._img.height, 1024)
        if sample_width < 32 or sample_height < 32:
            return (
                "insufficient_data", "unknown", "none", None,
                None, None, None, None, None,
            )

        left = (self._img.width - sample_width) // 2
        top = (self._img.height - sample_height) // 2
        sample_rect = (left, top, sample_width, sample_height)
        sample = self._img.crop((left, top, left + sample_width, top + sample_height))
        grayscale = sample.convert("L")
        try:
            values = np.array(grayscale, dtype=np.float32, copy=True, order="C")
        finally:
            grayscale.close()
            sample.close()

        residual = (
            values[1:, 1:] - values[:-1, 1:]
            - values[1:, :-1] + values[:-1, :-1]
        )
        residual -= float(residual.mean())
        variance = float(np.mean(residual * residual))
        high_frequency_stddev = float(np.sqrt(variance))
        coverage = (
            "full"
            if sample_width == self._img.width and sample_height == self._img.height
            else "sampled"
        )
        accuracy = "derived" if coverage == "full" else "estimated"
        if variance < 64.0:
            return (
                "not_detected", accuracy, coverage, sample_rect,
                None, None, None, None, round(high_frequency_stddev, 6),
            )

        max_lag = min(16, residual.shape[0] // 4, residual.shape[1] // 4)
        horizontal = [
            float(np.mean(residual[:, :-lag] * residual[:, lag:]) / variance)
            for lag in range(2, max_lag + 1)
        ]
        vertical = [
            float(np.mean(residual[:-lag, :] * residual[lag:, :]) / variance)
            for lag in range(2, max_lag + 1)
        ]
        horizontal_index = int(np.argmax(horizontal))
        vertical_index = int(np.argmax(vertical))
        horizontal_period = horizontal_index + 2
        vertical_period = vertical_index + 2
        horizontal_correlation = horizontal[horizontal_index]
        vertical_correlation = vertical[vertical_index]
        candidate = (
            horizontal_correlation >= 0.78
            and vertical_correlation >= 0.78
            and abs(horizontal_period - vertical_period) <= 1
        )
        return (
            "candidate" if candidate else "not_detected",
            accuracy,
            coverage,
            sample_rect,
            horizontal_period,
            round(horizontal_correlation, 6),
            vertical_period,
            round(vertical_correlation, 6),
            round(high_frequency_stddev, 6),
        )

    def engine_sample_semantics(self) -> str:
        """返回计算用 RGB8 帧的语义，绝不把显示转换当作原生样本。"""
        metadata = self.get_native_metadata()
        if (
            metadata.mode == "RGB"
            and metadata.dtype == "uint8"
            and metadata.shape == (self._img.height, self._img.width, 3)
        ):
            return "decoded_rgb8"
        return "display_rgb8"

    def engine_conversion_flags(self) -> tuple[str, ...]:
        """说明计算帧是否经过显式显示转换。"""
        if self.engine_sample_semantics() == "decoded_rgb8":
            return ()
        return ("DISPLAY_RGB8_CONVERSION", "NATIVE_IMAGE_PRESERVED")

    def get_engine_frame(self) -> np.ndarray:
        """返回供现有 RGB 算子计算的显示 RGB8 帧。

        RGBA、灰度、调色板、CMYK 与高位深图片会经过 Pillow 的显式 RGB8
        显示转换；原生样本不会丢弃，必须从 :meth:`get_native_frame` 与
        :meth:`get_native_metadata` 获取。调用方还必须记录
        :meth:`engine_sample_semantics` 和 :meth:`engine_conversion_flags`，
        不得将该数组描述为原始像素值。
        """
        assert self._img is not None
        converted = self._img.convert("RGB")
        try:
            return np.array(converted, dtype=np.uint8, copy=True, order="C")
        finally:
            converted.close()

    def close(self) -> None:
        if self._img is not None:
            self._img.close()
            self._img = None

    def __enter__(self) -> "ImageReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
