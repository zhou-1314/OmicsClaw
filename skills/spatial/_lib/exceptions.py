"""Custom exception hierarchy for SpatialClaw spatial analysis."""


class SpatialClawError(Exception):
    """Base exception for all SpatialClaw errors."""


class DataError(SpatialClawError):
    """Input data is missing, corrupt, or in an unexpected format."""


class ParameterError(SpatialClawError):
    """Invalid or conflicting analysis parameters."""


class ProcessingError(SpatialClawError):
    """An analysis step failed during execution."""


class DependencyError(SpatialClawError):
    """A required optional dependency is not installed."""


class PreprocessingRequiredError(SpatialClawError):
    """Data has not been preprocessed; run spatial-preprocess first."""


class RScriptError(ProcessingError):
    """An R subprocess call failed."""

    def __init__(self, script: str, returncode: int, stderr: str = ""):
        self.script = script
        self.returncode = returncode
        self.stderr = stderr
        stderr_tail = stderr[-500:] if stderr else "(empty)"
        super().__init__(
            f"R script '{script}' failed (exit code {returncode}): {stderr_tail}"
        )


class RScriptTimeoutError(RScriptError):
    """R subprocess exceeded its timeout."""

    def __init__(self, script: str, timeout: int):
        self.timeout = timeout
        super().__init__(script, -1, f"Timed out after {timeout}s")
