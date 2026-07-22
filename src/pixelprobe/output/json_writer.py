"""JSON 输出。

规则：--json 时 stdout 只输出合法 JSON；
成功含 "success": true，失败含 "success": false 和错误对象。
"""

from __future__ import annotations

import json
import sys


def dump_success(command: str, data: dict) -> str:
    """构造成功输出的 JSON 字符串。"""
    return json.dumps(
        {"success": True, "command": command, "data": data},
        ensure_ascii=False,
    )


def dump_error(command: str, error: dict) -> str:
    """构造失败输出的 JSON 字符串。"""
    return json.dumps(
        {"success": False, "command": command, "error": error},
        ensure_ascii=False,
    )


def print_success(command: str, data: dict) -> None:
    """把成功结果写到 stdout（唯一允许写 stdout 的 JSON 出口）。"""
    sys.stdout.write(dump_success(command, data) + "\n")


def print_error(command: str, error: dict) -> None:
    """把失败结果写到 stdout（JSON 模式下错误对象也走 stdout）。"""
    sys.stdout.write(dump_error(command, error) + "\n")
