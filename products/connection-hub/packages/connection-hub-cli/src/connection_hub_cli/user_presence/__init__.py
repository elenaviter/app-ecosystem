from connection_hub_cli.user_presence.backends import (
    UnavailableUserPresenceBackend,
    UserPresenceBackend,
)
from connection_hub_cli.user_presence.contracts import (
    ApprovalRequest,
    ApprovalResult,
    canonical_request_digest,
    require_matching_approval,
)
from connection_hub_cli.user_presence.errors import UserPresenceError
from connection_hub_cli.user_presence.macos import (
    MACOS_USER_PRESENCE_MECHANISM,
    MacOSUserPresenceBackend,
)
from connection_hub_cli.user_presence.operations import HttpOperationResult

__all__ = [
    "MACOS_USER_PRESENCE_MECHANISM",
    "ApprovalRequest",
    "ApprovalResult",
    "HttpOperationResult",
    "MacOSUserPresenceBackend",
    "UnavailableUserPresenceBackend",
    "UserPresenceBackend",
    "UserPresenceError",
    "canonical_request_digest",
    "require_matching_approval",
]
