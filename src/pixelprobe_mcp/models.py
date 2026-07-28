"""PixelProbe MCP 的输入、输出与分页模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """禁止未知字段的 MCP 输入基类。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PointInput(StrictModel):
    x: int = Field(description="原始媒体存储坐标 x，从 0 开始", ge=0)
    y: int = Field(description="原始媒体存储坐标 y，从 0 开始", ge=0)


class RectInput(StrictModel):
    x: int = Field(description="矩形左上角 x", ge=0)
    y: int = Field(description="矩形左上角 y", ge=0)
    width: int = Field(description="矩形宽度，采用半开区间", ge=1)
    height: int = Field(description="矩形高度，采用半开区间", ge=1)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height


class FrameSelector(StrictModel):
    frame: int | None = Field(default=None, description="显示顺序帧号，从 0 开始", ge=0)
    time_seconds: float | None = Field(
        default=None, description="归一化时间轴上的秒数", ge=0,
    )

    @model_validator(mode="after")
    def validate_exclusive(self) -> "FrameSelector":
        if self.frame is not None and self.time_seconds is not None:
            raise ValueError("frame 与 time_seconds 不能同时提供")
        return self


class InspectInput(StrictModel):
    media_path: str = Field(description="允许读取根目录内的本地图片或视频路径", min_length=1)
    detail: Literal["quick", "standard"] = Field(
        default="quick",
        description="quick 只读基础信息；standard 对受限长度视频增加代表帧、变化和异常扫描",
    )
    offset: int = Field(default=0, description="事件和异常列表分页偏移", ge=0)
    limit: int = Field(default=20, description="每类最多返回多少项", ge=1, le=100)


class FrameInput(FrameSelector):
    media_path: str = Field(description="允许读取根目录内的本地图片或视频路径", min_length=1)
    crop: RectInput | None = Field(
        default=None, description="可选原始分辨率裁剪；不会缩放或插值",
    )


class PixelInput(FrameSelector):
    media_path: str = Field(description="允许读取根目录内的本地图片或视频路径", min_length=1)
    points: list[PointInput] = Field(
        description="需要精确读取的原始像素坐标", min_length=1, max_length=256,
    )


class RegionInput(FrameSelector):
    media_path: str = Field(description="允许读取根目录内的本地图片或视频路径", min_length=1)
    rect: RectInput = Field(description="要统计的原始分辨率矩形")


class ChangesInput(StrictModel):
    media_path: str = Field(description="允许读取根目录内的本地视频路径", min_length=1)
    point: PointInput | None = Field(default=None, description="单像素变化源")
    rect: RectInput | None = Field(default=None, description="矩形区域变化源")
    grid: RectInput | None = Field(default=None, description="网格采样范围")
    grid_step: int | None = Field(default=None, description="网格采样间隔", ge=1)
    start_frame: int | None = Field(default=None, description="闭区间起始帧", ge=0)
    end_frame: int | None = Field(default=None, description="闭区间结束帧", ge=0)
    start_seconds: float | None = Field(default=None, description="闭区间起始秒数", ge=0)
    end_seconds: float | None = Field(default=None, description="闭区间结束秒数", ge=0)
    sample_every: int = Field(default=1, description="每隔多少帧采样", ge=1)
    offset: int = Field(default=0, description="记录分页偏移", ge=0)
    limit: int = Field(default=50, description="本页最多记录数", ge=1, le=200)
    sort: Literal["timeline", "score"] = Field(
        default="score", description="按时间顺序或变化得分降序返回",
    )

    @model_validator(mode="after")
    def validate_modes(self) -> "ChangesInput":
        if sum(value is not None for value in (self.point, self.rect, self.grid)) > 1:
            raise ValueError("point、rect、grid 最多提供一个")
        if self.grid_step is not None and self.grid is None:
            raise ValueError("grid_step 必须与 grid 一起提供")
        uses_frames = self.start_frame is not None or self.end_frame is not None
        uses_seconds = self.start_seconds is not None or self.end_seconds is not None
        if uses_frames and uses_seconds:
            raise ValueError("帧范围与时间范围不能混用")
        return self


class GenerateInput(StrictModel):
    media_path: str = Field(description="允许读取根目录内的源媒体路径", min_length=1)
    request: dict[str, Any] = Field(description="PixelProbe RepresentationRequest 对象")
    output_name: str | None = Field(
        default=None,
        description="可选 Bundle 名称；只能包含字母、数字、点、下划线和短横线",
        pattern=r"^[A-Za-z0-9._-]{1,80}$",
    )


class BundleListInput(StrictModel):
    bundle_path: str = Field(description="允许读取根目录内的 .bundle 目录", min_length=1)
    kind: Literal["data", "preview", "index", "metadata", "log"] | None = None
    offset: int = Field(default=0, description="Artifact 分页偏移", ge=0)
    limit: int = Field(default=20, description="本页最多 Artifact 数", ge=1, le=100)
    verify: Literal["full", "metadata"] = Field(
        default="metadata", description="metadata 只校验结构与大小；full 会校验全部 SHA-256，可能耗时较长",
    )


class AxisSelection(StrictModel):
    index: int | None = Field(default=None, description="选取单个轴索引", ge=0)
    start: int | None = Field(default=None, description="半开切片起点", ge=0)
    stop: int | None = Field(default=None, description="半开切片终点", ge=0)
    step: int = Field(default=1, description="正向切片步长", ge=1)

    @model_validator(mode="after")
    def validate_choice(self) -> "AxisSelection":
        if self.index is not None and (self.start is not None or self.stop is not None):
            raise ValueError("index 不能与 start/stop 同时使用")
        return self


class ArtifactReadInput(StrictModel):
    bundle_path: str = Field(description="允许读取根目录内的 .bundle 目录", min_length=1)
    artifact_id: str = Field(description="data Artifact 的稳定 ID", min_length=1, max_length=96)
    selection: list[AxisSelection] | None = Field(
        default=None,
        description="每个轴一个选择器；省略时只返回结构，不加载数值",
        max_length=16,
    )
    max_values: int = Field(default=256, description="本次最多返回的标量数", ge=1, le=4096)
    verify: Literal["full", "metadata"] = Field(default="metadata")


class NextAction(BaseModel):
    tool: str
    reason: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Pagination(BaseModel):
    total: int = Field(ge=0)
    count: int = Field(ge=0)
    offset: int = Field(ge=0)
    has_more: bool
    next_offset: int | None = Field(default=None, ge=0)


class ToolEnvelope(BaseModel):
    """所有结构化 MCP 工具共用的稳定响应包。"""

    success: Literal[True] = True
    data: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)
    pagination: Pagination | None = None
