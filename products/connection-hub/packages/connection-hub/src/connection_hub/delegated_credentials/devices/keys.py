# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""P-256 profile-device key validation and generation."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto import jwk

from connection_hub.delegated_credentials.devices._validation import (
    b64url,
    compact_json,
    json_object,
)
from connection_hub.delegated_credentials.devices.errors import DeviceCryptoError

DEVICE_KEY_CURVE = "P-256"


@dataclass(frozen=True, slots=True)
class DeviceKeyPair:
    """One exportable key pair destined for native host custody."""

    private_jwk: str = field(repr=False)
    public_jwk: Mapping[str, str]
    thumbprint: str


def parse_public_jwk(value: Any) -> dict[str, str]:
    candidate = json_object(value, reason="device_jwk_invalid")
    required = {"kty", "crv", "x", "y"}
    if set(candidate) - {
        "kty",
        "crv",
        "x",
        "y",
        "alg",
        "kid",
        "use",
        "key_ops",
    }:
        raise DeviceCryptoError("device_jwk_fields_invalid")
    if not required.issubset(candidate) or "d" in candidate:
        raise DeviceCryptoError("device_public_jwk_invalid")
    if candidate.get("kty") != "EC" or candidate.get("crv") != DEVICE_KEY_CURVE:
        raise DeviceCryptoError("device_jwk_curve_unsupported")
    for name in required:
        if not isinstance(candidate.get(name), str) or not candidate[name]:
            raise DeviceCryptoError("device_public_jwk_invalid")
    try:
        coordinates: dict[str, int] = {}
        for name in ("x", "y"):
            encoded = candidate[name]
            if not encoded.isascii():
                raise ValueError("coordinate is not ASCII")
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if len(decoded) != 32 or b64url(decoded) != encoded:
                raise ValueError("coordinate is not canonical P-256 width")
            coordinates[name] = int.from_bytes(decoded, "big")
        ec.EllipticCurvePublicNumbers(
            coordinates["x"],
            coordinates["y"],
            ec.SECP256R1(),
        ).public_key()
        key = jwk.JWK(**candidate)
        exported = json_object(
            key.export(private_key=False), reason="device_public_jwk_invalid"
        )
    except Exception as exc:  # noqa: BLE001
        raise DeviceCryptoError("device_public_jwk_invalid") from exc
    return {name: str(exported[name]) for name in ("crv", "kty", "x", "y")}


def parse_private_key(value: Any) -> jwk.JWK:
    candidate = json_object(value, reason="device_private_jwk_invalid")
    if candidate.get("kty") != "EC" or candidate.get("crv") != DEVICE_KEY_CURVE:
        raise DeviceCryptoError("device_jwk_curve_unsupported")
    if not isinstance(candidate.get("d"), str) or not candidate["d"]:
        raise DeviceCryptoError("device_private_jwk_invalid")
    try:
        key = jwk.JWK(**candidate)
        parse_public_jwk(key.export(private_key=False))
        return key
    except DeviceCryptoError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DeviceCryptoError("device_private_jwk_invalid") from exc


def jwk_thumbprint(public_jwk: Any) -> str:
    """RFC 7638 SHA-256 thumbprint for one P-256 public JWK."""

    candidate = parse_public_jwk(public_jwk)
    canonical = compact_json(
        {
            "crv": candidate["crv"],
            "kty": candidate["kty"],
            "x": candidate["x"],
            "y": candidate["y"],
        }
    ).encode("ascii")
    return b64url(hashlib.sha256(canonical).digest())


def public_jwk_from_private(private_jwk: Any) -> dict[str, str]:
    key = parse_private_key(private_jwk)
    return parse_public_jwk(key.export(private_key=False))


def generate_device_key() -> DeviceKeyPair:
    key = jwk.JWK.generate(kty="EC", crv=DEVICE_KEY_CURVE)
    private_value = json_object(
        key.export(private_key=True), reason="device_private_jwk_invalid"
    )
    public_value = parse_public_jwk(key.export(private_key=False))
    return DeviceKeyPair(
        private_jwk=compact_json(private_value),
        public_jwk=public_value,
        thumbprint=jwk_thumbprint(public_value),
    )
