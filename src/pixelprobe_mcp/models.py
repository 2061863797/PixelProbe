"""PixelProbe MCP 的输入、输出与分页模型（2.0）。

媒体分析统一走 CapabilityInput：能力名 + 媒体路径 + 地址文字 + 计算参数。
地址写法与命令行 --at 相同（见 docs/address.md）。路径一律经服务器白名单解析，
输出位置只由服务器决定，参数中不接受任何输出路径。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """禁止未知字段的 MCP 输入基类。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CapabilityInput(StrictModel):
    capability: str = Field(min_length=1, max_length=40, description="能力名，见 pixelprobe_system capabilities")
    media_path: str | None = Field(
        default=None, min_length=1, description="允许读取根目录内的图片、视频或结果包路径",
    )
    reference_path: str | None = Field(
        default=None, min_length=1, description="版本比较的第二个媒体（仅 compare_media）",
    )
    at: str | None = Field(
        default=None, max_length=2000,
        description='地址：时间 + 空间，如 "f=120 rect(400,200,200,150)"、"t=1s..3s step=2"、"cell(C4)"、"@event1"',
    )
    refs: dict[str, str] = Field(
        default_factory=dict, description="附加引用表：名称 → 地址文字，供 at 中的 @名称 使用",
    )
    options: dict[str, Any] = Field(default_factory=dict, description="能力的计算参数")
    images: bool = Field(default=True, description="是否返回图片（带坐标刻度的预览）")
    grid: bool = Field(default=False, description="在画面类图片上叠加 cell 网格（8×6，列 A～H、行 1～6）")
    max_side: int = Field(default=1024, ge=64, le=4096, description="返回图片的最长边上限（像素）")
    output_name: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9._-]{1,80}$",
        description="写结果包的能力可选的结果包名称；只能包含字母、数字、点、下划线和短横线",
    )
    offset: int = Field(default=0, ge=0, description="列表类结果的分页偏移")
    limit: int = Field(default=50, ge=1, le=200, description="列表类结果每页条数")


class JobIdInput(StrictModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$", description="提交任务返回的 ID")


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
