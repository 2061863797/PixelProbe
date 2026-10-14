# 组合分析流程

当前源码提供 Python run_workflow、CLI workflow 和 MCP pixelprobe_workflow。
三个流程共享有界流式读取、资源预算、输入哈希复验和原子 Bundle 提交；
MCP 同步调用与 operation=workflow 异步任务经过同一调度器。

## 三个流程

| workflow | 行为 | 适用边界 |
| --- | --- | --- |
| first_change | 按顺序比较相邻采样帧，返回最早超过阈值的帧对、时间和变化外接矩形，命中后停止 | 视频；仅定位检查范围内的变化，采样间隙可能漏掉瞬时变化 |
| compare_media | 比较两个图片或两个视频；视频按相同展示帧号配对 | 要求类型、尺寸相同，不缩放、不做内容或时间对齐；短视频结束后停止并标出未配对范围 |
| quality_overview | 汇总亮度与变化数值，以及黑、白、低亮度标准差候选 | 图片或视频；不是主观质量评分，不自动判断损坏或真伪 |

帧号从 0 开始，start_frame / end_frame 为闭区间；默认范围 0..1799，
sample_every 默认为 1，最多保留 10000 条采样记录。超过限制应显式缩小范围或增大采样间隔。
元数据帧数不是解码上限；实际没有帧或没有可配对帧时明确报错。

每像素取 RGB8 三通道绝对差的最大值，严格大于 threshold 才算变化像素。
变化像素占比严格大于 min_changed_ratio 时命中候选。默认 threshold=10，
min_changed_ratio=0。没有候选不代表范围外、采样间隙、Alpha、音频或语义内容没有变化。

亮度使用未线性化 RGB8 的 0.2126R+0.7152G+0.0722B；全帧 RGB 最大值小于 16
标记黑帧候选，最小值大于 240 标记白帧候选，亮度标准差小于 2 标记低变化候选。
这些候选可以是正常画面。图片只读取首张/首帧，不展开动图或多页图片。

## 用法

```powershell
pixelprobe workflow "视频.mkv" --mode first_change --at "f=..899" --output "首次变化.bundle" --json
pixelprobe workflow "新版.mkv" --mode compare_media --reference "旧版.mkv" --at "f=..299" --output "版本比较.bundle" --json
pixelprobe workflow "视频.mkv" --mode quality_overview --at "f=..1799 step=10" --output "质量概览.bundle" --json
```

```python
from pathlib import Path
from pixelprobe.workflows import WorkflowOptions, run_workflow

result = run_workflow(
    Path("视频.mkv"),
    output_path=Path("结果.bundle"),
    options=WorkflowOptions(workflow="first_change", start_frame=0, end_frame=899),
)
```

MCP 同步调用参数：

```json
{
  "media_path": "C:/media/new.mkv",
  "reference_path": "C:/media/old.mkv",
  "options": {"workflow": "compare_media", "end_frame": 299},
  "output_name": "comparison.bundle"
}
```

长任务使用 pixelprobe_submit_job，operation 为 workflow，payload 为上述对象。
两个输入都独立执行白名单检查，reference_path 不构成额外授权。
完成后通过 pixelprobe_get_job_result 领取摘要，再用现有 Artifact 分页接口读取结果。
组合流程没有 checkpoint，重启后不会自动重跑，不支持 resume_job 恢复。

## 数值、预览和覆盖范围

Bundle 内 workflow_metrics 保存逐帧/逐帧对的 float64 数值表，包括展示帧号、
两个时间戳、平均与最大差值、变化占比、亮度均值/标准差和 RGB 极值。
表中不适用值为 NaN，另有 validity 掩码；首个时间帧没有前驱，不能用零伪装比较结果。
每列定义、单位、阈值和局限保存在 Tensor schema 内。

存在可比较帧对时，还保存逐通道 uint8 绝对差和对应 PNG 预览，并通过 DAG 关联。
首次变化模式展示命中帧对；其他模式展示平均绝对差最大的帧对。
预览直接显示差值、不拉伸颜色，细小差异可能看起来很暗，应读取正式数值。
单张图片的质量概览只有数值报告，没有虚构差异图。

coverage 分别报告每个来源的请求范围、已分析数量、实际解码与转换数量、
最后已分析帧、采样间隙、EOF 确认和异常时间戳标记。
视频解码为顺序单遍，读取后继帧以确定持续时间时可能比已分析范围多一帧；
这些开销计入实际计数。高起始帧仍需解码前缀，采样也不会省去被跳过帧的底层解码。
两个视频的时间轴不自动同步，报告给出最大配对时间差，原始时间保存在数值表。

分析开始与提交前完整读取源文件计算哈希；局部解码不消除这两次源文件读取成本。
工作内存包含两个来源、数组副本和统计结果，结果写入沿用共享磁盘预算。
取消、超限或输入变化时不提交可领取 Bundle。

当前验收覆盖 Windows 源码和本地冻结程序；Linux/macOS 的 Python/uvx 入口仍须
在对应平台实际运行，不能用 Windows 结果代替。
