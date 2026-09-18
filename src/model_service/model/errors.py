class ModelLoadError(RuntimeError):
    """Raised when the configured artifact cannot become usable."""


class InputContractError(ValueError):
    """Raised when inference input violates the artifact contract."""


class InferenceError(RuntimeError):
    """Raised when the loaded runtime cannot complete inference."""


class NotReadyError(RuntimeError):
    """Raised when inference is requested before the model is ready."""
