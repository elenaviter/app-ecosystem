# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Device proof and encrypted-delivery errors."""


class DeviceCryptoError(ValueError):
    """A device key, proof, or encrypted package is invalid."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
