# PixelProbe

简体中文 | [English](https://github.com/2061863797/PixelProbe/blob/main/README_EN.md)

**把图片与视频分析精确到帧、时间、坐标和像素。**

PixelProbe 是一个本地图片与视频分析命令行工具。它可以定位视频中的关键变化、
提取指定画面、读取像素颜色、统计区域特征，并把长视频转换成时间线、时空切片
和统计图。媒体文件始终在本机处理。

## 主要功能

- **媒体信息**：查看尺寸、帧率、帧数、时长、编码和可变帧率信息。
- **精确取帧**：按帧号或时间提取画面，支持局部裁剪和预览缩放。
- **像素探测**：读取一个或多个像素的 RGB、HEX、HSV、Lab 和亮度。
- **区域分析**：统计选区的平均值、中位数、最值、标准差和颜色特征。
- **变化检测**：扫描点、区域或整帧，找出变化最明显的帧和时间。
- **颜色时间线**：观察固定像素或网格区域在整段视频中的颜色变化。
- **X–T / Y–T 切片**：在一张图中观察移动、闪烁和镜头变化。
- **时间域合成**：显出噪声中的隐藏图案、慢变水印和运动能量分布。
- **一键扫描**：生成代表帧网格、变化事件和黑帧、白帧、闪帧等异常信息。
- **帧比较与采样网格**：定位两帧差异，或用一张网格图浏览完整视频。
- **光流与频域分析**：分析运动方向、周期闪烁、条纹和摩尔纹。
- **统一表示生成**：一次请求生成 X–T、Path–T、ROI–T、聚合或光流结果。
- **可校验结果包**：保存原始数值、预览、坐标映射、来源和 SHA-256 校验信息。

## 安装

普通用户建议从 [GitHub Releases](https://github.com/2061863797/PixelProbe/releases/latest)
下载对应系统的独立 CLI，无需预装 Python：

| 系统 | 下载文件 |
| --- | --- |
| Windows 64 位 | `pixelprobe-windows-x86_64.zip` |
| Linux 64 位 | `pixelprobe-linux-x86_64.tar.gz` |
| macOS Apple 芯片 | `pixelprobe-macos-arm64.tar.gz` |

压缩包根目录直接包含可执行文件。解压到单独目录后运行：

```powershell
# Windows PowerShell
Expand-Archive .\pixelprobe-windows-x86_64.zip -DestinationPath .\pixelprobe
Set-Location .\pixelprobe
.\pixelprobe.exe --version
```

```bash
# Linux；macOS 请替换为 pixelprobe-macos-arm64.tar.gz
mkdir pixelprobe
tar -xzf pixelprobe-linux-x86_64.tar.gz -C pixelprobe
cd pixelprobe
chmod +x pixelprobe
./pixelprobe --version
```

需要光流、Zarr 或 MCP 等可选能力时，请使用 Python 3.11+ 安装固定版本：

```bash
python -m pip install "pixelprobe @ git+https://github.com/2061863797/PixelProbe.git@v1.0.3"
```

完整的解压、PATH、校验、可选依赖、更新和卸载说明见
[安装指南](https://github.com/2061863797/PixelProbe/blob/main/docs/installation.md)。

## 快速示例

```bash
# 查看媒体信息
pixelprobe info input.mp4

# 提取第 120 帧
pixelprobe frame input.mp4 --frame 120 --output frame120.png

# 提取 3.5 秒处的画面并裁剪
pixelprobe frame input.mp4 --time 3.5 --crop 400,200,300,300 --output crop.png

# 查询两个像素
pixelprobe pixel input.mp4 --frame 120 --point 520,340 --point 600,400

# 查询 PNG/BMP/GIF 等图片的原生通道值（含 Alpha、调色板索引或 16 位样本）
pixelprobe pixel input.png --sample native --point 520,340 --json

# 分析矩形区域
pixelprobe region input.mp4 --frame 120 --rect 400,200,200,150

# 导出像素颜色时间线
pixelprobe timeline input.mp4 --point 520,340 --output timeline.png --csv timeline.csv

# 找出选区变化最大的 10 帧
pixelprobe changes input.mp4 --rect 400,200,200,150 --top 10

# 一键生成概览网格、变化事件和异常帧
pixelprobe scan input.mp4 --sheet-output overview.png

# 时间域标准差合成
pixelprobe reduce input.mp4 --op std --output temporal-std.png

# 比较两帧并定位变化区域
pixelprobe compare input.mp4 --frame-a 100 --frame-b 101 --output diff.png

# 等距抽取 9 帧并拼接网格
pixelprobe sheet input.mp4 --count 9 --output sheet.png

# 检测周期闪烁
pixelprobe spectrum input.mp4 --source luma

# 稠密光流
pixelprobe flow input.mp4 --frame-a 100 --frame-b 101 --flow-output flow.png
```

## 示例效果

下面的短视频包含被强噪声遮盖的低对比度数字。单看某一帧很难稳定判断，按连续帧区间
聚合后，随时间变化的数字结构会更加明显：

- [查看或下载输入视频](https://github.com/2061863797/PixelProbe/blob/main/docs/assets/pixelprobe-noise-demo.mp4)
- 分析结果按帧号和时间分成六个连续区间，坐标和范围保持可追溯。

![按连续帧区间得到的噪声视频分析结果](https://raw.githubusercontent.com/2061863797/PixelProbe/main/docs/assets/pixelprobe-noise-analysis.png)

这个示例体现了 PixelProbe 的用途：Agent 用视觉理解画面，PixelProbe 提供准确的帧号、
时间范围和确定性数值处理作为证据；分析结果本身不替代语义判断。

`pixel` 默认返回历史兼容的显示 RGB8 值。`--sample native` 仅适用于图片，响应会
明确给出 `sample_semantics`：已明确识别的无损常见格式返回 `stored_sample`；JPEG 等
有损或无法确认的格式返回指定解码器的 `decoded_sample`，不会把它误称为压缩前原始 RGB。


## 生成可复现结果包

`generate` 接受一个请求 JSON，并把原始数值、坐标映射、预览和复现信息保存为
一个 `.bundle` 目录。下面是 X–T 请求：

```json
{
  "source": {"source_id": "source_main", "kind": "file", "uri": "input.mp4"},
  "selection": {"mode": "all", "sample_every": 1},
  "representation": "xt",
  "geometry": {
    "type": "line",
    "coordinate_space_id": "storage_pixels",
    "points": [[0, 120], [1919, 120]],
    "closed": false
  },
  "feature": {"name": "rgb", "config": {}},
  "output": {"format": "bundle", "include_preview": true}
}
```

保存为 `request.json` 后运行：

```bash
pixelprobe generate input.mp4 --request request.json --output result.bundle
pixelprobe validate result.bundle
```

多个请求可以放在同一个 JSON 数组中。它们引用同一媒体时共享一次解码。
`validate` 默认验证所有登记文件的大小和 SHA-256；`--metadata-only` 只检查
结构和路径，不能用于宣称内容完整。

`feature_t` 可生成逐帧灰度、HSV、Lab、相邻帧绝对差、时间 FFT、空间 FFT、
STFT 和 Farneback 光流等正式数值。STFT 必须明确提供 `window`、`length`、
`hop`、`padding` 与 `normalization`，不规则时间轴会明确报错，不会被静默当成
等间隔数据。时间 FFT 同样默认拒绝 VFR；只有显式设置
`"vfr_policy": "estimate"` 才会使用带估算标志的兼容模式。所有颜色转换和频域
图像都只是从 Data 派生的 Preview。

长任务可以同时启用内容缓存和检查点；中断后只有计划、输入文件、算子版本和请求
完全匹配时才允许恢复：

```bash
pixelprobe generate input.mp4 --request request.json --output result.bundle \
  --cache-dir .pixelprobe-cache --checkpoint result.checkpoint.json
pixelprobe generate input.mp4 --request request.json --output result.bundle \
  --cache-dir .pixelprobe-cache --resume-from result.checkpoint.json
```

`pixelprobe validate result.bundle --strict` 会把未知可选字段和未登记文件也视为错误。

## AI Agent 接入 MCP

PixelProbe MCP 是现有 Python 核心的薄适配层，不会复制或改变分析算法。它通过
本地 `stdio` 工作：Agent 负责理解人物、物体、文字、场景和构图；PixelProbe
负责核实帧号、时间、坐标、像素、区域统计、变化候选和正式数值表示。

在支持 MCP 的 Agent 中添加本地服务器：

```json
{
  "mcpServers": {
    "pixelprobe": {
      "command": "pixelprobe-mcp",
      "env": {
        "PIXELPROBE_MCP_ROOTS": "D:\\media"
      }
    }
  }
}
```

客户端会自行启动 `pixelprobe-mcp`，不要在普通终端中手动运行。Windows 可用分号、
macOS/Linux 可用冒号在 `PIXELPROBE_MCP_ROOTS` 中配置多个允许目录。服务器拒绝读取
这些目录之外的路径。正式 Bundle 默认写入第一个允许目录下的
`.pixelprobe-mcp/artifacts/`；也可用 `PIXELPROBE_MCP_ARTIFACT_ROOT` 指定其中的其他目录。

接入后，Agent 应先调用 `pixelprobe_inspect_media`，再调用
`pixelprobe_get_frame` 使用自身视觉查看原始画面，最后按需用精确像素、区域、变化和
Artifact 工具核实。MCP 还提供 `pixelprobe_analyze_media` Prompt 和
`pixelprobe://guidance` Resource，用于注入“视觉为主、确定性数据为辅助”的分析原则。

检查图片时，响应会分别说明存储样本通道、确定性分析使用的 RGB 通道和视觉 PNG
通道，并提供调色板使用量、完整 Alpha 统计及规则高频纹理候选。纹理候选附带检测范围、
周期和相关性证据，只用于提醒 Agent 复核，不会被描述为已经确认的压缩、损坏或伪造。

`pixelprobe_get_frame` 不会缩放画面。原始 PNG 超过客户端载荷限制时会明确失败并建议
裁剪或分区查看，不会静默返回缩略图。生成表示是唯一写操作，只能写入受控 Artifact
目录且不覆盖已有结果；其余工具均为只读操作。

### 更新 MCP

更新 PixelProbe 后，MCP 客户端必须重启服务器进程才会加载新版本。若使用克隆的仓库：

```bash
git pull
python -m pip install -e ".[mcp]"
```

若直接从 GitHub 安装，请把 `<发布标签或提交哈希>` 替换为目标版本并强制重新安装：

```bash
python -m pip install --upgrade --force-reinstall "pixelprobe[mcp] @ git+https://github.com/2061863797/PixelProbe.git@<发布标签或提交哈希>"
```

然后重启支持 MCP 的 Agent/桌面客户端，再调用 `pixelprobe_get_capabilities` 确认
`pixelprobe_version`。

## 命令一览

| 命令 | 功能 |
| --- | --- |
| `info` | 查看图片或视频信息 |
| `frame` | 按帧号或时间提取画面 |
| `pixel` | 查询一个或多个像素；`--sample native` 可读取图片原生通道值 |
| `region` | 分析矩形区域 |
| `timeline` | 生成像素颜色时间线 |
| `xt` | 生成水平扫描线的 X–T 切片 |
| `yt` | 生成垂直扫描线的 Y–T 切片 |
| `changes` | 定位变化最大的帧并分段为事件区间 |
| `scan` | 生成网格图、变化事件和异常帧概览 |
| `reduce` | 生成逐像素 mean/median/min/max/std/diff 统计图 |
| `compare` | 比较任意两帧并生成差异图 |
| `sheet` | 等距抽帧并拼接采样网格 |
| `spectrum` | 执行时间域 FFT，检测周期变化 |
| `spectrum2d` | 执行单帧空间 FFT，检测条纹或摩尔纹 |
| `flow` | 计算稠密光流与全局运动估计 |
| `generate` | 按请求生成一个或多个正式表示及 Bundle |
| `validate` | 完整校验 Bundle 的结构、文件和 SHA-256 |
| `cache clear` | 清理可安全删除的本机执行缓存 |

使用 `pixelprobe <命令> --help` 查看某个命令的全部参数。

## 输出模式

所有命令都支持 `--json`。分析类命令通常还支持：

- `--quiet`：只显示必要结果；
- `--verbose`：显示详细错误信息；
- `--no-progress`：关闭进度条。

脚本集成建议使用 JSON 模式，并根据进程退出码判断成功或失败：

```bash
pixelprobe info input.mp4 --json
```

业务错误会映射为稳定的非零退出码；诊断信息写入标准错误，不会污染 JSON。

## 坐标与时间约定

- 坐标原点位于左上角，x 向右，y 向下；
- 所有坐标都对应原始媒体分辨率；
- 帧号从 `0` 开始；
- 常用分析命令的帧范围包含起始帧和结束帧；
- `generate` 请求中的 `requested_end_frame_exclusive` 和时间结束值不包含在范围内；
- 时间以媒体首帧为 `0` 秒；
- 按时间取帧时，选择时间戳不晚于目标时间的最后一帧；
- 可变帧率视频使用真实帧时间，不按平均帧率估算。

变化峰值只表示像素变化较大，不能单独证明某个对象移动或某个事件发生。
分析事件时应检查候选帧之前、当时和之后的原始画面。

## 输入与输出

图片支持 PNG、JPEG、BMP、WebP 等常见格式。视频支持 MP4、MKV、AVI、MOV
等常见容器，具体编码支持取决于本机 FFmpeg/PyAV 环境。

结果可以写为：

- PNG：帧、裁剪图、时间线、时空切片和分析图；
- CSV：像素时间线和逐帧变化记录；
- JSON：适合脚本和自动化流程读取的结构化结果；
- NPY：保留 dtype、shape 和原始数值，支持局部读取；
- Zarr v3：可选的大型分块数组格式；
- Bundle：包含 Data、Preview、坐标映射、来源、ExecutionPlan、执行事件、
  provenance 和完整性校验。

Preview 只用于显示，不会替代或缩小 Data。清理本机缓存不会删除任何 Bundle。

## 开源许可

PixelProbe 采用 [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)
开源。使用、修改和分发时请遵守仓库中 `LICENSE` 的完整条款；第三方依赖仍适用其
各自的许可证。
