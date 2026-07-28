"""CLI 层公共设施：全局参数、错误处理与退出码映射。

CLI 层只做参数解析和输出编排，不实现像素算法。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import typer

from pixelprobe.domain.errors import DomainError
from pixelprobe.models.errors import PixelProbeError
from pixelprobe.output import json_writer
from pixelprobe.output.console import err_console

# 各命令共用的全局参数定义
JSON_OPT = typer.Option(False, "--json", help="stdout 输出标准 JSON")
QUIET_OPT = typer.Option(False, "--quiet", help="只输出必要结果")
VERBOSE_OPT = typer.Option(False, "--verbose", help="显示调试信息")
NO_PROGRESS_OPT = typer.Option(False, "--no-progress", help="关闭进度条")


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
