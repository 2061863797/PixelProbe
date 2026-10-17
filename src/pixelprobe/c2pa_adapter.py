"""官方 C2PA SDK 的离线验证适配层；不推断媒体真伪。"""
from __future__ import annotations

from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
from typing import Any

MAX_TRUST_BYTES = 64 * 1024


def verify_c2pa(path: Path, *, trust_anchors_pem: str | None = None) -> dict[str, Any]:
    if trust_anchors_pem is not None:
        if not trust_anchors_pem.strip() or len(trust_anchors_pem.encode("utf-8")) > MAX_TRUST_BYTES:
            raise ValueError("C2PA 信任锚必须是非空且不超过 64 KiB 的 PEM")
    policy: dict[str, Any] = {
        "remote_manifest_fetch": False, "ocsp_fetch": False,
        "trust_source": "caller_anchors" if trust_anchors_pem is not None else "sdk_defaults",
        "trust_anchors_sha256": (
            sha256(trust_anchors_pem.encode("utf-8")).hexdigest()
            if trust_anchors_pem is not None else None
        ),
        "revocation_scope": "embedded_only_no_online_check",
    }
    try:
        from c2pa import Context, Reader, C2paError
    except ImportError:
        from pixelprobe.utils.optional_deps import install_hint
        return {"status": "unavailable", "verification_state": "not_checked",
                "reason": "未安装可选依赖 c2pa-python", "hint": install_hint("evidence"),
                "policy": policy}
    except (OSError, RuntimeError) as exc:
        return {"status": "unavailable", "verification_state": "not_checked",
                "reason": "C2PA 原生依赖无法加载", "error_type": type(exc).__name__, "policy": policy}
    try:
        sdk_version = version("c2pa-python")
    except Exception:
        sdk_version = "unknown"
    common = {"policy": policy, "sdk_version": sdk_version,
              "automatic_authenticity_conclusion": None}
    settings: dict[str, Any] = {"verify": {
        "remote_manifest_fetch": False, "ocsp_fetch": False,
        "verify_after_reading": True, "verify_trust": True,
        "verify_timestamp_trust": True,
    }}
    if trust_anchors_pem is not None:
        # 显式替换锚集合，不允许证书白名单绕过链验证。
        settings["trust"] = {"trust_anchors": trust_anchors_pem}
    try:
        if trust_anchors_pem is not None:
            from cryptography.x509 import load_pem_x509_certificates
            if not load_pem_x509_certificates(trust_anchors_pem.encode("utf-8")):
                raise ValueError("C2PA 信任锚中没有证书")
        with Context.from_dict(settings) as context:
            with Reader(str(path), context=context) as reader:
                store = json.loads(reader.json())
        return {**common, **summarize_store(store)}
    except C2paError.ManifestNotFound:
        return {**common, "status": "completed", "verification_state": "no_credentials",
                "active_manifest": None}
    except C2paError.RemoteManifest as exc:
        return {**common, "status": "unavailable", "verification_state": "not_checked",
                "reason": "远程凭证未获取；离线检查不能判定其存在性或有效性",
                "error_type": type(exc).__name__}
    except Exception as exc:
        # 不依据异常文本猜测无凭证，也不把可能含路径的异常文本写进报告。
        return {**common, "status": "failed", "verification_state": "verification_failed",
                "error_type": type(exc).__name__}


def summarize_store(store: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(store, dict):
        raise ValueError("C2PA 清单结构无效")
    label = store.get("active_manifest")
    manifests = store.get("manifests") or {}
    if not label and not manifests:
        if store.get("validation_state") == "Invalid" or store.get("validation_status"):
            raise ValueError("C2PA 清单缺失且存在验证错误")
        return {"status": "completed", "verification_state": "no_credentials", "active_manifest": None}
    if not isinstance(manifests, dict) or label not in manifests:
        raise ValueError("C2PA 活动清单引用缺失")
    active = manifests[label]
    results = store.get("validation_results") or {}
    active_results = results.get("activeManifest") or {}
    failures = active_results.get("failure") or []
    successes = active_results.get("success") or []
    sdk_state = store.get("validation_state")
    # 旧格式没有分组结果时，保守使用 validation_status；未知错误必须保留失败。
    legacy = store.get("validation_status") or active.get("validation_status") or []
    codes = {item["code"] for item in failures}
    if not active_results:
        codes.update(item["code"] for item in legacy)
    success_codes = {item["code"] for item in successes}
    hard_failures = codes - {"signingCredential.untrusted"}
    if sdk_state == "Invalid" or hard_failures:
        state = "verification_failed"
    elif sdk_state == "Trusted" and not codes and "claimSignature.validated" in success_codes:
        state = "trusted"
    else:
        state = "trust_unknown"
    return {
        "status": "completed", "verification_state": state,
        "sdk_validation_state": sdk_state,
        "active_manifest": str(label),
        "validation_status": legacy,
        "validation_results": results,
        "signature_validated": "claimSignature.validated" in success_codes,
        "trust_note": "信任状态仅针对当前验证策略；不代表媒体内容真实，未执行在线吊销查询",
        "claim_generator": active.get("claim_generator"),
        "claim_generator_info": active.get("claim_generator_info"),
        "signature_info": active.get("signature_info"),
        "title": active.get("title"),
        "assertion_count": len(active.get("assertions") or []),
        "ingredient_count": len(active.get("ingredients") or []),
        "manifest_count": len(manifests),
    }
