"""Bundle 与 Artifact 的稳定错误。"""

from pixelprobe.domain.errors import DomainError


class BundleError(DomainError):
    code = "BUNDLE_ERROR"


def _error(name: str, code: str) -> type[BundleError]:
    return type(name, (BundleError,), {"code": code})


BundleTargetExistsError = _error("BundleTargetExistsError", "BUNDLE_TARGET_EXISTS")
BundleWriteError = _error("BundleWriteError", "BUNDLE_WRITE_FAILED")
BundleCommitError = _error("BundleCommitError", "BUNDLE_COMMIT_FAILED")
BundleManifestMissingError = _error("BundleManifestMissingError", "BUNDLE_MANIFEST_MISSING")
BundleManifestInvalidError = _error("BundleManifestInvalidError", "BUNDLE_MANIFEST_INVALID")
BundleIncompleteError = _error("BundleIncompleteError", "BUNDLE_INCOMPLETE")
BundleSchemaUnsupportedError = _error("BundleSchemaUnsupportedError", "BUNDLE_SCHEMA_UNSUPPORTED")
BundlePathUnsafeError = _error("BundlePathUnsafeError", "BUNDLE_PATH_UNSAFE")
ArtifactFileMissingError = _error("ArtifactFileMissingError", "ARTIFACT_FILE_MISSING")
ArtifactChecksumMismatchError = _error(
    "ArtifactChecksumMismatchError", "ARTIFACT_CHECKSUM_MISMATCH"
)
ArtifactSchemaMismatchError = _error(
    "ArtifactSchemaMismatchError", "ARTIFACT_SCHEMA_MISMATCH"
)
ProvenanceGraphInvalidError = _error(
    "ProvenanceGraphInvalidError", "PROVENANCE_GRAPH_INVALID"
)
ZarrDependencyMissingError = _error(
    "ZarrDependencyMissingError", "ZARR_DEPENDENCY_MISSING"
)
