"""能力登记表：每项分析功能只定义一次，命令行与 MCP 都从这里调用。

一项能力（Capability）声明：
- 名称与一句话说明；
- 接受的地址写法（时间、形状、step），与命令行 --at、MCP 的 at 参数共用同一规则；
- 计算参数模型（pydantic，只含影响计算的参数；输出路径、放大倍数等展示参数由各入口自行处理）；
- 执行函数：输入 Invocation，返回 Outcome（结构化数据 + 按需生成的图片 + 可引用的结果编号）；
- 命令行入口与 MCP 入口（工具名 + 选择器），供能力对等测试核对。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from pixelprobe.address import Address, check
from pixelprobe.models.errors import InvalidRangeError

ImageFactory = Callable[[], Any]


class NoOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class AddressRule:
    time_modes: frozenset[str] = frozenset()
    shapes: frozenset[str] = frozenset()
    step: bool = False

    def check(self, address: Address, command: str) -> None:
        check(
            address, command=command, time_modes=self.time_modes,
            shapes=self.shapes, step=self.step,
        )


@dataclass
class Invocation:
    """一次调用的输入。路径已由入口完成权限检查（MCP 白名单 / 命令行用户指定）。"""

    media: Path
    address: Address
    options: BaseModel
    index: Any = None
    reference: Path | None = None
    output_path: Path | None = None
    progress: Callable[[int, int], None] | None = None
    cancellation: Any = None
    # 只能由本机入口（命令行）提供的设置，如索引目录、缓存目录；MCP 不传，避免越过路径白名单
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class Outcome:
    """一次调用的结果。

    data：可直接序列化的结构化结果（不含任何输出文件路径）。
    images：名称 → 无参函数，调用时才生成 uint8 图片数组，避免为不需要的图片付出代价。
    artifacts：供入口写文件使用的原始对象（如变化记录、时间线结果、Bundle）。
    refs：结果编号 → 地址文字，可写入引用表供后续 @名称 使用。
    """

    data: dict[str, Any]
    images: dict[str, ImageFactory] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Capability:
    name: str
    summary: str
    run: Callable[[Invocation], Outcome]
    cli: str
    mcp_tool: str
    mcp_selector: str | None = None
    address: AddressRule = AddressRule()
    options: type[BaseModel] = NoOptions
    needs_media: bool = True
    image_names: tuple[str, ...] = ()
    writes_bundle: bool = False

    def invoke(
        self,
        media: Path,
        address: Address,
        options: Mapping[str, Any] | BaseModel | None = None,
        **context: Any,
    ) -> Outcome:
        """校验地址与参数后执行；命令行和 MCP 都经过这里。"""
        self.address.check(address, self.cli)
        if not isinstance(options, BaseModel):
            try:
                options = self.options.model_validate(dict(options or {}))
            except ValidationError as exc:
                details = "；".join(
                    f"{'.'.join(str(part) for part in error['loc']) or '参数'}: {error['msg']}"
                    for error in exc.errors(include_url=False)
                )
                raise InvalidRangeError(f"{self.name} 的参数无效：{details}") from exc
        return self.run(Invocation(media=media, address=address, options=options, **context))


_REGISTRY: dict[str, Capability] = {}


def register(capability: Capability) -> Capability:
    if capability.name in _REGISTRY:
        raise ValueError(f"能力 {capability.name} 重复登记")
    _REGISTRY[capability.name] = capability
    return capability


def capability(name: str) -> Capability:
    _load()
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"未登记的能力：{name}") from exc


def all_capabilities() -> tuple[Capability, ...]:
    _load()
    return tuple(_REGISTRY.values())


def _load() -> None:
    # 各能力模块在导入时登记自身
    from pixelprobe.capabilities import media, temporal, bundles  # noqa: F401
