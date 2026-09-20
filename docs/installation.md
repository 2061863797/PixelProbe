# 安装 PixelProbe

## 推荐：Python wheel 或 uvx

正式发行只提供 PixelProbe 的 Python wheel 和源码包，不发布包含 PyAV、FFmpeg、
OpenCV、C2PA 等第三方原生库的独立可执行程序。MCP 用户优先使用
[uvx 固定版本配置](uvx.md)；普通 CLI 用户安装 Release 中的 wheel。

## 使用 Python 安装

需要 Python 3.11 或更高版本。下载 Release 中的 `.whl` 文件，然后执行：

```bash
python -m pip install ./pixelprobe-<版本>-py3-none-any.whl
pixelprobe --version
```

也可固定到发布标签直接从 GitHub 安装：

```bash
python -m pip install "pixelprobe @ git+https://github.com/2061863797/PixelProbe.git@v1.1.0"
```

按需安装可选能力：

```bash
# 光流分析
python -m pip install "pixelprobe[flow] @ git+https://github.com/2061863797/PixelProbe.git@v1.1.0"

# Zarr 分块存储
python -m pip install "pixelprobe[storage] @ git+https://github.com/2061863797/PixelProbe.git@v1.1.0"

# MCP 服务
python -m pip install "pixelprobe[mcp] @ git+https://github.com/2061863797/PixelProbe.git@v1.1.0"
```

完整安装可一次选择全部 extra：

```bash
python -m pip install "pixelprobe[mcp,flow,storage,evidence] @ git+https://github.com/2061863797/PixelProbe.git@v1.1.0"
```

## 从源码安装（开发者）

```bash
git clone https://github.com/2061863797/PixelProbe.git
cd PixelProbe
python -m pip install -e ".[dev]"
pytest
```

## 仅供本地使用的单文件构建

仓库保留 PyInstaller 配置和本地构建脚本，方便开发者在自己的机器和依赖环境中
生成私人使用的单文件程序。该流程不产生官方发行物：

```bash
python -m pip install -e ".[dev,mcp,flow,storage,evidence]" pyinstaller
python scripts/build_local_standalone.py --output build/local-pixelprobe
```

输出目录必须尚不存在，脚本不会覆盖已有文件。它会先构建到临时目录，依次运行
`--version`、`--help`、`doctor --json`，默认再运行冻结程序真实 CLI/MCP 本地验收；
全部成功后才原子提交输出目录。使用 `--skip-acceptance-tests` 可只执行启动冒烟。

输出包含：

- `pixelprobe` 或 `pixelprobe.exe`；
- `README.md`、`README_EN.md` 和 `LICENSE`；
- `build-info.json`，记录平台、Python、依赖版本和可执行文件 SHA256；
- `LOCAL-BUILD.txt`，明确其为本地私有构建。

本地产物会收集当前环境中的原生库。除非已经完成这些具体二进制的许可、通知和
对应源码交付审查，否则不要公开上传、发布或转发该可执行文件。

## 更新与卸载

- Python 安装：用新 wheel 执行 `python -m pip install --upgrade <wheel>`；用 `python -m pip uninstall pixelprobe` 卸载。
- 本地单文件：用新输出目录重新构建并切换路径；删除该目录即可卸载。

如果安装成功但终端提示找不到 `pixelprobe`，请重新打开终端，并确认 Python 的 Scripts 目录已加入 `PATH`。
