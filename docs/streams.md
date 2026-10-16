# 视频码流结构（streams）

`streams` 读取视频的容器与压缩包结构，给出取证与质量排查用的**可复核线索**。
缺省不解码画面，长视频也很快；`--frame-types`（MCP `options.frame_types=true`）再顺序解码一遍统计 I/P/B 帧。

```powershell
pixelprobe streams "片子.mp4"
pixelprobe streams "片子.mp4" --frame-types --json
```

MCP：`pixelprobe_evidence`，视频扩展名自动选 `kind=streams`（图片为 `kind=image`）。

## 输出

| 字段 | 内容 |
|---|---|
| `container` | 封装格式、时长、起始时间、总码率、文件大小、容器标签（如 encoder、creation_time） |
| `streams` | 每路流的编码、profile、码率、time_base、标签；视频另有尺寸、像素格式、色彩范围/空间/原色/传递特性、像素宽高比、平均/标称帧率 |
| `video_packets` | 主视频流压缩包结构：包数、关键帧数与位置、GOP 长度（列表、统计、直方图、末段长度）、是否固定、B 帧（按 pts/dts 顺序推断）、dts 回退数、pts 重复数、中位帧间隔、时间戳空隙（> 1.5 倍中位间隔）、包大小统计（全部 / 关键帧 / 非关键帧）、每秒字节序列 |
| `frame_types` | 仅 `--frame-types`：各类型计数与解码顺序序列（I/P/B；S 为 GMC，小写 i/p/b 为 SI/SP/BI） |
| `clues` | 线索：`MUXER_TAG`、`NO_CREATION_TIME`、`IRREGULAR_GOP`、`SINGLE_KEYFRAME`、`TIMESTAMP_IRREGULARITY`、`TIMESTAMP_GAPS`、`VARIABLE_FRAME_RATE`、`REORDER_WITHOUT_B`，各附证据 |
| `limitations` | 局限说明；`automatic_authenticity_conclusion` 恒为 null |

## 局限

- 容器标签与编码参数可被任意改写，只能作为线索，不能证明来源或真伪。
- 封装器标签（如 `Lavf`）只说明最后一次封装所用的软件，不等于重新编码。
- GOP 不规则可能来自场景切换自适应关键帧、直播推流或剪辑拼接，需结合画面判断
  （可配合 `find "cut"` 对照关键帧位置与镜头切换）。
- 未解码时帧类型未知；B 帧存在与否由 pts/dts 顺序推断。
- 不分析量化参数（QP）：PyAV 不提供逐帧 QP，需要专门的码流解析器，暂不做。
