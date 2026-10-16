"""搜索类能力：find 搜索语言。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from pixelprobe.capabilities.base import AddressRule, Capability, Invocation, Outcome, register
from pixelprobe.core.media_reader import detect_media_type
from pixelprobe.models.errors import UnsupportedMediaError


class FindOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000, description=(
        '搜索语句，条件用 | 连接取并集，如 "black | flash"、"freeze >= 0.5s"、"similar f=120"、'
        '"frame.luma > 200"、"rect(0,0,50,50).r < 30"、"first change in cell(B2)"'
    ))
    jump_threshold: float = Field(
        default=0.1, gt=0, le=1,
        description="cut/flash 的跳变阈值：与上一帧的平均绝对差 / 255 不低于此值（0～1）",
    )
    freeze_tolerance: float = Field(
        default=0.1, ge=0, le=255, description="freeze：整帧平均绝对差不超过此值视为停住（0～255）",
    )
    animation_tolerance: float = Field(
        default=1.0, ge=0, le=255, description="animation：区域平均绝对差超过此值算在动（0～255）",
    )
    settle_seconds: float = Field(
        default=0.2, gt=0, le=60, description="animation：静止持续多少秒算停稳",
    )
    limit: int = Field(default=200, ge=1, le=2000, description="最多返回的结果区间数")


def _find(call: Invocation) -> Outcome:
    from pixelprobe.find import DEFINITIONS, find, parse_query
    options: FindOptions = call.options  # type: ignore[assignment]
    terms = parse_query(options.query)
    if detect_media_type(call.media) != "video":
        raise UnsupportedMediaError("find 只适用于视频", hint="图片请用 pixel / region 读取数值")
    cancel = call.cancellation.raise_if_cancelled if call.cancellation is not None else None
    result = find(
        call.media, terms, call.address, index=call.index,
        jump_threshold=options.jump_threshold, freeze_tolerance=options.freeze_tolerance,
        animation_tolerance=options.animation_tolerance, settle_seconds=options.settle_seconds,
        limit=options.limit,
        progress=call.progress, cancel_check=cancel,
    )
    # 结果都是帧区间，编号统一写成范围（单帧为 f=n..n），可直接用于 sheet/changes 等接受范围的能力
    refs = {
        f"match{number}": f"f={match['start_frame']}..{match['end_frame']}"
        for number, match in enumerate(result.matches, start=1)
    }
    used = {kind for kind in result.kinds}
    return Outcome(
        data={
            "query": options.query,
            "terms": [str(term) for term in terms],
            "start_frame": result.frame_range.start,
            "end_frame": result.frame_range.end,
            "frames_analyzed": result.frames_analyzed,
            "match_count": len(result.matches),
            "truncated": result.truncated,
            "matches": result.matches,
            "definitions": {kind: text for kind, text in DEFINITIONS.items() if kind in used},
            "computed_from": result.computed_from,
        },
        refs=refs,
        warnings=["结果按固定数值定义判定，只是候选；请查看对应时刻的画面再下结论"],
    )


class StreamsOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame_types: bool = Field(
        default=False, description="再顺序解码一遍统计 I/P/B 帧类型（较慢）；缺省只读压缩包结构",
    )


def _streams(call: Invocation) -> Outcome:
    from pixelprobe.streams import analyze_streams
    options: StreamsOptions = call.options  # type: ignore[assignment]
    if detect_media_type(call.media) != "video":
        raise UnsupportedMediaError("streams 只适用于视频", hint="图片取证请用 evidence")
    cancel = call.cancellation.raise_if_cancelled if call.cancellation is not None else None
    report = analyze_streams(
        call.media, frame_types=options.frame_types, progress=call.progress, cancel_check=cancel,
    )
    return Outcome(
        data=report,
        warnings=["码流结构只提供可复核线索与局限，不判定是否重新编码、剪辑或伪造"],
    )


STREAMS = register(Capability(
    name="streams", summary="视频容器与编码结构：标签、流参数、关键帧间隔、时间戳问题、每秒码率与取证线索",
    run=_streams, cli="streams", mcp_tool="pixelprobe_evidence", mcp_selector="streams",
    options=StreamsOptions,
))


FIND = register(Capability(
    name="find", summary="按搜索语句定位时刻与区间（黑帧、闪帧、切换、停住、掉帧、相似帧、指标条件、区域变化）",
    run=_find, cli="find", mcp_tool="pixelprobe_find", mcp_selector="query",
    address=AddressRule(frozenset({"all", "range"})),
    options=FindOptions,
))
