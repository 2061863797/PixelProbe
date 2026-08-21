# PixelProbe 核心数据模型设计

> 文档状态：规范性草案 0.1  
> 对应项目阶段：V0.6 及以后  
> 本文是时间、坐标、像素、Tensor 与映射语义的唯一权威定义。

## 1. 目标与非目标

本文定义 PixelProbe 各模块之间交换数据时必须遵守的类型、字段、数学约定和错误行为，使媒体读取、时空采样、特征计算、存储和外部接口使用同一套语义。

本文负责：

- 媒体来源与内容身份。
- 帧的显示顺序、时间戳、像素语义和来源。
- 时间选择、坐标空间、几何对象和变换链。
- 多维数值场、轴、通道、单位和有效性。
- 输出到输入的可计算映射。
- 字段级精度与溯源引用。

本文不负责：

- 算子的调度、缓存和并行执行。
- Bundle 的磁盘目录和原子提交过程。
- 具体光流、频谱或质量分析算法。
- GUI、MCP、模型推理和远程媒体传输。

## 2. 规范用语与基础约定

“必须”“不得”表示强制要求；“应该”表示除非有明确理由否则必须遵守；“可以”表示可选能力。

### 2.1 时间

- `presentation_index` 按显示顺序编号，从 `0` 开始，不能使用解码顺序代替。
- 新接口中的时间区间统一为半开区间 `[start, end)`。
- 旧 CLI 的 `--start-frame/--end-frame` 保持闭区间；兼容层必须将结束帧转换成新选择模型中的排他边界。
- 原始时间必须由整数 `pts` 和有理数 `time_base` 表示；浮点秒数只是派生值。
- `source_timestamp_seconds = pts × time_base`。
- `timeline_time_seconds` 是相对媒体展示起点归一化后的时间，正常情况下第一帧为 `0`。
- PTS 缺失、重复、倒退或时间轴存在空洞时不得静默修正，必须写入异常标志。

### 2.2 坐标

- 存储像素坐标原点位于左上角，`x` 向右，`y` 向下。
- 整数 `(x, y)` 表示对应存储像素的中心。
- 连续坐标中，像素 `(x, y)` 的覆盖区域为 `[x-0.5,x+0.5) × [y-0.5,y+0.5)`。
- 矩形使用 `[x,x+width) × [y,y+height)` 半开区间。
- 裁剪默认要求矩形完全位于有效范围内；截断、填充、镜像或环绕必须显式指定。
- `storage`、`display`、`normalized`、`camera`、`world` 是不同坐标空间，不能只靠字段名称推断。

### 2.3 像素值

- `stored_sample`：码流或无损文件中实际保存的通道/采样值，例如调色板索引或 YUV 采样。
- `decoded_sample`：指定解码器和版本解码得到的数值。
- `display_value`：应用方向、色彩管理、Alpha 和显示变换后得到的数值。
- JPEG 和有损视频不存在可恢复的“压缩前原始 RGB”，不得将解码 RGB 标记为 `stored_sample` 或绝对原始值。
- 精确数据和预览数据必须分别保存；任何缩放、归一化、色彩映射或对比度增强都会生成新的 Preview，不得覆盖 Data。

### 2.4 数值与序列化

- 高频运行时对象使用 `@dataclass(slots=True)`；不可变描述对象同时使用 `frozen=True`。
- 公开请求、JSON 响应和 manifest 使用 Pydantic v2。
- JSON 字段使用 `snake_case`，UTF-8 编码，时间单位为秒，大小单位为字节。
- `NaN`、`Infinity` 和 `-Infinity` 不得直接写入 JSON；必须转换为 `null` 并附加有效性或异常信息。

## 3. 枚举

```python
from enum import StrEnum

class AccuracyLevel(StrEnum):
    EXACT = "exact"
    DECODED = "decoded"
    DERIVED = "derived"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"

class CoordinateSpaceKind(StrEnum):
    STORAGE = "storage"
    DISPLAY = "display"
    NORMALIZED = "normalized"
    CAMERA = "camera"
    WORLD = "world"

class AxisKind(StrEnum):
    TIME = "time"
    X = "x"
    Y = "y"
    PATH = "path"
    CHANNEL = "channel"
    FREQUENCY = "frequency"
    BATCH = "batch"
    FEATURE = "feature"

class StorageKind(StrEnum):
    MEMORY = "memory"
    NPY = "npy"
    MEMMAP = "memmap"
    ZARR = "zarr"
    ARTIFACT = "artifact"
```

枚举值一经进入正式 Bundle 不得改变含义；新增值允许，重命名必须通过 Schema 迁移完成。

## 4. 公共辅助类型

### 4.1 AccuracyInfo

```python
from pydantic import BaseModel, Field

class AccuracyInfo(BaseModel):
    level: AccuracyLevel
    source: str = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    tolerance: float | None = Field(default=None, ge=0)
    unit: str | None = None
```

不变量：

- `exact` 表示直接来自可靠结构或无损整数读取，不表示现实世界真值。
- `decoded` 必须能通过 provenance 找到解码器名称和版本。
- `estimated` 应提供假设或估计方法。
- `tolerance` 仅表示数值比较容差，不能替代精度等级。

### 4.2 ProvenanceRef

```python
class ProvenanceRef(BaseModel):
    provenance_id: str
    manifest_uri: str | None = None
```

运行时对象只保存引用，不复制完整溯源图。`provenance_id` 在所属 Bundle 或执行会话内必须唯一。

### 4.3 ArtifactRef

```python
class ArtifactRef(BaseModel):
    artifact_id: str
    media_type: str
    uri: str
    sha256: str
    schema_version: str
```

`sha256` 使用小写十六进制完整内容哈希。相对 URI 以 Bundle 根目录为基准。

## 5. MediaSource 与 MediaIdentity

### 5.1 MediaSource

```python
class MediaSource(BaseModel):
    source_id: str
    kind: Literal["file", "image_sequence", "frame_stream"]
    uri: str
    sequence_manifest: str | None = None
    declared_media_type: Literal["image", "video", "image_sequence"] | None = None
```

| 字段 | 必填 | 说明 |
|---|---:|---|
| `source_id` | 是 | 单次请求内稳定标识 |
| `kind` | 是 | V1 正式支持 `file`；其余保留接口 |
| `uri` | 是 | 本地文件路径或受控资源 URI |
| `sequence_manifest` | 条件 | 图片序列必须提供每张图片的时间信息 |
| `declared_media_type` | 否 | 用户声明，只作为提示，不能代替实际探测 |

V1 不接受网络 URL。图片序列不得仅通过文件名排序推断时间，必须由 manifest 提供展示顺序、时间戳和持续时间。

### 5.2 MediaIdentity

```python
class MediaIdentity(BaseModel):
    source_id: str
    size_bytes: int = Field(ge=0)
    sha256: str
    file_id: str | None = None
    modified_time_ns: int | None = None
    actual_format: str | None = None
```

完整 SHA-256 是持久化 Artifact 和跨机器复现的内容身份。文件 ID、大小和修改时间只用于当前进程的快速变化检测，不能单独作为持久缓存键。

## 6. FramePacket

`FramePacket` 是解码层向计算层交付单个展示帧的运行时对象。

```python
from dataclasses import dataclass
from fractions import Fraction
import numpy as np

@dataclass(slots=True)
class FramePacket:
    data: np.ndarray
    presentation_index: int
    decode_index: int | None
    pts: int | None
    dts: int | None
    time_base: Fraction
    source_timestamp_seconds: float | None
    timeline_time_seconds: float
    duration_pts: int | None
    duration_seconds: float | None
    key_frame: bool | None
    stored_pixel_format: str | None
    decoded_pixel_format: str
    color_metadata: dict[str, object]
    transform_chain: "TransformChain"
    sample_semantics: Literal["decoded_sample", "display_value"]
    accuracy: AccuracyInfo
    provenance: ProvenanceRef
    flags: tuple[str, ...] = ()
```

不变量：

- `presentation_index >= 0`，同一流内连续且唯一。
- `data` 的前两维必须与解码后的存储宽高一致。
- V1 视频帧默认输出 `RGB uint8`、形状 `[height,width,3]`，并标记为 `decoded_sample`。
- `timeline_time_seconds >= 0`；异常负时间必须通过归一化和映射显式保存。
- PTS 缺失时 `pts=None`，不得根据平均 FPS 伪造 PTS。
- `flags` 至少支持 `PTS_MISSING`、`PTS_DUPLICATE`、`PTS_NON_MONOTONIC`、`TIMELINE_GAP`、`FORMAT_CHANGED`。

FramePacket 生命周期只覆盖当前执行过程，不直接序列化像素数组。需要持久化时转换成 TensorField 和 Artifact。

## 7. TemporalSelection

```python
class TemporalSelection(BaseModel):
    mode: Literal["all", "frame_interval", "time_interval", "indices"]
    requested_start_frame: int | None = None
    requested_end_frame_exclusive: int | None = None
    requested_start_seconds: float | None = None
    requested_end_seconds: float | None = None
    requested_indices: tuple[int, ...] = ()
    sample_every: int = Field(default=1, ge=1)
    resolved_presentation_indices: tuple[int, ...] = ()
    resolved_timestamps_seconds: tuple[float, ...] = ()
    mapping_id: str | None = None
```

规则：

- 每种 `mode` 只能填写对应的一组请求字段。
- `frame_interval` 和 `time_interval` 均采用排他结束边界。
- `indices` 必须严格递增且不能重复。
- 解析完成后，帧索引与时间戳数组长度必须相等。
- 时间区间命中规则是选择展示时间戳满足 `start <= t < end` 的帧。
- 时间轴空洞不能用上一帧填充，除非请求显式设置填充策略。

旧 CLI 闭区间 `[start_frame,end_frame]` 转换为 `[start_frame,end_frame+1)`；若 `end_frame` 是最大整数或越界，应先进行范围校验。

## 8. ArrayHandle

大型 TensorField 通过统一只读句柄访问。

```python
from typing import Protocol, Sequence

class ArrayHandle(Protocol):
    @property
    def shape(self) -> tuple[int, ...]: ...

    @property
    def dtype(self) -> str: ...

    @property
    def storage_kind(self) -> StorageKind: ...

    @property
    def chunk_shape(self) -> tuple[int, ...] | None: ...

    def read(self, selection: tuple[slice | int, ...]) -> np.ndarray: ...

    def materialize(self, *, max_bytes: int | None = None) -> np.ndarray: ...
```

实现必须：

- 保持读取结果的轴顺序和 dtype。
- 对越界 selection 报稳定错误，不自动截断。
- `materialize` 超过资源限制时失败，不降低分辨率。
- 文件型句柄在读取前验证目标 Artifact 身份。
- 不向调用方暴露可修改底层缓存的数组视图。

## 9. AxisSpec 与 ChannelSpec

```python
class AxisSpec(BaseModel):
    name: str
    kind: AxisKind
    length: int = Field(ge=0)
    unit: str | None = None
    coordinate_mode: Literal["index", "regular", "irregular"] = "index"
    start: float | None = None
    step: float | None = None
    coordinates_ref: ArtifactRef | None = None
    mapping_id: str | None = None

class ChannelSpec(BaseModel):
    name: str
    unit: str | None = None
    semantic: str
    value_range: tuple[float, float] | None = None
    accuracy: AccuracyInfo
```

不变量：

- TensorField 中轴名称必须唯一。
- `regular` 必须提供 `start` 和非零 `step`。
- `irregular` 必须提供 `coordinates_ref`。
- 时间轴单位固定为 `second`；原始 PTS 通过映射或索引 Artifact 保存。
- `channel` 轴长度必须等于 `channels` 数量。
- 通道不得只用含糊名称，如 `value1`；必须表达数值语义。

## 10. CoordinateSpace 与 TransformChain

```python
class CoordinateSpace(BaseModel):
    coordinate_space_id: str
    kind: CoordinateSpaceKind
    axes: tuple[str, ...]
    width: int | None = None
    height: int | None = None
    unit: str = "pixel"
    parent_space_id: str | None = None

class TransformStep(BaseModel):
    operation: Literal[
        "identity", "exif_orientation", "rotate", "scale",
        "translate", "crop", "affine", "perspective"
    ]
    parameters: dict[str, object]
    rounding: Literal["none", "nearest", "floor", "ceil"] = "none"

class TransformChain(BaseModel):
    source_space_id: str
    target_space_id: str
    steps: tuple[TransformStep, ...]
    invertible: bool
```

变换按 `steps` 顺序执行。色彩转换和 Alpha 合成不是空间变换，不得放入 TransformChain。

## 11. Geometry

公开请求使用带判别字段的几何模型：

```python
class PointGeometry(BaseModel):
    type: Literal["point"] = "point"
    coordinate_space_id: str
    x: float
    y: float

class RectGeometry(BaseModel):
    type: Literal["rect"] = "rect"
    coordinate_space_id: str
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)

class PathGeometry(BaseModel):
    type: Literal["line", "polyline", "curve"]
    coordinate_space_id: str
    points: tuple[tuple[float, float], ...]
    closed: bool = False

class MaskGeometry(BaseModel):
    type: Literal["mask"] = "mask"
    coordinate_space_id: str
    mask_ref: ArtifactRef
```

几何定义和实际采样点必须分开。路径的插值方法、重采样数量、宽度、横截面聚合和边界策略属于 Sampling Operator 配置，不属于 Geometry 本身。

## 12. AxisMapping

AxisMapping 描述输出如何对应输入，必须可以被程序查询和组合。

```python
class AxisMapping(BaseModel):
    mapping_id: str
    kind: Literal["affine", "index", "interval", "weighted", "lookup", "composite"]
    input_artifact_id: str
    input_axes: tuple[str, ...]
    output_artifact_id: str | None = None
    output_axes: tuple[str, ...]
    parameters: dict[str, object]
    child_mapping_ids: tuple[str, ...] = ()
    accuracy: AccuracyInfo
```

各映射的规范参数：

| `kind` | 必需参数 | 用途 |
|---|---|---|
| `affine` | `scale`, `offset` | 规则坐标或时间缩放 |
| `index` | `indices_ref` | 输出位置对应离散输入索引 |
| `interval` | `starts_ref`, `ends_ref` | 每个输出对应输入区间 |
| `weighted` | `indices_ref`, `weights_ref` | 插值和聚合 |
| `lookup` | `coordinates_ref` | VFR、曲线和非线性映射 |
| `composite` | `child_mapping_ids` | 按顺序组合映射 |

规则映射不得展开成逐元素表。不规则映射的数据必须存为索引 Artifact，不能嵌入超大 JSON。

## 13. TensorField

```python
@dataclass(slots=True, frozen=True)
class TensorField:
    tensor_id: str
    data: ArrayHandle
    axes: tuple[AxisSpec, ...]
    channels: tuple[ChannelSpec, ...]
    coordinate_space: CoordinateSpace | None
    axis_mappings: tuple[AxisMapping, ...]
    validity: ArrayHandle | None
    accuracy: AccuracyInfo
    provenance: ProvenanceRef
    attributes: dict[str, object]
```

持久化和公开 JSON 不直接序列化运行时 ArrayHandle，而使用 Pydantic 描述模型：

```python
class TensorFieldDescriptor(BaseModel):
    schema_version: str
    tensor_id: str
    data_ref: ArtifactRef
    dtype: str
    shape: tuple[int, ...]
    axes: tuple[AxisSpec, ...]
    channels: tuple[ChannelSpec, ...]
    coordinate_space_id: str | None
    mapping_ids: tuple[str, ...]
    validity_ref: ArtifactRef | None = None
    accuracy: AccuracyInfo
    provenance_id: str
    attributes: dict[str, object] = Field(default_factory=dict)
```

不变量：

- `len(axes) == len(data.shape)`。
- 每个 `AxisSpec.length` 必须等于对应 shape 维度。
- `data.dtype` 必须等于序列化描述中的 dtype。
- `validity` 若存在，其 shape 必须可广播到 data shape，dtype 必须是 boolean。
- Data Tensor 保留原始或计算数值；预览结果必须使用新的 tensor/artifact ID。
- `attributes` 只能保存小型、可 JSON 序列化的补充信息，不能绕过正式字段。

推荐轴顺序：

- 视频帧序列：`time,y,x,channel`。
- X-T：`time,x,channel`。
- Y-T：`time,y,channel`。
- Path-T：`time,path,channel`。
- ROI-T 聚合：`time,channel`。
- 稠密光流：`time,y,x,channel`，通道为 `flow_x,flow_y`；幅度可以是派生通道或独立 Tensor。

## 14. 有效示例

下面是一个 Path-T RGB TensorField 的可序列化描述；数组数据保存在 NPY Artifact 中。

```json
{
  "schema_version": "0.1.0",
  "tensor_id": "tensor_path_t_rgb",
  "data_ref": {
    "artifact_id": "data_path_t_rgb",
    "media_type": "application/x-npy",
    "uri": "artifacts/data_path_t_rgb/data.npy",
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "schema_version": "0.1.0"
  },
  "dtype": "uint8",
  "shape": [300, 512, 3],
  "axes": [
    {
      "name": "time",
      "kind": "time",
      "length": 300,
      "unit": "second",
      "coordinate_mode": "irregular",
      "coordinates_ref": {
        "artifact_id": "index_time",
        "media_type": "application/x-npy",
        "uri": "indexes/index_time.npy",
        "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "schema_version": "0.1.0"
      },
      "mapping_id": "map_time"
    },
    {
      "name": "path",
      "kind": "path",
      "length": 512,
      "unit": "pixel",
      "coordinate_mode": "irregular",
      "coordinates_ref": {
        "artifact_id": "index_path",
        "media_type": "application/x-npy",
        "uri": "indexes/index_path.npy",
        "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        "schema_version": "0.1.0"
      },
      "mapping_id": "map_path"
    },
    {
      "name": "channel",
      "kind": "channel",
      "length": 3,
      "unit": null,
      "coordinate_mode": "index",
      "mapping_id": null
    }
  ],
  "channels": [
    {"name": "r", "unit": "code_value", "semantic": "decoded_srgb_red", "value_range": [0, 255], "accuracy": {"level": "decoded", "source": "pyav", "assumptions": [], "tolerance": null, "unit": "code_value"}},
    {"name": "g", "unit": "code_value", "semantic": "decoded_srgb_green", "value_range": [0, 255], "accuracy": {"level": "decoded", "source": "pyav", "assumptions": [], "tolerance": null, "unit": "code_value"}},
    {"name": "b", "unit": "code_value", "semantic": "decoded_srgb_blue", "value_range": [0, 255], "accuracy": {"level": "decoded", "source": "pyav", "assumptions": [], "tolerance": null, "unit": "code_value"}}
  ],
  "coordinate_space_id": "storage_pixels",
  "mapping_ids": ["map_time", "map_path"],
  "validity_ref": null,
  "accuracy": {"level": "decoded", "source": "path_sampling", "assumptions": ["bilinear interpolation"], "tolerance": null, "unit": null},
  "provenance_id": "prov_path_t_rgb"
}
```

## 15. 无效示例

```json
{
  "tensor_id": "invalid",
  "dtype": "uint8",
  "shape": [300, 512, 3],
  "axes": [
    {"name": "time", "kind": "time", "length": 299},
    {"name": "path", "kind": "path", "length": 512}
  ],
  "channels": ["r", "g", "b"]
}
```

该对象必须被拒绝，因为轴数量与 shape 维度不一致、时间轴长度不一致、缺少 channel 轴，且 channels 没有正式 ChannelSpec。

## 16. 生命周期与数据流

```text
MediaSource
  → probe
MediaIdentity + stream metadata
  → decode
FramePacket stream
  → operator
TensorField
  → artifact writer
ArtifactRef + AxisMapping + ProvenanceRef
```

规则：

1. 打开媒体时建立 MediaIdentity 快照。
2. 解码期间持续检查文件是否变化。
3. FramePacket 只在执行期间流动，不作为大型 JSON 返回。
4. Operator 输出 TensorField；大数据先写临时 Artifact。
5. Artifact 提交成功后，TensorField 的持久化描述引用 ArtifactRef。
6. Preview 由 Data Tensor 派生，拥有独立 provenance 和校验和。

## 17. 稳定错误

模型与访问层至少定义：

- `SCHEMA_VERSION_UNSUPPORTED`
- `MODEL_VALIDATION_FAILED`
- `AXIS_SHAPE_MISMATCH`
- `CHANNEL_COUNT_MISMATCH`
- `COORDINATE_SPACE_MISMATCH`
- `MAPPING_NOT_INVERTIBLE`
- `TIME_SELECTION_INVALID`
- `TIMESTAMP_MISSING`
- `TIMELINE_GAP`
- `ARRAY_SELECTION_OUT_OF_RANGE`
- `MATERIALIZATION_LIMIT_EXCEEDED`
- `ARTIFACT_IDENTITY_MISMATCH`
- `MEDIA_CHANGED_DURING_ANALYSIS`

错误必须包含稳定 code、用户可读 message、相关对象 ID、字段路径和可选修复建议。

## 18. 兼容与版本策略

- 本规范 Schema 初始版本为 `0.1.0`。
- 同一主版本内允许新增可选字段和枚举值。
- 删除字段、改变默认语义或坐标数学必须提升 Schema 主版本。
- 读取器必须拒绝未知主版本；未知可选字段在同主版本中保留但忽略。
- 现有 `MediaInfo`、`SpacetimeResult`、`TimelineResult`、`TemporalReduceResult` 和 `FlowResult` 通过兼容适配器逐步转换，1.0 前不删除。

## 19. 测试要求

- Pydantic 模型生成 JSON Schema，并对本文有效/无效示例做自动测试。
- FramePacket 的显示顺序、PTS、归一化时间和像素身份使用确定性视频验证。
- 坐标变换组合、逆变换、像素中心和半开矩形使用属性测试。
- TemporalSelection 覆盖 CFR、VFR、非零起始时间、重复和缺失 PTS。
- AxisMapping 覆盖规则、不规则、区间、加权和组合映射。
- TensorField 覆盖 shape、轴、通道、有效性掩码和 dtype 不一致错误。
- ArrayHandle 对内存、NPY、memmap 和 Zarr 使用同一读取契约测试。
- 任意预览操作都不得改变源 Data Tensor 或其校验和。
