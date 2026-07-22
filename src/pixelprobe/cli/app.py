"""Typer 主入口：注册全部子命令。"""

from __future__ import annotations

import typer

from pixelprobe.cli import (
    changes_cmd,
    compare_cmd,
    flow_cmd,
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
)
from pixelprobe.output.console import err_console, out_console
from pixelprobe.version import __version__

app = typer.Typer(
    name="pixelprobe",
    help=(
        "PixelProbe：面向人类和 AI Agent 的图片、视频像素分析工具。\n\n"
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
    try:
        app()
    except KeyboardInterrupt:
        # 兜底：命令内部未捕获的 Ctrl+C（如参数解析阶段）
        err_console.print("已取消", style="yellow")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
