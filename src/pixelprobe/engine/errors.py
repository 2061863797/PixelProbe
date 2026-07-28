"""执行、分块、checkpoint 与缓存的稳定错误。"""

from pixelprobe.domain.errors import DomainError


class EngineError(DomainError):
    code = "ENGINE_ERROR"


def _error(name: str, code: str) -> type[EngineError]:
    return type(name, (EngineError,), {"code": code})


ResourcePlanUnsatisfiableError = _error(
    "ResourcePlanUnsatisfiableError", "RESOURCE_PLAN_UNSATISFIABLE"
)
ChunkMappingMismatchError = _error(
    "ChunkMappingMismatchError", "CHUNK_MAPPING_MISMATCH"
)
CheckpointIncompatibleError = _error(
    "CheckpointIncompatibleError", "CHECKPOINT_INCOMPATIBLE"
)
ExecutionCancelledError = _error(
    "ExecutionCancelledError", "EXECUTION_CANCELLED"
)
ExecutionTimeoutError = _error("ExecutionTimeoutError", "EXECUTION_TIMEOUT")
PartialResultNotAllowedError = _error(
    "PartialResultNotAllowedError", "PARTIAL_RESULT_NOT_ALLOWED"
)
CacheEntryCorruptError = _error("CacheEntryCorruptError", "CACHE_ENTRY_CORRUPT")
OperatorNotRegisteredError = _error(
    "OperatorNotRegisteredError", "OPERATOR_NOT_REGISTERED"
)
OperatorConfigInvalidError = _error(
    "OperatorConfigInvalidError", "OPERATOR_CONFIG_INVALID"
)
OperatorTypeMismatchError = _error(
    "OperatorTypeMismatchError", "OPERATOR_TYPE_MISMATCH"
)
OperatorExecutionError = _error(
    "OperatorExecutionError", "OPERATOR_EXECUTION_FAILED"
)
