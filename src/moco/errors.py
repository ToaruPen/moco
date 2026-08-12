from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from moco.codex.rpc import JsonValue


class MocoError(RuntimeError):
    """Base class for stable moco runtime errors."""


class CodexError(MocoError):
    """Base error for Codex process and protocol failures."""


class CodexPromptError(CodexError):
    """A local Realtime prompt file could not be used safely."""


class CodexCommandError(CodexError):
    """The configured or discovered Codex command is unavailable."""


class CodexRpcError(CodexError):
    """A JSON-RPC server or protocol error."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        data: JsonValue = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class CodexRpcProtocolError(CodexRpcError):
    """A Codex JSON-RPC message violated the protocol contract."""

    def __init__(
        self,
        message: str,
        *,
        client_response_id: int | str | None = None,
        server_request_id: int | str | None = None,
    ) -> None:
        super().__init__(message)
        self.client_response_id = client_response_id
        self.server_request_id = server_request_id


class CodexRpcTimeoutError(CodexRpcError):
    """A Codex JSON-RPC operation exceeded its deadline."""

    def __init__(self, method: str, timeout: float) -> None:
        super().__init__(f"Codex RPC method {method!r} timed out after {timeout:g} seconds")
        self.method = method
        self.timeout = timeout


class CodexProcessExitedError(CodexRpcError):
    """The Codex app-server process exited or could not be started."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class CodexSchemaError(CodexError):
    """The installed Codex schema cannot satisfy a semantic contract."""


class CodexCapabilityError(CodexError):
    """Required Codex runtime capability discovery failed closed."""


class AgentTurnErrorCode(StrEnum):
    FAILED = "agent_turn_failed"
    INTERRUPTED = "agent_turn_interrupted"
    OUTCOME_UNKNOWN = "agent_outcome_unknown"


class CodexAgentError(CodexError):
    """A stable, payload-free Agent thread or turn operation failed."""

    def __init__(
        self,
        message: str,
        *,
        code: AgentTurnErrorCode | str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code


class HostPlatformError(MocoError):
    """The host cannot provide a required portable platform boundary."""


class PrivateStateError(MocoError):
    """The runtime state location does not satisfy the host security boundary."""


class CodexReviewError(CodexError):
    """A local approval review could not be published, decided, or completed."""
