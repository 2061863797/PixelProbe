# 安装 PixelProbe

## 推荐：下载独立 CLI

独立 CLI 不要求预先安装 Python。打开 [GitHub Releases](https://github.com/2061863797/PixelProbe/releases/latest)，按系统下载：

| 系统 | 文件 |
| --- | --- |
| Windows 64 位 | `pixelprobe-windows-x86_64.zip` |
| Linux 64 位 | `pixelprobe-linux-x86_64.tar.gz` |
| macOS Apple 芯片 | `pixelprobe-macos-arm64.tar.gz` |

解压后先在当前目录验证：

```powershell
# Windows PowerShell
.\pixelprobe.exe --version
.\pixelprobe.exe --help
```

```bash
# Linux / macOS
chmod +x pixelprobe
./pixelprobe --version
./pixelprobe --help
```

把可执行文件所在目录加入 `PATH` 后，就可以在任意目录使用 `pixelprobe`。下载页同时提供 `SHA256SUMS.txt`；可用 `Get-FileHash -Algorithm SHA256 <文件>`（Windows）或 `sha256sum <文件>`（Linux）核对。

独立 CLI 包含核心图片与视频分析能力。光流、Zarr 存储和 MCP 属于可选 Python 依赖，请使用下一节的 Python 安装方式。

## 使用 Python 安装

需要 Python 3.11 或更高版本。下载 Release 中的 `.whl` 文件，然后执行：

```bash
python -m pip install ./pixelprobe-<版本>-py3-none-any.whl
pixelprobe --version
```

也可固定到发布标签直接从 GitHub 安装：

```bash
python -m pip install "pixelprobe @ git+https://github.com/2061863797/PixelProbe.git@v1.0.2"
```

按需安装可选能力：

```bash
# 光流分析
python -m pip install "pixelprobe[flow] @ git+https://github.com/2061863797/PixelProbe.git@v1.0.2"

# Zarr 分块存储
python -m pip install "pixelprobe[storage] @ git+https://github.com/2061863797/PixelProbe.git@v1.0.2"

# MCP 服务
python -m pip install "pixelprobe[mcp] @ git+https://github.com/2061863797/PixelProbe.git@v1.0.2"
```

## 从源码安装（开发者）

```bash
git clone https://github.com/2061863797/PixelProbe.git
cd PixelProbe
python -m pip install -e ".[dev]"
pytest
```

## 更新与卸载

- 独立 CLI：下载新版本并替换原可执行文件；删除该文件即可卸载。
- Python 安装：用新 wheel 执行 `python -m pip install --upgrade <wheel>`；用 `python -m pip uninstall pixelprobe` 卸载。

如果安装成功但终端提示找不到 `pixelprobe`，请重新打开终端，并确认 Python 的 Scripts 目录或独立 CLI 所在目录已加入 `PATH`。
