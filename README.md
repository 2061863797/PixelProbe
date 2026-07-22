# PixelProbe

**让 AI 看懂画面，也让分析结果精确到帧、时间和像素。**

PixelProbe 是一款本地图片与视频分析工具。它可以快速定位视频中的关键变化、
提取指定画面、读取像素颜色、统计区域特征，并把长视频转换成便于观察的时间线
与时空切片。

它既可以作为可视化软件直接使用，也可以通过命令行完成批量分析，还可以作为
MCP 工具交给 AI 调用。媒体文件始终在本机处理。

![PixelProbe 本地可视化界面](docs/screenshot-gui.png)

## 主要功能

- **媒体信息**：查看尺寸、帧率、帧数、时长、编码和可变帧率信息。
- **精确取帧**：按帧号或时间提取画面，支持局部裁剪和预览缩放。
- **像素探测**：读取一个或多个像素的 RGB、HEX、HSV、Lab 和亮度。
- **区域分析**：统计选区的平均值、中位数、最值、标准差和颜色特征。
- **变化检测**：扫描点、区域或网格，找出变化最明显的帧和时间。
- **颜色时间线**：观察固定像素或网格区域在整段视频中的颜色变化。
- **X–T / Y–T 切片**：把空间与时间放在同一张图中，直观看到移动、闪烁和镜头变化。
- **AI 工具调用**：让支持 MCP 的 AI 自动选择分析步骤、查看关键帧并引用精确数据。

## 三种使用方式

| 方式 | 适合场景 | 启动命令 |
| --- | --- | --- |
| 可视化界面 | 手动浏览、点击像素、拖框分析、查看变化图表 | `pixelprobe-web` |
| 命令行 | 批量处理、脚本调用、导出 PNG/CSV/JSON | `pixelprobe` |
| MCP | 让 AI 自主分析本地图片和视频 | `pixelprobe-mcp` |

## AI 快速接入 MCP

最省事的方式是让 AI 客户端通过 `uvx` 直接从 GitHub 获取并运行 PixelProbe，
不需要先单独安装 PixelProbe。电脑上需要有
[uv](https://docs.astral.sh/uv/getting-started/installation/)。

Codex 用户只需执行：

```bash
codex mcp add pixelprobe -- uvx --from git+https://github.com/2061863797/PixelProbe.git pixelprobe-mcp
```

其他支持本地 stdio MCP 的 AI 客户端，可以复制下面的配置：

```json
{
  "mcpServers": {
    "pixelprobe": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/2061863797/PixelProbe.git",
        "pixelprobe-mcp"
      ]
    }
  }
}
```

首次调用时会从 GitHub 下载 PixelProbe 及其运行依赖，之后会使用本机缓存。
注意：缓存不会自动跟随仓库更新，PixelProbe 发布新版本后请执行
`uv cache clean pixelprobe` 后重启 AI 客户端（或用 `uvx --refresh` 方式启动）。
接入完成后，可以直接把本地媒体路径和问题交给 AI，例如：

> 分析 `D:\videos\test.mp4`，找出画面右上角第一次发生明显变化的准确帧号和时间。

AI 可以读取指定的本地图片和视频。只有名称含 `save` 的五个保存工具会写入文件，
并且必须由 AI 明确提供输出路径；其他工具只返回分析结果和预览图。
连接 MCP 时，PixelProbe 会自动向 AI 注入协作原则：AI 原有的视觉和视频理解
负责看懂画面，PixelProbe 负责辅助定位并提供精确数据。

## 安装软件

需要 Python 3.11 或更高版本。优先支持 Windows 10/11，同时兼容 Linux 和 macOS。
从 GitHub 安装完整软件：

```bash
python -m pip install "git+https://github.com/2061863797/PixelProbe.git"
```

安装后会同时提供可视化界面、命令行和 MCP Server。可以先检查版本：

```bash
pixelprobe --version
```

如果软件已经安装，AI 客户端的 MCP 配置可以直接使用
`{"command": "pixelprobe-mcp"}`，无需再通过 `uvx` 下载。
`pixelprobe-mcp` 是由 AI 客户端启动的后台协议进程；直接在终端运行时会等待
AI 连接，不会出现交互界面。

## 可视化界面

运行：

```bash
pixelprobe-web
```

浏览器会自动打开 `http://127.0.0.1:8799/`。输入本地图片或视频路径后，可以：

- 播放视频、拖动时间轴、使用方向键逐帧查看；
- 单击画面读取像素颜色；
- 拖框选择区域并立即查看统计结果；
- 对选区执行变化检测，点击变化峰值跳转到对应帧，并查看自动分段的事件区间；
- 查看颜色时间线和 X–T / Y–T 时空切片；
- 时间域合成（含去条纹/平滑增强）、两帧差异比较和两帧光流分析；
- 调整采样步长，在速度和精度之间切换。

可变帧率视频会使用真实逐帧时间进行定位。浏览器无法直接播放某种编码时，
界面会自动切换到后端单帧预览。

Web 服务只允许本机访问，不会监听 `0.0.0.0` 等外部地址。

注意：`pixelprobe-web` 和 `pixelprobe-mcp` 都是长驻进程，升级 PixelProbe
之后必须重启它们（MCP 由 AI 客户端管理，重启客户端或重连 MCP 即可），
否则会继续运行旧版本代码，出现页面与接口不匹配等问题。

## 命令行

常用示例：

```bash
# 查看视频信息
pixelprobe info input.mp4

# 提取第 120 帧
pixelprobe frame input.mp4 --frame 120 --output frame120.png

# 提取 3.5 秒处的画面并裁剪
pixelprobe frame input.mp4 --time 3.5 --crop 400,200,300,300 --output crop.png

# 查询两个像素
pixelprobe pixel input.mp4 --frame 120 --point 520,340 --point 600,400

# 分析矩形区域
pixelprobe region input.mp4 --frame 120 --rect 400,200,200,150

# 导出像素颜色时间线
pixelprobe timeline input.mp4 --point 520,340 --output timeline.png --csv timeline.csv

# 找出选区变化最大的 10 帧（不给目标时默认整帧，并自动分段为事件）
pixelprobe changes input.mp4 --rect 400,200,200,150 --top 10

# 一键概览：信息 + 代表帧网格 + 变化事件 + 异常帧（单遍解码）
pixelprobe scan input.mp4 --sheet-output 概览.png

# 时间域合成：噪声中的隐藏图案 / 运动能量分布
pixelprobe reduce input.mp4 --op std --output 统计图.png

# 比较两帧差异并定位变化区域
pixelprobe compare input.mp4 --frame-a 100 --frame-b 101 --output 差异.png

# 等距抽 9 帧拼网格图
pixelprobe sheet input.mp4 --count 9 --output 网格.png

# 周期闪烁检测（FFT 主频）
pixelprobe spectrum input.mp4 --source luma

# 稠密光流（需要 pip install "pixelprobe[flow]"）
pixelprobe flow input.mp4 --frame-a 100 --frame-b 101 --flow-output 流场.png
```

命令一览：

| 命令 | 功能 |
| --- | --- |
| `info` | 查看图片或视频信息 |
| `frame` | 按帧号或时间提取画面 |
| `pixel` | 查询一个或多个像素 |
| `region` | 分析矩形区域 |
| `timeline` | 生成像素颜色时间线 |
| `xt` | 生成水平扫描线的 X–T 切片 |
| `yt` | 生成垂直扫描线的 Y–T 切片 |
| `changes` | 定位变化最大的帧并分段为事件区间 |
| `scan` | 一键概览：网格图 + 变化事件 + 异常帧 |
| `reduce` | 时间域合成：逐像素 mean/median/min/max/std/diff 统计图 |
| `compare` | 比较任意两帧：差异热力图与变化区域 bbox |
| `sheet` | 等距抽帧拼接采样网格图 |
| `spectrum` | 时间域 FFT：周期闪烁 / 周期变化检测 |
| `spectrum2d` | 单帧空间 FFT：条纹 / 摩尔纹检测 |
| `flow` | 稠密光流与全局运动估计（需 `[flow]` 可选依赖） |

所有命令均支持：

- `--json`：输出机器可读 JSON；
- `--quiet`：只显示必要结果；
- `--verbose`：显示详细错误信息；
- `--no-progress`：关闭进度条。

使用 `pixelprobe <命令> --help` 可以查看该命令的全部参数。

## AI 如何使用 PixelProbe

PixelProbe 不会取代 AI 原有的视频处理能力。AI 的原生视觉负责理解对象、动作、
事件与上下文；PixelProbe 的变化检测、时间线和时空切片用于缩小候选范围，
并把视觉判断核对到精确的帧、时间、坐标和颜色。

变化峰值只表示像素变化较大，不能单独证明某个对象移动或某个事件发生。
AI 应把原生视频理解作为主要依据，并查看候选点前后的画面进行交叉确认。

例如，当你询问“这个球从什么时候开始移动”时，AI 可以自动完成：

1. 使用原生视频/视觉能力理解场景并识别球；
2. 读取视频信息，确定准确尺寸、帧数和时间范围；
3. 对球所在区域扫描变化，只把结果当作候选时刻；
4. 提取候选点之前、当时和之后的画面，用视觉能力确认移动；
5. 使用 PixelProbe 的真实帧号和时间戳给出精确答案。

MCP 提供媒体信息、取帧、像素、区域、时间线、时空切片、变化检测、一键扫描、
时间域合成、两帧比较、采样网格、频域分析和光流等只读工具，全部参数直接
暴露在工具 schema 顶层（无嵌套对象）。面对未知视频，建议先用
`pixelprobe_scan_media` 一次调用建立概览，再逐步聚焦。
需要写入文件时，AI 会使用独立的 PNG 保存工具，并明确提供输出路径；同名文件会被覆盖。

光流分析（`pixelprobe_optical_flow` / `pixelprobe flow`）依赖 OpenCV，
按需安装：`pip install "pixelprobe[flow]"`。未安装时其余功能不受影响，
调用光流会返回带安装提示的 `DEPENDENCY_MISSING` 错误。

仓库内置 Claude Code 技能 `.claude/skills/pixelprobe-video-analysis/SKILL.md`，
包含完整的"场景 → 工具"决策表、统计图/光流图/频谱判读方法和参数经验值。
在本仓库目录内使用 Claude Code 时自动可用；想在任意目录使用，
把该技能目录复制到 `~/.claude/skills/` 即可。

## 坐标与时间

- 坐标原点位于左上角，x 向右，y 向下；
- 所有坐标都对应原始媒体分辨率；
- 帧号从 `0` 开始；
- 帧范围包含起始帧和结束帧；
- 时间以媒体首帧为 `0` 秒；
- 按时间取帧时，选择时间戳不晚于目标时间的最后一帧；
- 可变帧率视频使用真实帧时间，不按平均帧率估算。

## 输入与输出

图片支持 PNG、JPEG、BMP、WebP 等常见格式。视频支持 MP4、MKV、AVI、MOV 等
常见容器，具体编码支持取决于本机的 FFmpeg/PyAV 环境。

分析结果可以输出为：

- PNG：帧、裁剪图、时间线和时空切片；
- CSV：像素时间线和逐帧变化记录；
- JSON：适合脚本和 AI 读取的结构化结果。

## 更多文档

- [API 参考](docs/api.md)
