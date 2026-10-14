"""CLI 层公共设施：全局参数、错误处理与退出码映射。

CLI 层只做参数解析和输出编排，不实现像素算法。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import click
import typer

from pixelprobe.address import Address, needs_media_size, parse_address
from pixelprobe.domain.errors import DomainError
from pixelprobe.models.errors import AddressError, PixelProbeError
from pixelprobe.output import json_writer
from pixelprobe.output.console import err_console

# 各命令共用的全局参数定义
JSON_OPT = typer.Option(False, "--json", help="stdout 输出标准 JSON")
QUIET_OPT = typer.Option(False, "--quiet", help="只输出必要结果")
VERBOSE_OPT = typer.Option(False, "--verbose", help="显示调试信息")
NO_PROGRESS_OPT = typer.Option(False, "--no-progress", help="关闭进度条")
AT_OPT = typer.Option(
    None, "--at",
    help='分析位置（时间+空间），如 "f=120 rect(400,200,200,150)"、"t=1s..3s step=2"、'
         '"cell(C4)"；完整写法见 docs/address.md',
)
REF_FILE_OPT = typer.Option(
    None, "--ref-file", help="引用表 JSON，供 --at 中的 @名称 使用",
)
NO_INDEX_OPT = typer.Option(
    False, "--no-index", help="不使用已建立的媒体索引，直接解码",
)
INDEX_DIR_OPT = typer.Option(
    None, "--index-dir", help="媒体索引目录（缺省读环境变量 PIXELPROBE_INDEX_DIR 或本机数据目录）",
)


def find_index(media: Path, *, no_index: bool, index_dir: Path | None):
    """取与媒体当前内容匹配的索引；未建立、已过期或被禁用时返回 None。"""
    if no_index:
        return None
    from pixelprobe.core.media_reader import detect_media_type
    from pixelprobe.index import IndexStore
    if detect_media_type(Path(media)) != "video":
        return None
    return IndexStore(index_dir).lookup(media)


def load_refs(ref_file: Path | None) -> dict[str, str]:
    """读取引用表：{"refs": {...}}、CLI 输出信封 {"data": {"refs": {...}}} 或扁平映射。"""
    if ref_file is None:
        return {}
    try:
        payload = json.loads(Path(ref_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AddressError(f"无法读取引用表 {ref_file}：{exc}") from exc
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if isinstance(payload, dict) and isinstance(payload.get("refs"), dict):
        payload = payload["refs"]
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
    ):
        raise AddressError(f"引用表 {ref_file} 应为 名称 → 地址文字 的 JSON 对象")
    return payload


def read_address(at: str | None, ref_file: Path | None = None) -> Address:
    return parse_address(at, load_refs(ref_file))


def media_size(media: Path, address: Address) -> tuple[int, int]:
    """cell(...)/pid(...) 需要画面尺寸；其余情况不打开媒体，返回 (0, 0)。"""
    if not needs_media_size(address):
        return 0, 0
    from pixelprobe import core
    info = core.get_media_info(media)
    return info.width, info.height


def usage_error_details(error: BaseException) -> tuple[str, int] | None:
    """兼容 Click 及 Typer 内置 Click 的参数错误。"""
    is_click_usage_error = isinstance(error, click.UsageError)
    is_typer_usage_error = any(
        base.__name__ == "UsageError" and base.__module__.startswith("typer.")
        for base in type(error).__mro__
    )
    if not is_click_usage_error and not is_typer_usage_error:
        return None
    formatter = getattr(error, "format_message", None)
    exit_code = getattr(error, "exit_code", None)
    if not callable(formatter) or not isinstance(exit_code, int):
        return None
    return str(formatter()), exit_code


@dataclass
class CliContext:
    """单次命令调用的输出模式。"""

    json_mode: bool = False
    quiet: bool = False
    verbose: bool = False
    no_progress: bool = False

    @property
    def progress_disabled(self) -> bool:
        return self.no_progress or self.quiet or self.json_mode


@contextmanager
def cli_guard(command: str, ctx: CliContext) -> Iterator[None]:
    """统一异常处理：业务错误映射退出码，Ctrl+C 安全退出。"""
    try:
        yield
    except PixelProbeError as exc:
        if ctx.json_mode:
            json_writer.print_error(command, exc.to_dict())
        else:
            err_console.print(f"错误：{exc.message}", style="red", highlight=False)
            if exc.hint:
                err_console.print(f"提示：{exc.hint}", style="yellow", highlight=False)
        raise typer.Exit(exc.exit_code) from exc
    except DomainError as exc:
        if ctx.json_mode:
            json_writer.print_error(command, exc.to_dict())
        else:
            err_console.print(f"错误：{exc.message}", style="red", highlight=False)
            if exc.hint:
                err_console.print(
                    f"提示：{exc.hint}", style="yellow", highlight=False
                )
        raise typer.Exit(2) from exc
    except KeyboardInterrupt as exc:
        if ctx.json_mode:
            json_writer.print_error(command, {
                "code": "CANCELLED",
                "message": "操作已取消，临时文件已清理",
            })
        else:
            err_console.print("已取消，临时文件已清理", style="yellow")
        raise typer.Exit(130) from exc
    except typer.Exit:
        raise
    except Exception as exc:  # 未预期错误 → 一般运行错误（退出码 1）
        usage_error = usage_error_details(exc)
        if usage_error is not None:
            message, exit_code = usage_error
            if ctx.json_mode:
                json_writer.print_error(command, {
                    "code": "INVALID_ARGUMENT", "message": message,
                })
            else:
                err_console.print(
                    f"参数错误：{message}", style="red", highlight=False,
                )
            raise typer.Exit(exit_code) from exc
        if ctx.verbose:
            err_console.print_exception()
        if ctx.json_mode:
            json_writer.print_error(
                command,
                {"code": "RUNTIME_ERROR", "message": str(exc)},
            )
        else:
            err_console.print(f"运行错误：{exc}", style="red", highlight=False)
        raise typer.Exit(1) from exc
