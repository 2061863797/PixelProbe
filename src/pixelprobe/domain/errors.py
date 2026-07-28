"""领域模型与数组访问的稳定错误。"""

from __future__ import annotations


class DomainError(ValueError):
    """领域层错误基类，不依赖 CLI 或旧模型包。"""

    code = "DOMAIN_ERROR"

    def __init__(
        self,
        message: str,
        *,
        object_id: str | None = None,
        field_path: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.object_id = object_id
        self.field_path = field_path
        self.hint = hint

    def to_dict(self) -> dict[str, str]:
        data = {"code": self.code, "message": self.message}
        for name in ("object_id", "field_path", "hint"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return data


class AxisShapeMismatchError(DomainError):
    code = "AXIS_SHAPE_MISMATCH"


class ChannelCountMismatchError(DomainError):
    code = "CHANNEL_COUNT_MISMATCH"


class ArraySelectionOutOfRangeError(DomainError):
    code = "ARRAY_SELECTION_OUT_OF_RANGE"


class MaterializationLimitExceededError(DomainError):
    code = "MATERIALIZATION_LIMIT_EXCEEDED"


class SchemaVersionUnsupportedError(DomainError):
    code = "SCHEMA_VERSION_UNSUPPORTED"


class ModelValidationError(DomainError):
    code = "MODEL_VALIDATION_FAILED"


class CoordinateSpaceMismatchError(DomainError):
    code = "COORDINATE_SPACE_MISMATCH"


class MappingNotInvertibleError(DomainError):
    code = "MAPPING_NOT_INVERTIBLE"


class TimeSelectionInvalidError(DomainError):
    code = "TIME_SELECTION_INVALID"


class TimestampMissingError(DomainError):
    code = "TIMESTAMP_MISSING"


class TimelineGapError(DomainError):
    code = "TIMELINE_GAP"


class ArtifactIdentityMismatchError(DomainError):
    code = "ARTIFACT_IDENTITY_MISMATCH"


class MediaChangedDuringAnalysisError(DomainError):
    code = "MEDIA_CHANGED_DURING_ANALYSIS"
