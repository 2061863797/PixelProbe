# Windows 原生依赖核对记录

日期：2026-09-20。对象为已验收的本地 1.0.3 候选，PyAV 18.1.0、
OpenCV 5.0.0.93、C2PA Python 0.37.10。Linux/macOS 未执行。

## 已验证的事实

- PyAV 18.1.0 官方 Windows wheel SHA256 为
  `ea1480b7a8d5405cb5f382b344731bf125fd2c1c6fae3964f6c48595628387ff`；
  本机安装的 252 个有哈希运行文件逐项符合该 wheel 的 RECORD。
  发布标签 `v18.1.0` 提交为 `7e3d950a...`，其配置固定使用
  `pyav-ffmpeg 8.1.2-1`（提交 `a71bf927...`）。Windows 原生发布包 SHA256
  为 `d00e5c5a...`；精确构建源码中 dav1d 为 1.5.3、SVT-AV1 为 4.1.0，
  不能使用 upstream main 的后续版本说明代替本次证据。
- C2PA Python 0.37.10 官方 Windows wheel SHA256 为
  `25ced040898ef3374e110b91e7d3754777950b5e992a87cde96937fa8ffa848a`；
  其运行时 DLL 与官方 wheel、`c2pa-rs c2pa-v0.90.19` Windows 发布包三方
  SHA256 均为 `8184c745...`。12 个安装运行文件符合 wheel RECORD；两个
  `.lib` 编译链接输入单独排除，不参与冻结程序运行文件核验。
- OpenCV 5.0.0.93 官方 Windows wheel SHA256 为
  `829717b6a95554f273e49e357cee3b3a2a26b6f4842fbc1bed2b45bdd8f87e0e`；
  45 个有哈希安装运行文件逐项符合 wheel RECORD。

- 直接调用实际 avcodec 和 avutil DLL 的 license/configuration 导出。
  两者自报 LGPL version 3 or later，构建启用 version3、libx264 和 libx265。
  当前 wheel 同时附带 libx264/libx265 DLL。没有仅依据 Python 包的 BSD 声明判断这些 DLL。
- 精确检查 `pyav-ffmpeg 8.1.2-1` 的 `patches/ffmpeg.patch`：补丁把
  libx264/libx265 从 FFmpeg 的 `EXTERNAL_LIBRARY_GPL_LIST` 移到
  `EXTERNAL_LIBRARY_VERSION3_LIST`。这解释了 DLL 为何在启用两库时仍自报 LGPLv3，
  但补丁的分类不是 x264/x265 许可或商业授权的证据。
- DLL SHA256 与 dependencies.json 的原生文件条目独立比对一致。
  FFmpeg 组件版本包括 avcodec 62.28.102、avutil 60.26.102、avformat 62.12.102。
- OpenCV 构建信息显示内部还含另一套 FFmpeg：
  avcodec 61.19.100、avformat 61.7.100、avutil 59.39.100。
  同时静态内嵌 JPEG/WebP/PNG/TIFF/OpenJPEG、IPP、Protobuf 等组件。
- C2PA 的 Python 层 MIT/Apache 许可已补齐；这不自动覆盖 c2pa_c.dll 内嵌的所有组件。

证据生成器为 scripts/build_release_inventory.py，Windows 上生成 native-evidence.json，
保存原生导出、版本和 OpenCV 构建信息；其他平台标记未执行。
本次证据位于 build/acceptance-windows/native-audit-20260920/。

## 不能据此作出的结论

DLL 的 license 字符串不是完整的再分发审核结论。
[FFmpeg 官方说明](https://ffmpeg.org/legal.html)要求考虑具体编译组件、
外部库及与二进制精确对应的源码，而非只看最外层包许可。
启用 x264/x265 与 DLL 自报 LGPL 的组合需要进一步核对对应上游构建和许可；
本记录不推断整个软件因此合法或违法，也不自动变更项目许可证。

wheel、发布标签、构建脚本和原生发布包的来源链现已精确记录；这解决的是来源
可追溯性，仍未解决所有再分发义务。后续仍需核对 x264/x265 与 FFmpeg 构建组合、
C2PA Cargo.lock 静态链接依赖的许可原文，以及 OpenCV 随包第三方通知的覆盖范围。
解除 PyAV 阻断需要可复核的 x264/x265 授权依据，或改为构建不含这两个库、
且不进行上述许可重分类的 FFmpeg；不能只把清单状态从 false 改为 true。

## 清单状态

dependencies.json 升至 schema_version=2：

- license_files_collected：Python 包附带/补充许可和 CPython 许可收集是否完成。
- redistribution_review_complete：当前明确为 false。

packaging/native-sources.json 保存三平台 wheel 和上游来源指纹，
packaging/release-constraints.txt 阻止本地审计时静默升级关键原生包。
正式工作流已经移除独立程序构建，只发布不含这些 DLL 的 PixelProbe Python
wheel 和源码包。若未来恢复独立版公开发行，清单生成器的
--require-redistribution-review 会在当前状态下拒绝该产物。

原有 schema_version=1 的 complete 只表示文件收集完成，不代表可以据此发布。
此前 ZIP 保留为本地功能验收候选，未上传或发布；没有重写原归档或更改其 SHA256。
