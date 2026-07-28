"""1.0 前保留的旧接口适配器。"""

from pixelprobe.compat.legacy_ranges import (
    legacy_frame_range_to_selection,
    selection_to_legacy_frame_range,
)
from pixelprobe.compat.legacy_results import (
    preview_image,
    spacetime_array,
    timeline_matrix,
)

__all__ = [
    "legacy_frame_range_to_selection",
    "preview_image",
    "selection_to_legacy_frame_range",
    "spacetime_array",
    "timeline_matrix",
]
