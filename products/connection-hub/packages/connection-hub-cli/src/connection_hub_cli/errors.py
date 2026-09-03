from __future__ import annotations


class ConnectionHubCliError(RuntimeError):
    """A user-facing failure whose text is safe to render."""

    def __init__(self, code: str, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code

    def __str__(self) -> str:
        return self.message


class StateError(ConnectionHubCliError):
    pass


class ProfileError(ConnectionHubCliError):
    pass


class CredentialError(ConnectionHubCliError):
    pass


class UpstreamError(ConnectionHubCliError):
    pass


class ClientConfigurationError(ConnectionHubCliError):
    pass


class HostControlError(ConnectionHubCliError):
    pass


class AuthorizationError(ConnectionHubCliError):
    pass
