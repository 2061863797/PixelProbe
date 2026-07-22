"""时间单位换算工具。CLI 统一使用秒，JSON 同时返回秒和毫秒。"""

from __future__ import annotations


def seconds_to_ms(seconds: float) -> float:
    """秒转毫秒，保留 3 位小数。"""
    return round(seconds * 1000.0, 3)


def round_seconds(seconds: float) -> float:
    """时间秒值统一保留 6 位小数，保证 JSON 输出稳定。"""
    return round(seconds, 6)
