# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Bound encrypted package delivery to enrolled profile devices."""

from __future__ import annotations

from typing import Any, Mapping

from jwcrypto import jwe, jwk

from connection_hub.delegated_credentials.devices._validation import (
    compact_json,
    json_object,
)
from connection_hub.delegated_credentials.devices.errors import DeviceCryptoError
from connection_hub.delegated_credentials.devices.keys import (
    parse_private_key,
    parse_public_jwk,
)

DEVICE_PACKAGE_ALGORITHM = "ECDH-ES"
DEVICE_PACKAGE_ENCRYPTION = "A256GCM"
MAX_PACKAGE_BYTES = 128 * 1024


def _package_header(bindings: Mapping[str, Any]) -> dict[str, Any]:
    clean = json_object(bindings, reason="device_package_bindings_invalid")
    if (
        not clean
        or len(
            compact_json(
                clean,
                reason="device_package_bindings_invalid",
            ).encode("utf-8")
        )
        > 8192
    ):
        raise DeviceCryptoError("device_package_bindings_invalid")
    return {
        "alg": DEVICE_PACKAGE_ALGORITHM,
        "chb": clean,
        "cty": "application/json",
        "enc": DEVICE_PACKAGE_ENCRYPTION,
        "typ": "connection-hub-reissue+jwe",
    }


def encrypt_device_package(
    payload: Mapping[str, Any],
    *,
    public_jwk: Any,
    bindings: Mapping[str, Any],
) -> str:
    """Encrypt one bounded package to a profile device's public key."""

    plaintext = compact_json(
        json_object(payload, reason="device_package_payload_invalid"),
        reason="device_package_payload_invalid",
    ).encode("utf-8")
    if not plaintext or len(plaintext) > MAX_PACKAGE_BYTES:
        raise DeviceCryptoError("device_package_payload_invalid")
    protected = _package_header(bindings)
    encrypted = jwe.JWE(
        plaintext=plaintext,
        protected=compact_json(protected),
        algs=[DEVICE_PACKAGE_ALGORITHM, DEVICE_PACKAGE_ENCRYPTION],
    )
    try:
        encrypted.add_recipient(jwk.JWK(**parse_public_jwk(public_jwk)))
        compact = encrypted.serialize(compact=True)
    except Exception as exc:  # noqa: BLE001
        raise DeviceCryptoError("device_package_encryption_failed") from exc
    if len(compact.encode("ascii")) > MAX_PACKAGE_BYTES * 2:
        raise DeviceCryptoError("device_package_too_large")
    return compact


def decrypt_device_package(
    package: str,
    *,
    private_jwk: Any,
    expected_bindings: Mapping[str, Any],
) -> dict[str, Any]:
    """Decrypt a package and require the exact protected delivery bindings."""

    compact = str(package or "").strip()
    if not compact or len(compact.encode("utf-8")) > MAX_PACKAGE_BYTES * 2:
        raise DeviceCryptoError("device_package_invalid")
    encrypted = jwe.JWE(
        algs=[DEVICE_PACKAGE_ALGORITHM, DEVICE_PACKAGE_ENCRYPTION]
    )
    try:
        encrypted.deserialize(compact)
        header = dict(encrypted.jose_header or {})
    except Exception as exc:  # noqa: BLE001
        raise DeviceCryptoError("device_package_invalid") from exc
    ephemeral_key = header.pop("epk", None)
    if ephemeral_key is None:
        raise DeviceCryptoError("device_package_header_invalid")
    parse_public_jwk(ephemeral_key)
    expected_header = _package_header(expected_bindings)
    if header != expected_header:
        raise DeviceCryptoError("device_package_binding_mismatch")
    try:
        encrypted.decrypt(parse_private_key(private_jwk))
        payload = json_object(
            encrypted.payload, reason="device_package_payload_invalid"
        )
    except DeviceCryptoError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DeviceCryptoError("device_package_decryption_failed") from exc
    if (
        len(
            compact_json(
                payload,
                reason="device_package_payload_invalid",
            ).encode("utf-8")
        )
        > MAX_PACKAGE_BYTES
    ):
        raise DeviceCryptoError("device_package_payload_invalid")
    return payload
