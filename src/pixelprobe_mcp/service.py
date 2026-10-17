"""MCP 业务层（2.0）：经路径白名单与资源边界后，调用与命令行相同的能力函数。

本层只负责：解析并复验输入路径、决定受控输出位置、施加 MCP 资源边界、
把图片编码为带刻度的预览，以及控制响应大小。分析与结果组装全部在能力登记表中。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import ValidationError

from pixelprobe import core
from pixelprobe.address import Address, parse_address
from pixelprobe.artifacts import BundleReader
from pixelprobe.capabilities import Capability, Outcome, all_capabilities, capability
from pixelprobe.models.media_info import MediaInfo
from pixelprobe.operators.base import ResourcePolicy
from pixelprobe.output.annotate import Axis, annotate, frame_axis
from pixelprobe.session import media_sessions
from pixelprobe.version import __version__
from pixelprobe_mcp.config import FileIdentity, ServerConfig
from pixelprobe_mcp.models import (
    ArtifactReadInput,
    BundleListInput,
    CapabilityInput,
    NextAction,
    Pagination,
    ToolEnvelope,
)
from pixelprobe_mcp.resource_plan import MCP_INDEX_THUMB_BUDGET_BYTES

CHARACTER_LIMIT = 25_000
MCP_MAX_SCAN_SOURCE_FRAMES = 1_800
MCP_MAX_SOURCE_FRAMES = 3_600
MCP_MAX_GENERATE_SOURCE_FRAMES = 1_800
MCP_MAX_GRID_POINTS = 65_536
MCP_MAX_FRAME_WORKING_BYTES = 256 * 1024 * 1024
MCP_GENERATE_RESOURCES = ResourcePolicy(
    max_memory_bytes=256 * 1024 * 1024,
    max_temporary_bytes=2 * 1024 * 1024 * 1024,
    timeout_seconds=120.0,
    preferred_chunk_bytes=32 * 1024 * 1024,
    allow_partial=False,
)
SINGLE_FRAME = {"frame", "pixel", "region", "spectrum2d", "compare"}
SEQUENTIAL = {
    "changes", "scan", "find", "track", "sheet", "timeline", "xt", "yt", "reduce", "spectrum", "flow",
    "first_change", "compare_media", "quality_overview",
}
_NO_SEMANTICS = "只是数值线索，不能单独证明具体事件、物体或内容；请先查看候选时刻前、中、后的画面"
SEMANTIC_WARNINGS = {
    "changes": f"变化分数{_NO_SEMANTICS}",
    "scan": f"事件与异常候选{_NO_SEMANTICS}",
    "spectrum": f"频谱峰值{_NO_SEMANTICS}",
    "spectrum2d": f"空间频谱峰值{_NO_SEMANTICS}",
    "flow": f"光流{_NO_SEMANTICS}",
    "reduce": f"时间域合成图{_NO_SEMANTICS}",
}
# 列表字段按 offset/limit 分页返回，其余字段完整返回
PAGED_LISTS = (
    "frames", "times", "top", "events", "anomalies", "points", "pixels",
    "sheet_frames", "top_peaks", "peaks", "segments", "records", "matches",
)


class McpResourceLimitError(ValueError):
    """MCP 外层资源边界拒绝执行，以免分页被误解为计算限制。"""


class McpInputError(ValueError):
    """调用参数组合不成立；消息不含路径，可原样返回给 Agent。"""


AGENT_GUIDANCE = """你正在使用 PixelProbe 分析本地图片或视频。

工作原则：
1. 语义理解（人物、物体、文字、场景、构图）以 Agent 自身视觉能力为主；PixelProbe 负责
   帧号、时间、坐标、像素数值、变化定位和取证线索等确定性数据。
2. 先粗后细：pixelprobe_overview 了解全貌 → pixelprobe_find 定位时刻 →
   pixelprobe_look 带刻度查看 → pixelprobe_measure 读取精确数值。
3. 位置统一写在 at 参数里，例如 "f=120 rect(400,200,200,150)"、"t=1s..3s step=2"、
   "cell(C4)"、"@event1"。look 加 grid=true 会叠加 8×6 网格，可直接用格子名写地址。
4. 结果中的 refs（如 event1、anomaly2、seg3）可在后续 at 中用 @名称 引用，不必抄写帧号。
5. 返回的预览图会按 max_side 缩放并标注原始坐标刻度；需要精确数值时用 measure，
   不要从缩放后的预览读取像素值。
6. 不要仅凭变化分数、频谱或光流推断具体事件；先查看候选时刻前、中、后的画面再下结论。
7. 取证工具只给出可复核的线索与局限，不能单独证明伪造、压缩或事件发生。
8. 长视频先用 pixelprobe_index build 建立索引（wait=false 后台执行），之后整帧变化检测直接查表。
9. Preview 只用于视觉展示；需要数值结论时读取结果包中的 DataArtifact（pixelprobe_bundle read）。
10. 图片的存储通道、分析 RGB 通道和视觉 PNG 通道含义不同；P 模式样本是调色板索引，
    必须结合 palette、alpha 和 flags 解读。
"""


def _page(total: int, offset: int, count: int) -> Pagination:
    next_offset = offset + count
    has_more = next_offset < total
    return Pagination(
        total=total, count=count, offset=offset, has_more=has_more,
        next_offset=next_offset if has_more else None,
    )


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _ensure_response_size(response: ToolEnvelope) -> ToolEnvelope:
    size = _json_size(response.model_dump(mode="json"))
    if size > CHARACTER_LIMIT:
        raise McpInputError(
            f"响应约 {size} 字符，超过 {CHARACTER_LIMIT}；请减小 limit、缩小地址范围或分页读取"
        )
    return response


def _paginate(data: dict[str, Any], offset: int, limit: int) -> Pagination | None:
    """对较长的列表字段分页；返回其中最长列表的分页信息。"""
    longest: tuple[int, int] | None = None
    for key in PAGED_LISTS:
        value = data.get(key)
        if isinstance(value, list) and len(value) > limit:
            page = value[offset: offset + limit]
            data[key] = page
            data.setdefault("list_totals", {})[key] = len(value)
            if longest is None or len(value) > longest[0]:
                longest = (len(value), len(page))
    if longest is None:
        return None
    return _page(longest[0], offset, longest[1])


class PixelProbeService:
    """经白名单与资源边界后调用能力函数；不另写分析算法。"""

    def __init__(self, config: ServerConfig, *, cancellation=None) -> None:
        self.cancellation = cancellation
        self.config = config

    # ---------------- 路径与边界 ----------------

    def _resolve_media(self, value: str) -> tuple[Path, FileIdentity]:
        identity = self.config.resolve_file_identity(value)
        return identity.path, identity

    def _verify_media_unchanged(self, identity: FileIdentity) -> None:
        self.config.verify_file_identity(identity)

    @staticmethod
    def _ensure_frame_budget(info: MediaInfo) -> None:
        working_bytes = info.width * info.height * 3 * 4
        if working_bytes > MCP_MAX_FRAME_WORKING_BYTES:
            raise McpResourceLimitError(
                f"原始全帧解码工作集约 {working_bytes} 字节，超过 MCP 限制 "
                f"{MCP_MAX_FRAME_WORKING_BYTES} 字节；rect(...) 不减少完整解码开销"
            )

    @staticmethod
    def _source_frames(address: Address, info: MediaInfo) -> int | None:
        spec = address.time
        if spec is not None and spec.kind == "frame" and spec.mode != "all":
            values = [value for value in spec.values if value is not None]
            if spec.mode == "range" and spec.values[1] is None:
                return info.frame_count
            if values:
                return int(max(values)) + 1
        return info.frame_count

    @staticmethod
    def _ensure_generation_budget(info: MediaInfo, requests: list) -> None:
        """按执行计划估算每个请求需要顺序解码的源帧数；全片选择按总帧数计。"""
        if info.media_type != "video":
            return
        from pixelprobe_mcp.resource_plan import media_plan
        for raw in requests:
            plan = media_plan("generate_representation", info, {"request": raw})
            frames = plan.get("decoded_frames_per_pass_estimate")
            if frames is None or frames > MCP_MAX_GENERATE_SOURCE_FRAMES:
                raise McpResourceLimitError(
                    f"表示生成最多处理 {MCP_MAX_GENERATE_SOURCE_FRAMES} 个源解码帧，当前执行计划需要 "
                    f"{frames if frames is not None else '未知数量'} 帧；请缩小选择范围的末端"
                )

    def _ensure_budget(
        self, item: Capability, info: MediaInfo, address: Address, options: dict, index,
    ) -> None:
        if item.name in SINGLE_FRAME or item.name in SEQUENTIAL:
            self._ensure_frame_budget(info)
        if item.name == "changes":
            from pixelprobe.address import grid_of
            grid = grid_of(address)
            if grid is not None:
                step = grid.step or 1
                count = ((grid.width + step - 1) // step) * ((grid.height + step - 1) // step)
                if count > MCP_MAX_GRID_POINTS:
                    raise McpResourceLimitError(
                        f"网格最多 {MCP_MAX_GRID_POINTS} 个采样点，当前 {count} 个；请缩小 grid 或增大 step"
                    )
        if item.name == "generate":
            self._ensure_frame_budget(info)
            self._ensure_generation_budget(info, options.get("requests") or [])
            return
        if info.media_type != "video" or item.name not in SEQUENTIAL:
            return
        served_by_index = (
            index is not None and item.name in {"changes", "scan"}
            and not address.shapes and (address.step or 1) == 1
        )
        if index is not None and item.name == "find":
            from pixelprobe.find import needs_decode, parse_query
            served_by_index = not needs_decode(parse_query(str(options.get("query", ""))))
        if served_by_index:
            return
        limit = MCP_MAX_SCAN_SOURCE_FRAMES if item.name == "scan" else MCP_MAX_SOURCE_FRAMES
        frames = self._source_frames(address, info)
        if frames is None:
            raise McpResourceLimitError(
                f"{item.name} 需要可用的总帧数才能估算解码量；请用 f=a..b 指定帧范围"
            )
        if frames > limit:
            raise McpResourceLimitError(
                f"{item.name} 最多顺序解码 {limit} 帧，当前地址需要 {frames} 帧；"
                "请用 f=a..b 缩小范围，或先用 pixelprobe_index build 建立索引后做整帧变化检测"
            )

    # ---------------- 能力调用 ----------------

    def capabilities(self) -> ToolEnvelope:
        def describe(item: Capability) -> dict[str, Any]:
            fields = item.options.model_json_schema().get("properties", {})
            return {
                "name": item.name, "summary": item.summary,
                "tool": item.mcp_tool, "selector": item.mcp_selector,
                "cli": f"pixelprobe {item.cli}",
                "at": {
                    "time": sorted(item.address.time_modes),
                    "shapes": sorted(item.address.shapes),
                    "step": item.address.step,
                },
                # 顶层参数：直接写在工具调用里，例如 pixelprobe_bundle(action="generate", requests=[...])
                "arguments": {name: fields[name] for name in item.mcp_arguments},
                # 其余计算参数放在工具的 options 对象里
                "options": {name: value for name, value in fields.items()
                            if name not in item.mcp_arguments},
                "images": list(item.image_names),
                "writes_bundle": item.writes_bundle,
            }

        items = [describe(item) for item in all_capabilities()]
        return ToolEnvelope(data={
            "server": "pixelprobe_mcp",
            "transport": "stdio",
            "pixelprobe_version": __version__,
            "principle": "Agent 原生视觉为主，PixelProbe 确定性数据为辅助",
            "address_syntax": "docs/address.md；与命令行 --at 相同",
            "allowed_roots": [str(root) for root in self.config.allowed_roots],
            "artifact_root": str(self.config.artifact_root),
            "limits": {
                "max_image_payload_bytes": self.config.max_image_bytes,
                "max_text_characters": CHARACTER_LIMIT,
                "max_source_frames": MCP_MAX_SOURCE_FRAMES,
                "max_scan_source_frames": MCP_MAX_SCAN_SOURCE_FRAMES,
                "max_generate_source_frames": MCP_MAX_GENERATE_SOURCE_FRAMES,
                "generate_resources": MCP_GENERATE_RESOURCES.model_dump(mode="json"),
                "max_grid_points": MCP_MAX_GRID_POINTS,
                "max_frame_working_bytes": MCP_MAX_FRAME_WORKING_BYTES,
                "total_memory_bytes": self.config.total_memory_bytes,
                "total_temporary_bytes": self.config.total_temporary_bytes,
                "workers": self.config.workers,
                "queue_size": self.config.queue_size,
            },
            "capabilities": items,
        })

    def run_capability(
        self, params: CapabilityInput,
    ) -> tuple[ToolEnvelope, list[tuple[str, bytes]]]:
        try:
            item = capability(params.capability)
        except KeyError as exc:
            names = "、".join(sorted(c.name for c in all_capabilities()))
            raise McpInputError(f"未知能力；可用：{names}") from exc
        identities: list[FileIdentity] = []
        media: Path
        if not item.needs_media:
            if params.media_path:
                raise McpInputError(f"{item.name} 不接受 media_path")
            media = Path(".")
        elif item.name == "validate":
            if not params.media_path:
                raise McpInputError("validate 需要 media_path 指向结果包目录")
            media = self.config.resolve_directory(params.media_path)
        else:
            if not params.media_path:
                raise McpInputError(f"{item.name} 需要 media_path")
            media, identity = self._resolve_media(params.media_path)
            identities.append(identity)
        reference = None
        if item.name == "compare_media":
            if not params.reference_path:
                raise McpInputError("compare_media 需要 reference_path")
            reference, reference_identity = self._resolve_media(params.reference_path)
            identities.append(reference_identity)
        elif params.reference_path:
            raise McpInputError("只有 compare_media 接受 reference_path")
        output_path = None
        if item.writes_bundle:
            name = params.output_name or f"{item.name}-{uuid.uuid4().hex}.bundle"
            if not name.endswith(".bundle"):
                name += ".bundle"
            output_path = self.config.prepare_artifact_target(name)
        address = parse_address(params.at, params.refs)
        is_media = item.needs_media and item.name != "validate"
        info = core.get_media_info(media) if is_media else None
        index = None
        if info is not None and info.media_type == "video" and not params.no_index:
            index = media_sessions().index_for(media)
        if info is not None:
            self._ensure_budget(item, info, address, params.options, index)
        settings: dict[str, Any] = {}
        if item.name == "generate":
            # 内存上限跟随该任务分到的额度；时限、临时空间与分块仍按 MCP 固定边界
            from pixelprobe.utils.resource_budget import ambient_memory_budget
            settings["resources"] = MCP_GENERATE_RESOURCES.model_copy(update={
                "max_memory_bytes": max(MCP_GENERATE_RESOURCES.max_memory_bytes,
                                        ambient_memory_budget()),
            })
        outcome = item.invoke(
            media, address, params.options,
            index=index, reference=reference, output_path=output_path,
            cancellation=self.cancellation,
            settings={**settings, **({"thumb_budget": MCP_INDEX_THUMB_BUDGET_BYTES}
                                     if item.name == "index_build" else {})},
        )
        for identity in identities:
            self._verify_media_unchanged(identity)
        data = dict(outcome.data)
        warnings = list(outcome.warnings)
        if item.name in SEMANTIC_WARNINGS:
            warnings.insert(0, SEMANTIC_WARNINGS[item.name])
        if (item.name in {"frame", "pixel", "region"} and info is not None
              and info.media_type == "image" and params.options.get("sample") != "native"):
            self._warn_display_conversion(info, warnings)
        images: list[tuple[str, bytes]] = []
        if params.images and outcome.images:
            data["images"] = []
            for name, factory in outcome.images.items():
                rendered = self._render_image(item.name, name, factory(), data, outcome, params)
                images.append((name, rendered.png))
                data["images"].append({"name": name, "width": rendered.width,
                                       "height": rendered.height, **rendered.meta})
        if outcome.refs:
            data["refs"] = dict(outcome.refs)
        if item.writes_bundle and "bundle" in outcome.artifacts:
            data["bundle_path"] = str(outcome.artifacts["bundle"].root)
        pagination = _paginate(data, params.offset, params.limit)
        response = ToolEnvelope(
            data=data, warnings=warnings, pagination=pagination,
            next_actions=self._next_actions(item, media, outcome),
        )
        return _ensure_response_size(response), images

    @staticmethod
    def _warn_display_conversion(info: MediaInfo, warnings: list[str]) -> None:
        """透明或调色板图片的显示 RGB8 不等于存储样本，需明确告知（只看已读出的模式，不重新打开图片）。"""
        mode = info.color_mode or ""
        has_alpha = "A" in mode
        if has_alpha or mode == "P":
            warnings.append(
                f"数值与预览来自显示 RGB8 转换（存储模式 {mode}，"
                f"{'含 Alpha' if has_alpha else '调色板索引'}）；预览不保留透明度。"
                "需要存储样本请用 pixelprobe_measure what=pixel options.sample=native"
            )

    def _render_image(self, cap: str, name: str, array: np.ndarray, data: dict,
                      outcome: Outcome, params: CapabilityInput):
        x_axis = y_axis = None
        grid = False
        title = f"{cap}:{name}"
        height, width = array.shape[:2]
        if cap in {"frame", "region", "reduce", "compare"} or name in {"flow", "magnitude"}:
            # 刻度标原始存储坐标：裁剪/区域图从其左上角开始计
            x0, y0 = 0, 0
            if cap == "region":
                x0, y0 = outcome.artifacts["rect"][:2]
            else:
                rect = data.get("crop") if cap == "frame" else data.get("rect")
                if isinstance(rect, dict):
                    x0, y0 = rect["x"], rect["y"]
            x_axis = Axis("x", width, offset=x0)
            y_axis = Axis("y", height, offset=y0)
            # cell 网格按整幅画面划分，只在未裁剪的整幅图上叠加
            grid = params.grid and (x0, y0) == (0, 0) and cap in {"frame", "reduce", "compare"}
            if data.get("frame") is not None:
                title = f"{cap} f={data['frame']}"
        elif cap in {"xt", "yt"}:
            x_axis = Axis("x" if cap == "xt" else "y", width)
            y_axis = frame_axis(data.get("frames", list(range(height))))
            title = f"{cap} {'row' if cap == 'xt' else 'col'}={data.get('fixed_coordinate')}"
        elif cap == "track" and name == "follow":
            # 横轴：相对跟踪点的水平偏移；纵轴：帧号
            x_axis = Axis("dx", width, offset=-(width // 2))
            y_axis = frame_axis(data.get("frames", list(range(height))))
            title = "track follow"
        elif cap == "track":
            x_axis, y_axis = Axis("x", width), Axis("y", height)
            title = f"track path f={data.get('start_frame')}..{data.get('end_frame')}"
        elif cap == "timeline":
            x_axis = frame_axis(data.get("frames", list(range(width))))
            y_axis = Axis("point", height, label=lambda i: f"P{i + 1}")
        return annotate(
            array, x_axis=x_axis, y_axis=y_axis, grid=grid, title=title,
            max_side=params.max_side, max_bytes=self.config.max_image_bytes,
        )

    @staticmethod
    def _next_actions(item: Capability, media: Path, outcome: Outcome) -> list[NextAction]:
        actions: list[NextAction] = []
        if "match1" in outcome.refs:
            actions.append(NextAction(
                tool="pixelprobe_look",
                reason="搜索结果只是数值候选；用 @match1 查看第一个区间的画面",
                arguments={"media_path": str(media), "view": "sheet", "at": "@match1"},
            ))
        if "event1" in outcome.refs:
            actions.append(NextAction(
                tool="pixelprobe_look",
                reason="变化分数没有语义；用 @event1 查看该事件前后的网格画面",
                arguments={"media_path": str(media), "view": "sheet", "at": "@event1"},
            ))
        if item.writes_bundle:
            bundle = outcome.artifacts.get("bundle")
            if bundle is not None:
                actions.append(NextAction(
                    tool="pixelprobe_bundle",
                    reason="分页列出结果包中的正式数值与预览",
                    arguments={"action": "list", "bundle_path": str(bundle.root)},
                ))
        return actions

    # ---------------- 结果包读取 ----------------

    def list_artifacts(self, params: BundleListInput) -> ToolEnvelope:
        root = self.config.resolve_directory(params.bundle_path)
        bundle = BundleReader().open(root, verify=params.verify)
        records = [
            record for record in bundle.manifest.artifacts
            if params.kind is None or record.kind == params.kind
        ]
        page_records = records[params.offset : params.offset + params.limit]
        return _ensure_response_size(ToolEnvelope(
            data={
                "bundle_path": str(root), "bundle_id": bundle.manifest.bundle_id,
                "schema_version": bundle.manifest.schema_version,
                "verify": params.verify,
                "notices": list(bundle.notices),
                "artifacts": [record.model_dump(mode="json") for record in page_records],
            },
            pagination=_page(len(records), params.offset, len(page_records)),
        ))

    def read_artifact(self, params: ArtifactReadInput) -> ToolEnvelope:
        root = self.config.resolve_directory(params.bundle_path)
        bundle = BundleReader().open(root, verify=params.verify)
        tensor = bundle.open_tensor(params.artifact_id)
        try:
            data: dict[str, Any] = {
                "bundle_path": str(root), "artifact_id": params.artifact_id,
                "verify": params.verify,
                "tensor_id": tensor.tensor_id, "shape": list(tensor.data.shape),
                "dtype": tensor.data.dtype,
                "axes": [axis.model_dump(mode="json") for axis in tensor.axes],
                "channels": [channel.model_dump(mode="json") for channel in tensor.channels],
                "coordinate_space": (
                    tensor.coordinate_space.model_dump(mode="json")
                    if tensor.coordinate_space else None
                ),
                "accuracy": tensor.accuracy.model_dump(mode="json"),
                "selection": None, "selected_shape": None, "values": None,
            }
            actions: list[NextAction] = []
            if params.selection is None:
                actions.append(NextAction(
                    tool="pixelprobe_bundle",
                    reason="按 axes 顺序提供 selection 后读取有限数量正式数值",
                    arguments={
                        "action": "read",
                        "bundle_path": str(root), "artifact_id": params.artifact_id,
                        "selection": [
                            {"start": 0, "stop": min(axis.length, 1)} for axis in tensor.axes
                        ],
                    },
                ))
            else:
                if len(params.selection) != len(tensor.data.shape):
                    raise McpInputError(
                        f"selection 需要 {len(tensor.data.shape)} 个轴，实际 {len(params.selection)}"
                    )
                selection: list[slice | int] = []
                count = 1
                for axis, (item, length) in enumerate(zip(params.selection, tensor.data.shape)):
                    if item.index is not None:
                        selection.append(item.index)
                        continue
                    start = 0 if item.start is None else item.start
                    stop = length if item.stop is None else item.stop
                    if start > stop or stop > length:
                        raise McpInputError(f"第 {axis} 轴切片必须满足 0 <= start <= stop <= {length}")
                    selection.append(slice(start, stop, item.step))
                    count *= len(range(start, stop, item.step))
                if count > params.max_values:
                    raise McpInputError(
                        f"选择包含 {count} 个标量，超过 max_values={params.max_values}；请缩小切片"
                    )
                array = tensor.data.read(tuple(selection))
                data["selection"] = [item.model_dump(mode="json") for item in params.selection]
                data["selected_shape"] = list(array.shape)
                data["value_count"] = int(array.size)
                if np.iscomplexobj(array):
                    data["values"] = {"real": array.real.tolist(), "imag": array.imag.tolist()}
                else:
                    data["values"] = array.tolist()
            warnings = []
            if params.verify == "metadata":
                warnings.append(
                    "当前为 metadata 验证：已检查结构和文件大小，未重新计算全部 SHA-256；"
                    "需要完整内容完整性校验时请以 verify='full' 重试"
                )
            return _ensure_response_size(ToolEnvelope(data=data, warnings=warnings, next_actions=actions))
        finally:
            close = getattr(tensor.data, "close", None)
            if callable(close):
                close()


def validation_message(exc: ValidationError) -> str:
    """把 Pydantic 错误压缩为 Agent 可直接修正的消息。"""
    items = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(item) for item in error["loc"]) or "请求"
        items.append(f"{location}: {error['msg']}")
    return "；".join(items)
