# C2PA 离线验证

PixelProbe 使用官方 c2pa-python 0.37.10 系列完成清单、签名与证书链验证。
安装 evidence extra 后，现有 evidence CLI、Python API 与 MCP 工具自动执行该步骤。
未安装时仍明确返回 unavailable / not_checked，不影响其他证据分析。

## 结果语义

| verification_state | 含义 |
| --- | --- |
| no_credentials | SDK 明确报告没有内嵌凭证；不代表媒体真实或伪造 |
| trust_unknown | 没有发现明确完整性失败，但未证明签名方受当前策略信任；检查 signature_validated 和 SDK 状态 |
| trusted | SDK 返回 Trusted，活动清单签名验证成功且没有活动清单失败项 |
| verification_failed | 内容绑定、签名、证书、清单结构或验证配置失败；保留结构化失败码 |
| not_checked | 缺少依赖，或离线策略阻止取得远程凭证，未完成检查 |

报告包含 SDK 版本、原始 SDK 状态、活动清单的结构化验证结果、签名信息、
清单数量和信任策略。材料清单的结果保留在 validation_results 内；
材料的失败不能直接当作活动清单签名失败。报告经过现有长度与深度限制，
不包含证书私钥或异常中的本地路径。

remote_manifest_fetch 和 ocsp_fetch 均显式关闭，签名、证书信任与时间戳信任验证保持开启。
验证可使用嵌入的吊销信息，不表示已查询最新在线吊销状态。
Trusted 只表示在所记录策略下凭证验证通过，不能证明画面内容真实。

## 显式信任锚

默认使用安装版本 SDK 的默认信任集合，不宣称已配置业务或机构信任列表。
调用方可以提供 PEM 根证书集合，显式替换默认信任锚；不使用绕过链验证的 allowed_list。
集合最多 64 KiB，报告保存所传 PEM 的 SHA-256 和 caller_anchors 策略。
无效 PEM 作为 C2PA 单项验证失败记录，其他证据分析继续。

CLI：

```powershell
pixelprobe evidence "图片.jpg" --c2pa-trust-anchors "机构根证书.pem" --output "证据.bundle" --json
```

Python：

```python
from pathlib import Path
from pixelprobe.evidence import analyze_evidence

result = analyze_evidence(
    Path("图片.jpg"),
    output_path=Path("证据.bundle"),
    c2pa_trust_anchors_pem=Path("机构根证书.pem").read_text("utf-8"),
)
```

MCP pixelprobe_evidence 与 evidence 任务载荷支持相同的 c2pa_trust_anchors_pem
可选文本参数；不增加服务器任意文件读取入口。该证书集合由调用方指定，
不能把它形成的信任判断等同于公共或机构背书。

## 验证与发布边界

测试临时生成 RSA-PSS 签名证书及真实 JPEG，不保存生产私钥，不访问时间戳服务。
密码库独立验证签名回调输出，并由官方 SDK 验证最终媒体。
覆盖无凭证、未知信任、显式根证书信任、JPEG 内容篡改、清单篡改、
无效信任锚、中文路径 CLI、Bundle 完整性和真实 spawn 工作者。
SDK 0.37.10 的验证器要求测试签名证书包含组织字段；测试显式包含该字段，
没有通过忽略签名失败来让测试通过。

Linux Python 矩阵及三平台核心 CI 已配置真实 evidence 依赖。
Windows 本机运行结果记录在 optimization-progress.md；
Linux/macOS 的 Python/uvx C2PA 验证仍需在对应平台实际运行，不由 Windows 测试替代。
开发者本地冻结程序的验证结果只适用于该本地产物。

参考：[官方上下文与离线设置](https://github.com/contentauth/c2pa-python/blob/main/docs/context-settings.md)，
[官方清单结果与读取接口](https://github.com/contentauth/c2pa-python/blob/main/docs/working-stores.md)。
