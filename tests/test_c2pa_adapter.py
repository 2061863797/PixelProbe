import json
import sys
from types import SimpleNamespace

import pytest

from pixelprobe.c2pa_adapter import summarize_store, verify_c2pa


def store(state="Valid", failure=(), success=("claimSignature.validated",)):
    return {"active_manifest": "a", "manifests": {"a": {}},
            "validation_state": state,
            "validation_results": {"activeManifest": {
                "failure": [{"code": code} for code in failure],
                "success": [{"code": code} for code in success],
                "informational": [{"code": "timeStamp.untrusted"}],
            }}}


@pytest.mark.parametrize("state,failures,expected", [
    ("Valid", ["signingCredential.untrusted"], "trust_unknown"),
    ("Trusted", [], "trusted"),
    ("Trusted", ["assertion.dataHash.mismatch"], "verification_failed"),
    ("Invalid", [], "verification_failed"),
    ("Valid", ["unknown.failure"], "verification_failed"),
    ("FutureState", [], "trust_unknown"),
])
def test_structured_status_classification(state, failures, expected):
    assert summarize_store(store(state, failures))["verification_state"] == expected


def test_trusted_requires_signature_success():
    assert summarize_store(store("Trusted", success=()))["verification_state"] == "trust_unknown"


def test_missing_active_manifest_is_not_absent():
    with pytest.raises(ValueError):
        summarize_store({"active_manifest": "missing", "manifests": {"other": {}}})


def test_invalid_empty_store_is_not_absent():
    with pytest.raises(ValueError):
        summarize_store({"validation_state": "Invalid"})


def test_native_import_failure_is_explicit(monkeypatch, tmp_path):
    import builtins
    original = builtins.__import__
    def broken_import(name, *args, **kwargs):
        if name == "c2pa":
            raise OSError("private DLL path")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", broken_import)
    result = verify_c2pa(tmp_path / "input.jpg")
    assert result["status"] == "unavailable"
    assert result["verification_state"] == "not_checked"
    assert "private DLL path" not in json.dumps(result)


@pytest.mark.parametrize("kind,expected", [
    ("absent", "no_credentials"), ("remote", "not_checked"),
    ("other", "verification_failed"), ("ok", "trusted"),
])
def test_offline_settings_and_typed_errors(monkeypatch, tmp_path, kind, expected):
    class Missing(Exception):
        pass
    class Remote(Exception):
        pass
    class Context:
        @classmethod
        def from_dict(cls, settings):
            assert settings["verify"] == {
                "remote_manifest_fetch": False, "ocsp_fetch": False,
                "verify_after_reading": True, "verify_trust": True,
                "verify_timestamp_trust": True,
            }
            return cls()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class Reader(Context):
        def __init__(self, *args, **kwargs):
            errors = {"absent": Missing, "remote": Remote, "other": RuntimeError}
            if kind in errors:
                raise errors[kind]("no manifest found secret-path")
        def json(self):
            return json.dumps(store("Trusted"))
    monkeypatch.setitem(sys.modules, "c2pa", SimpleNamespace(
        Context=Context, Reader=Reader,
        C2paError=SimpleNamespace(ManifestNotFound=Missing, RemoteManifest=Remote)))
    result = verify_c2pa(tmp_path / "input.jpg")
    assert result["verification_state"] == expected
    assert "secret-path" not in json.dumps(result)


@pytest.mark.parametrize("pem", ["", " ", "x" * 65537, "中" * 30000],
                         ids=["empty", "blank", "oversize-ascii", "oversize-utf8"])
def test_anchor_byte_limit(pem, tmp_path):
    with pytest.raises(ValueError):
        verify_c2pa(tmp_path / "input.jpg", trust_anchors_pem=pem)
