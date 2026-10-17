# 安装与接入

正式发行只提供 Python wheel 和源码包，不发布包含 PyAV、FFmpeg、OpenCV、C2PA 等第三方原生库的
独立程序。下文命令中的 `v2.1.1` 是当前版本标签，升级时换成新标签即可。

| 你要做什么 | 推荐方式 |
|---|---|
| 让 Claude Code / Codex 使用 PixelProbe | [插件](#插件) |
| 在其他支持 MCP 的客户端里使用 | [手动配置 MCP](#手动配置-mcp) |
| 在终端或脚本里使用命令行 | [命令行安装](#命令行安装) |
| 修改代码、跑测试 | [从源码安装](#从源码安装) |

## 插件

插件把 MCP 服务和 5 个场景 Skill 打包在一起，AI 助手会在合适的任务里自动使用：

| Skill | 用途 |
|---|---|
| `pixelprobe-core` | 通用打法：overview → find → look → measure，地址写法与结果编号 |
| `ui-recording` | 录屏与 UI：动画时长、卡顿掉帧、控件何时变化、截图对比、颜色与位置 |
| `long-video` | 长视频：建索引、镜头切换、黑场闪帧、相似画面、自适应切片 |
| `quality` | 质量与异常：模糊、块效应、冻结重复、闪烁、条纹、码率骤降 |
| `forensics` | 取证线索：EXIF、C2PA、ELA/噪声/边缘、视频码流结构，不判定真伪 |

前提：安装 [uv](https://docs.astral.sh/uv/)。插件用 `uvx` 下载并启动固定版本的 PixelProbe（含光流、Zarr、C2PA
全部能力），首次启动需要联网（约 30 秒），之后复用缓存（约 2 秒）。

**Claude Code**

```bash
claude plugin marketplace add 2061863797/PixelProbe
```

```bash
claude plugin install pixelprobe@pixelprobe
```

也可以在 Claude Code 里输入 `/plugin` 打开插件界面安装。安装时可填写两项设置，之后可在 `/config` 里修改：

- **允许分析的媒体目录**：多个目录 Windows 用分号、macOS/Linux 用冒号分隔；留空表示只允许当前项目目录。
- **媒体分析内存总预算（MB）**：留空按本机内存自动分配，一般不用填。

**Codex**

```bash
codex plugin marketplace add 2061863797/PixelProbe
```

```bash
codex plugin add pixelprobe@pixelprobe
```

Codex 下 MCP 服务名为 `pixelprobe-plugin`。Codex 插件没有设置项，允许目录为 MCP 服务的启动目录；
需要分析其他目录时改用[手动配置 MCP](#手动配置-mcp)，两者不要同时使用。

**已经手动配置过 PixelProbe MCP？** 删除手动配置，只保留插件，否则会出现两套工具。
Codex 插件的服务名特意用 `pixelprobe-plugin`，因为同名时手动配置会把插件的服务整个遮住。

## 手动配置 MCP

在支持 MCP 的客户端里添加本地服务器，由 uvx 管理隔离环境（uvx 不是安全沙箱，也不会消除原生依赖）：

```json
{
  "mcpServers": {
    "pixelprobe": {
      "command": "uvx",
      "args": [
        "--python", "3.12",
        "--from", "pixelprobe[mcp,flow,storage,evidence] @ git+https://github.com/2061863797/PixelProbe.git@v2.1.1",
        "pixelprobe-mcp"
      ],
      "env": {"PIXELPROBE_MCP_ROOTS": "C:/Media"}
    }
  }
}
```

- 把允许目录改成你的媒体目录；多个目录 Windows 用分号、macOS/Linux 用冒号分隔。服务拒绝读取其他路径。
- 上面装的是全部能力（光流、Zarr、C2PA）；只需要基础功能时可改为 `[mcp]`，首次下载更小，但光流、Zarr 和 C2PA 验证不可用。
- 内存默认按本机自动分配，详见[资源控制](resource-control.md)。

连接客户端前可先用同一 `--from` 预热环境：

```bash
uvx --python 3.12 --from "pixelprobe[mcp,flow,storage,evidence] @ git+https://github.com/2061863797/PixelProbe.git@v2.1.1" pixelprobe --version
```

预热成功只说明环境能启动，仍须在客户端里实际调用一次确认 MCP 连接正常。

## 命令行安装

需要 Python 3.11 或更高版本。固定到发布标签安装全部能力：

```bash
python -m pip install "pixelprobe[mcp,flow,storage,evidence] @ git+https://github.com/2061863797/PixelProbe.git@v2.1.1"
pixelprobe --version
```

也可以下载 [Release](https://github.com/2061863797/PixelProbe/releases) 中的 wheel 安装：
`python -m pip install ./pixelprobe-<版本>-py3-none-any.whl`。可选能力按需选择：

| extra | 能力 |
|---|---|
| `mcp` | MCP 服务 |
| `flow` | 光流分析（OpenCV） |
| `storage` | Zarr 分块存储 |
| `evidence` | C2PA 签名验证 |

如果安装成功但终端提示找不到 `pixelprobe`，请重新打开终端，并确认 Python 的 Scripts 目录已加入 `PATH`。

## 从源码安装

```bash
git clone https://github.com/2061863797/PixelProbe.git
cd PixelProbe
python -m pip install -e ".[dev,mcp,flow,storage,evidence]"
python -m pytest
```

### 目录配置与诊断

```bash
pixelprobe doctor --json --allow-root C:/Media
pixelprobe --mcp --allow-root C:/Media --artifact-root C:/Media/results
pixelprobe --mcp --allow-root C:/Media --workers 2 --queue-size 16
```

`--allow-root` 可重复指定，显式参数优先于环境变量；结果目录必须位于允许目录内。`doctor` 检查依赖能否导入、
目录配置和磁盘空间；`healthy` 表示核心依赖与配置正常，可选依赖缺失会在 checks 中显示为 unavailable。

### 仅供本地使用的单文件程序

开发者可在自己的环境里生成私人使用的单文件程序，它不是官方发行物：

```bash
python -m pip install -e ".[dev,mcp,flow,storage,evidence]" pyinstaller
python scripts/build_local_standalone.py --output build/local-pixelprobe
```

输出目录必须尚不存在，脚本不会覆盖已有文件；它先依次运行 `--version`、`--help`、`doctor --json`，
默认再运行冻结程序的真实 CLI/MCP 验收（`--skip-acceptance-tests` 只做启动冒烟），全部成功后才提交输出目录。
输出包含程序、README、LICENSE、记录依赖版本与哈希的 `build-info.json`，以及标明本地私有构建的 `LOCAL-BUILD.txt`。
产物会收集当前环境的原生库，完成许可审查前不要公开上传或转发。

## 更新、回滚与卸载

| 方式 | 更新 | 卸载 |
|---|---|---|
| 插件 | `claude plugin update pixelprobe@pixelprobe` 或 `codex plugin marketplace upgrade pixelprobe` 后重新安装 | `claude plugin uninstall pixelprobe@pixelprobe` |
| 手动 MCP | 预热新标签后修改配置中的标签并重启客户端；保留旧配置以便回滚 | 删除该服务器配置 |
| 命令行 | 用新标签或新 wheel 执行 `python -m pip install --upgrade ...` | `python -m pip uninstall pixelprobe` |
| 本地单文件 | 用新输出目录重新构建并切换路径 | 删除该目录 |

更新后必须重启 MCP 服务进程才会加载新版本，可调用 `pixelprobe_system`（action=capabilities）确认 `pixelprobe_version`。
不要删除整个 uv 缓存或覆盖运行中的程序。
