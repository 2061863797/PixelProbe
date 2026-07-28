"""正式数值 Artifact 的数组访问入口。"""

from pixelprobe.artifacts.array_io import NpyArrayHandle, save_npy
from pixelprobe.artifacts.bundle import (
    BUNDLE_SCHEMA_VERSION,
    Bundle,
    BundleReader,
    BundleWriter,
    sha256_file,
    stable_id,
)
from pixelprobe.artifacts.models import ArtifactRecord, BundleManifest
from pixelprobe.artifacts.zarr_io import ZarrArrayHandle, save_zarr

__all__ = [
    "ArtifactRecord",
    "BUNDLE_SCHEMA_VERSION",
    "Bundle",
    "BundleManifest",
    "BundleReader",
    "BundleWriter",
    "NpyArrayHandle",
    "ZarrArrayHandle",
    "save_npy",
    "save_zarr",
    "sha256_file",
    "stable_id",
]
