from __future__ import annotations

from connection_hub.delegated_credentials.devices._authority_support import (
    ProfileDeviceAuthoritySupport,
)
from connection_hub.delegated_credentials.devices.enrollment_authority import (
    DEFAULT_ENROLLMENT_RECEIPT_TTL_SECONDS,
    EnrollmentAuthorityMixin,
)
from connection_hub.delegated_credentials.devices.package_authority import (
    PackageAuthorityMixin,
)
from connection_hub.delegated_credentials.devices.recovery_authority import (
    DEFAULT_RECOVERY_ATTEMPT_TTL_SECONDS,
    DEFAULT_REISSUE_PACKAGE_TTL_SECONDS,
    RecoveryAuthorityMixin,
)
from connection_hub.delegated_credentials.devices.status_authority import (
    CardStatusAuthorityMixin,
)


class PostgresProfileDeviceAuthority(
    EnrollmentAuthorityMixin,
    RecoveryAuthorityMixin,
    PackageAuthorityMixin,
    CardStatusAuthorityMixin,
    ProfileDeviceAuthoritySupport,
):
    """Transactional authority for profile keys and credential delivery."""


__all__ = [
    "DEFAULT_ENROLLMENT_RECEIPT_TTL_SECONDS",
    "DEFAULT_RECOVERY_ATTEMPT_TTL_SECONDS",
    "DEFAULT_REISSUE_PACKAGE_TTL_SECONDS",
    "PostgresProfileDeviceAuthority",
]
