from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from moco.codex.rpc import JsonValue


class MocoError(RuntimeError):
    """Base class for stable moco runtime errors."""


class CodexError(MocoError):
    """Base error for Codex process and protocol failures."""


class CodexPromptError(CodexError):
    """A local Realtime prompt file could not be used safely."""


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
