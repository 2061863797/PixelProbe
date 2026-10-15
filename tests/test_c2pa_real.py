"""真实签名样本在测试中生成，不保存或使用生产私钥。"""
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image


@pytest.fixture
def signed_asset(tmp_path):
    c2pa = pytest.importorskip("c2pa")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

    now = datetime.now(timezone.utc)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PixelProbe test root")])
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PixelProbe test signer"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PixelProbe tests")])

    def certificate(subject, issuer, public_key, ca):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                .public_key(public_key).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=2))
                .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False, key_agreement=False,
                    key_cert_sign=ca, crl_sign=ca, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False))

    root = certificate(root_name, root_name, root_key.public_key(), True).sign(root_key, hashes.SHA256())
    leaf = (certificate(leaf_name, root_name, key.public_key(), False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
            .sign(root_key, hashes.SHA256()))
    pem = lambda cert: cert.public_bytes(serialization.Encoding.PEM).decode()
    root_pem = pem(root)
    settings = {
        "verify": {"remote_manifest_fetch": False, "ocsp_fetch": False},
        "trust": {"trust_anchors": root_pem},
        "builder": {"thumbnail": {"enabled": False}},
    }
    source = tmp_path / "原图.jpg"
    target = tmp_path / "签名.jpg"
    Image.new("RGB", (32, 32), (120, 80, 40)).save(source)
    def sign(data):
        signature = key.sign(data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        key.public_key().verify(signature, data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        return signature

    signer = c2pa.Signer.from_callback(sign, c2pa.C2paSigningAlg.PS256, pem(leaf) + root_pem)
    with signer, c2pa.Context.from_dict(settings) as context:
        with c2pa.Builder({"title": "test asset", "format": "image/jpeg",
                           "claim_generator_info": [{"name": "PixelProbe tests"}],
                           "assertions": [{"label": "c2pa.actions", "data": {"actions": [
                               {"action": "c2pa.created", "digitalSourceType":
                                "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"}]}}]}, context=context) as builder:
            with source.open("rb") as src, target.open("w+b") as dst:
                builder.sign(signer, "image/jpeg", src, dst)
    return target, source, root_pem


def test_real_sdk_signature_and_trust(signed_asset):
    from pixelprobe.c2pa_adapter import verify_c2pa
    target, source, root_pem = signed_asset
    unknown = verify_c2pa(target)
    assert unknown["verification_state"] == "trust_unknown"
    assert unknown["signature_validated"] is True
    assert unknown["sdk_validation_state"] == "Valid"
    trusted = verify_c2pa(target, trust_anchors_pem=root_pem)
    assert trusted["verification_state"] == "trusted"
    assert trusted["sdk_validation_state"] == "Trusted"
    assert trusted["policy"]["trust_anchors_sha256"]
    assert verify_c2pa(source)["verification_state"] == "no_credentials"


@pytest.mark.parametrize("change", ["content", "claim"])
def test_real_tampering_is_not_trust_unknown(signed_asset, change):
    from pixelprobe.c2pa_adapter import verify_c2pa
    target, _, root_pem = signed_asset
    data = bytearray(target.read_bytes())
    if change == "content":
        assert data[6:10] == b"JFIF"
        data[16] ^= 1  # 修改被签名的 JFIF 密度，保留可解码 JPEG。
        expected = "assertion.dataHash.mismatch"
    else:
        position = data.index(b"test asset")
        data[position] = ord("b")
        expected = "claimSignature.mismatch"
    target.write_bytes(data)
    result = verify_c2pa(target, trust_anchors_pem=root_pem)
    assert result["verification_state"] == "verification_failed"
    assert expected in {item["code"] for item in result["validation_results"]["activeManifest"]["failure"]}


def test_real_c2pa_cli_and_bundle(signed_asset, tmp_path):
    from conftest import run_json
    from pixelprobe.artifacts import BundleReader
    target, _, root_pem = signed_asset
    anchors = tmp_path / "信任锚.pem"
    anchors.write_text(root_pem, encoding="utf-8")
    output = tmp_path / "证据.bundle"
    payload = run_json("evidence", str(target), "--c2pa-trust-anchors", str(anchors),
                       "--output", str(output), "--json")
    assert payload["success"]
    assert payload["data"]["c2pa"]["verification_state"] == "trusted"
    assert payload["data"]["automatic_authenticity_conclusion"] is None
    BundleReader().open(output, verify="full")


def test_real_c2pa_spawn_worker(signed_asset, tmp_path):
    from pixelprobe_mcp.config import ServerConfig
    from pixelprobe_mcp.worker import run_operation_sync
    target, _, root_pem = signed_asset
    response, _ = run_operation_sync(
        ServerConfig((tmp_path,), tmp_path / "artifacts"), "capability",
        {"capability": "evidence", "media_path": str(target),
         "options": {"c2pa_trust_anchors_pem": root_pem}},
        timeout_seconds=30,
    )
    assert response.data["c2pa"]["verification_state"] == "trusted"


def test_bad_anchor_is_failed_not_default_trust(signed_asset):
    from pixelprobe.c2pa_adapter import verify_c2pa
    target, _, _ = signed_asset
    result = verify_c2pa(target, trust_anchors_pem="invalid PEM")
    assert result["verification_state"] == "verification_failed"
    assert result["policy"]["trust_source"] == "caller_anchors"
