from __future__ import annotations


class ConnectionHubClientError(RuntimeError):
    """A fixed, user-safe failure of the caller side of Connection Hub.

    It carries a stable ``code``, a message safe to show, and the process
    ``exit_code`` a command line should use. It does not derive from any KDCube
    type: the caller layer runs without KDCube packages (W322).
    """

    def __init__(self, code: str, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = str(code or "connection_hub_error")
        self.message = str(message or "Connection Hub could not complete the request.")
        self.exit_code = int(exit_code)

    def __str__(self) -> str:
        return self.message


# The command line's historical name for the same base.
ConnectionHubCliError = ConnectionHubClientError


class StateError(ConnectionHubClientError):
    pass


class ProfileError(ConnectionHubClientError):
    pass


class CredentialError(ConnectionHubClientError):
    pass


class UpstreamError(ConnectionHubClientError):
    pass


class ClientConfigurationError(ConnectionHubClientError):
    pass


class HostControlError(ConnectionHubClientError):
    pass


AuthorizationError = ConnectionHubClientError
