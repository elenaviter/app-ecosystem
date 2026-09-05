from __future__ import annotations

from kdcube_cli.management.errors import ManagementCliError

ConnectionHubCliError = ManagementCliError


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


AuthorizationError = ManagementCliError
