# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Profile-device proof and recovery contracts."""

from connection_hub.delegated_credentials.devices.errors import DeviceCryptoError
from connection_hub.delegated_credentials.devices.keys import (
    DEVICE_KEY_CURVE,
    DeviceKeyPair,
    generate_device_key,
    jwk_thumbprint,
    public_jwk_from_private,
)
from connection_hub.delegated_credentials.devices.packages import (
    decrypt_device_package,
    encrypt_device_package,
)
from connection_hub.delegated_credentials.devices.proof import (
    DEVICE_PROOF_ALGORITHM,
    DEVICE_PROOF_TYPE,
    VerifiedDeviceProof,
    build_device_proof,
    normalize_proof_uri,
    verify_device_proof,
)

__all__ = [
    "DEVICE_KEY_CURVE",
    "DEVICE_PROOF_ALGORITHM",
    "DEVICE_PROOF_TYPE",
    "DeviceCryptoError",
    "DeviceKeyPair",
    "VerifiedDeviceProof",
    "build_device_proof",
    "decrypt_device_package",
    "encrypt_device_package",
    "generate_device_key",
    "jwk_thumbprint",
    "normalize_proof_uri",
    "public_jwk_from_private",
    "verify_device_proof",
]
