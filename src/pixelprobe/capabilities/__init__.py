"""能力登记表：命令行与 MCP 共用的分析功能定义。详见 base 模块说明。"""

from pixelprobe.capabilities.base import (
    AddressRule,
    Capability,
    Invocation,
    NoOptions,
    Outcome,
    all_capabilities,
    capability,
)

__all__ = [
    "AddressRule", "Capability", "Invocation", "NoOptions", "Outcome",
    "all_capabilities", "capability",
]
