from __future__ import annotations


class PipelineError(Exception):
    """Base error with a stable code safe to cross the MCP boundary."""

    code = "pipeline_error"

    def __init__(self, message: str = "Pipeline operation failed", *, diagnostics=None) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})

    def public_error(self) -> dict[str, object]:
        value: dict[str, object] = {"code": self.code, "message": str(self)}
        if self.diagnostics:
            value["diagnostics"] = self.diagnostics
        return value


class ValidationError(PipelineError):
    code = "validation_error"


class JobNotFoundError(PipelineError):
    code = "job_not_found"


class CandidateNotFoundError(PipelineError):
    code = "candidate_not_found"


class InvalidTransitionError(PipelineError):
    code = "invalid_transition"


class ProviderUnavailableError(PipelineError):
    code = "provider_unavailable"


class ProviderAuthenticationError(PipelineError):
    code = "provider_authentication_failed"


class ProviderRateLimitError(PipelineError):
    code = "provider_rate_limited"


class ProviderRequestError(PipelineError):
    code = "provider_request_failed"


class ProviderProtocolError(PipelineError):
    code = "provider_protocol_error"


class ProviderTaskError(PipelineError):
    code = "provider_task_failed"


class ProviderTimeoutError(PipelineError):
    code = "provider_timeout"


class ProviderDownloadError(PipelineError):
    code = "provider_download_failed"


class JobCancelledError(PipelineError):
    code = "job_cancelled"


class BridgeUnavailableError(PipelineError):
    code = "blender_bridge_unavailable"


class AuthenticationError(PipelineError):
    code = "authentication_failed"
