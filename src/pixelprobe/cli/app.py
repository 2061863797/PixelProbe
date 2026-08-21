"""Typer 主入口：注册全部子命令。"""

from __future__ import annotations

import sys

import click
import typer

from pixelprobe.cli import (
    cache_cmd,
    changes_cmd,
    compare_cmd,
    flow_cmd,
    generate_cmd,
    frame_cmd,
    info_cmd,
    pixel_cmd,
    reduce_cmd,
    region_cmd,
    scan_cmd,
    sheet_cmd,
    spacetime_cmd,
    spectrum_cmd,
    timeline_cmd,
    usage_error_details,
    validate_cmd,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import err_console, out_console
from pixelprobe.version import __version__

app = typer.Typer(
    name="pixelprobe",
    help=(
        "PixelProbe：本地图片与视频像素分析 CLI 工具。\n\n"
        "坐标原点在左上角（x 向右，y 向下）；帧号从 0 开始；"
        "帧范围为闭区间（--start-frame 10 --end-frame 20 共 11 帧）；"
        "时间单位为秒。"
    ),
    add_completion=False,
    no_args_is_help=True,
)

app.command("info")(info_cmd.info)
app.command("frame")(frame_cmd.frame)
app.command("pixel")(pixel_cmd.pixel)
app.command("region")(region_cmd.region)
app.command("timeline")(timeline_cmd.timeline)
app.command("xt")(spacetime_cmd.xt)
app.command("yt")(spacetime_cmd.yt)
app.command("changes")(changes_cmd.changes)
app.command("reduce")(reduce_cmd.reduce)
app.command("compare")(compare_cmd.compare)
app.command("sheet")(sheet_cmd.sheet)
app.command("scan")(scan_cmd.scan)
app.command("spectrum")(spectrum_cmd.spectrum)
app.command("spectrum2d")(spectrum_cmd.spectrum2d)
app.command("flow")(flow_cmd.flow)
app.command("generate")(generate_cmd.generate)
app.command("validate")(validate_cmd.validate)
app.add_typer(cache_cmd.app, name="cache")


def _version_callback(value: bool) -> None:
    if value:
        out_console.print(f"pixelprobe {__version__}", highlight=False)
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="显示版本号并退出",
    ),
) -> None:
    """PixelProbe CLI。"""


def main() -> None:
    """console_scripts 入口。"""
    json_mode_requested = "--json" in sys.argv[1:]
    try:
        # 仅 JSON 模式让解析错误回到这里；普通终端保留 Typer 原有呈现。
        result = app(standalone_mode=not json_mode_requested)
        # Click 在 standalone_mode=False 时会把 typer.Exit 转换为返回的
        # 退出码，而不是抛出异常。必须显式转回 SystemExit，否则 JSON 失败
        # 响应会错误地以 0 退出。
        if json_mode_requested and isinstance(result, int) and result != 0:
            raise SystemExit(result)
    except click.UsageError as exc:
        if json_mode_requested:
            command = next(
                (argument for argument in sys.argv[1:] if not argument.startswith("-")),
                "pixelprobe",
            )
            json_writer.print_error(command, {
                "code": "INVALID_ARGUMENT",
                "message": exc.format_message(),
            })
        else:
            exc.show()
        raise SystemExit(exc.exit_code) from exc
    except (click.Abort, typer.Abort) as exc:
        if json_mode_requested:
            json_writer.print_error("pixelprobe", {
                "code": "CANCELLED", "message": "操作已取消",
            })
        else:
            err_console.print("已取消", style="yellow")
        raise SystemExit(130) from exc
    except KeyboardInterrupt:
        # 兜底：命令内部未捕获的 Ctrl+C（如参数解析阶段）
        if json_mode_requested:
            json_writer.print_error("pixelprobe", {
                "code": "CANCELLED", "message": "操作已取消",
            })
        else:
            err_console.print("已取消", style="yellow")
        raise SystemExit(130) from None
    except typer.Exit as exc:
        raise SystemExit(exc.exit_code) from exc
    except Exception as exc:
        # Typer 0.27 起可使用其内置 Click，异常不再是外部
        # click.UsageError 的实例；按公共错误行为识别后维持稳定协议。
        usage_error = usage_error_details(exc)
        if usage_error is None:
            raise
        message, exit_code = usage_error
        if json_mode_requested:
            command = next(
                (argument for argument in sys.argv[1:] if not argument.startswith("-")),
                "pixelprobe",
            )
            json_writer.print_error(command, {
                "code": "INVALID_ARGUMENT", "message": message,
            })
        else:
            show = getattr(exc, "show", None)
            if callable(show):
                show()
        raise SystemExit(exit_code) from exc


if __name__ == "__main__":
    main()
