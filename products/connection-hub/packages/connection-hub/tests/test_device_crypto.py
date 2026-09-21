from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable

import pytest
from jwcrypto import jwk, jws

from connection_hub.delegated_credentials.devices import (
    DeviceCryptoError,
    build_device_proof,
    decrypt_device_package,
    encrypt_device_package,
    generate_device_key,
    jwk_thumbprint,
    normalize_proof_uri,
    public_jwk_from_private,
    verify_device_proof,
)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _json_segment(value: dict[str, object]) -> str:
    return _b64url(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    )


def _proof_claims() -> dict[str, object]:
    return {
        "htm": "POST",
        "htu": "https://example.test/token",
        "iat": 1000,
        "jti": "proof-1",
        "nonce": "nonce",
    }


def _signed_proof(
    private_jwk: str,
    *,
    header: dict[str, object],
) -> str:
    token = jws.JWS(
        json.dumps(
            _proof_claims(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    )
    token.allowed_algs = ["ES256"]
    token.add_signature(
        jwk.JWK(**json.loads(private_jwk)),
        protected=json.dumps(
            header,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        alg="ES256",
    )
    return token.serialize(compact=True)


def _replace_compact_header(
    compact: str,
    transform: Callable[[dict[str, object]], dict[str, object]],
) -> str:
    parts = compact.split(".")
    padded = parts[0] + "=" * (-len(parts[0]) % 4)
    header = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    parts[0] = _json_segment(transform(header))
    return ".".join(parts)


def test_generated_device_key_has_stable_public_thumbprint() -> None:
    pair = generate_device_key()

    assert json.loads(pair.private_jwk)["d"]
    assert "d" not in pair.public_jwk
    assert public_jwk_from_private(pair.private_jwk) == pair.public_jwk
    assert jwk_thumbprint(pair.public_jwk) == pair.thumbprint


def test_device_proof_binds_key_request_nonce_and_access_token() -> None:
    pair = generate_device_key()
    proof = build_device_proof(
        pair.private_jwk,
        method="post",
        uri="https://EXAMPLE.test:443/oauth/token?ignored=yes",
        nonce="server-nonce",
        access_token="kst1.synthetic",
        now=1000,
        jti="proof-1",
    )

    verified = verify_device_proof(
        proof,
        method="POST",
        uri="https://example.test/oauth/token",
        nonce="server-nonce",
        access_token="kst1.synthetic",
        expected_thumbprint=pair.thumbprint,
        now=1059,
    )

    assert verified.thumbprint == pair.thumbprint
    assert verified.jti == "proof-1"
    assert verified.uri == "https://example.test/oauth/token"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"method": "GET"}, "device_proof_method_mismatch"),
        ({"uri": "https://example.test/other"}, "device_proof_uri_mismatch"),
        ({"nonce": "other"}, "device_proof_nonce_mismatch"),
        ({"access_token": "other"}, "device_proof_access_token_mismatch"),
        ({"now": 1061}, "device_proof_iat_outside_window"),
    ],
)
def test_device_proof_refuses_changed_or_stale_request(
    change: dict[str, object], reason: str
) -> None:
    pair = generate_device_key()
    proof = build_device_proof(
        pair.private_jwk,
        method="POST",
        uri="https://example.test/recovery",
        nonce="nonce",
        access_token="access",
        now=1000,
    )
    arguments: dict[str, object] = {
        "method": "POST",
        "uri": "https://example.test/recovery",
        "nonce": "nonce",
        "access_token": "access",
        "now": 1000,
    }
    arguments.update(change)

    with pytest.raises(DeviceCryptoError) as captured:
        verify_device_proof(proof, **arguments)

    assert captured.value.reason == reason


def test_device_proof_refuses_a_different_expected_key() -> None:
    pair = generate_device_key()
    other = generate_device_key()
    proof = build_device_proof(
        pair.private_jwk,
        method="POST",
        uri="https://example.test/token",
        nonce="nonce",
        now=1000,
    )

    with pytest.raises(DeviceCryptoError) as captured:
        verify_device_proof(
            proof,
            method="POST",
            uri="https://example.test/token",
            nonce="nonce",
            expected_thumbprint=other.thumbprint,
            now=1000,
        )

    assert captured.value.reason == "device_proof_key_mismatch"


@pytest.mark.parametrize("algorithm", ["HS256", "none"])
def test_device_proof_refuses_algorithm_confusion(algorithm: str) -> None:
    pair = generate_device_key()
    protected = _json_segment(
        {
            "alg": algorithm,
            "jwk": dict(pair.public_jwk),
            "typ": "dpop+jwt",
        }
    )
    payload = _json_segment(_proof_claims())
    signing_input = f"{protected}.{payload}".encode("ascii")
    signature = (
        _b64url(
            hmac.new(
                json.dumps(dict(pair.public_jwk), sort_keys=True).encode("ascii"),
                signing_input,
                hashlib.sha256,
            ).digest()
        )
        if algorithm == "HS256"
        else ""
    )

    with pytest.raises(DeviceCryptoError) as captured:
        verify_device_proof(
            f"{protected}.{payload}.{signature}",
            method="POST",
            uri="https://example.test/token",
            nonce="nonce",
            now=1000,
        )

    assert captured.value.reason == "device_proof_header_invalid"


@pytest.mark.parametrize("extra", ["kid", "jku", "x5u"])
def test_device_proof_refuses_extra_protected_headers(extra: str) -> None:
    pair = generate_device_key()
    proof = _signed_proof(
        pair.private_jwk,
        header={
            "alg": "ES256",
            "jwk": dict(pair.public_jwk),
            "typ": "dpop+jwt",
            extra: "https://attacker.invalid/key",
        },
    )

    with pytest.raises(DeviceCryptoError) as captured:
        verify_device_proof(
            proof,
            method="POST",
            uri="https://example.test/token",
            nonce="nonce",
            now=1000,
        )

    assert captured.value.reason == "device_proof_header_invalid"


def test_device_proof_refuses_private_embedded_jwk() -> None:
    pair = generate_device_key()
    private_value = json.loads(pair.private_jwk)
    proof = _signed_proof(
        pair.private_jwk,
        header={"alg": "ES256", "jwk": private_value, "typ": "dpop+jwt"},
    )

    with pytest.raises(DeviceCryptoError) as captured:
        verify_device_proof(
            proof,
            method="POST",
            uri="https://example.test/token",
            nonce="nonce",
            now=1000,
        )

    assert captured.value.reason == "device_jwk_fields_invalid"


def test_device_proof_refuses_off_curve_embedded_jwk() -> None:
    pair = generate_device_key()
    proof = build_device_proof(
        pair.private_jwk,
        method="POST",
        uri="https://example.test/token",
        nonce="nonce",
        now=1000,
    )

    def off_curve(header: dict[str, object]) -> dict[str, object]:
        embedded = dict(header["jwk"])
        embedded["x"] = _b64url(bytes(32))
        embedded["y"] = _b64url(bytes(32))
        return {**header, "jwk": embedded}

    changed = _replace_compact_header(proof, off_curve)
    with pytest.raises(DeviceCryptoError) as captured:
        verify_device_proof(
            changed,
            method="POST",
            uri="https://example.test/token",
            nonce="nonce",
            now=1000,
        )

    assert captured.value.reason == "device_public_jwk_invalid"


def test_device_proof_refuses_non_ascii_access_token() -> None:
    pair = generate_device_key()

    with pytest.raises(DeviceCryptoError) as captured:
        build_device_proof(
            pair.private_jwk,
            method="POST",
            uri="https://example.test/token",
            nonce="nonce",
            access_token="not-ascii-\u00e9",
            now=1000,
        )

    assert captured.value.reason == "device_proof_access_token_invalid"


def test_device_package_is_encrypted_and_bound_to_delivery_context() -> None:
    pair = generate_device_key()
    bindings = {
        "access_id": "card-1",
        "card_revision": 7,
        "device_id": "device-1",
        "device_revision": 2,
        "package_id": "package-1",
    }
    payload = {
        "access_token": "secret-access",
        "refresh_token": "secret-refresh",
        "delivery_nonce": "delivery-1",
    }

    package = encrypt_device_package(
        payload,
        public_jwk=pair.public_jwk,
        bindings=bindings,
    )

    assert "secret-access" not in package
    assert (
        decrypt_device_package(
            package,
            private_jwk=pair.private_jwk,
            expected_bindings=bindings,
        )
        == payload
    )

    with pytest.raises(DeviceCryptoError) as changed:
        decrypt_device_package(
            package,
            private_jwk=pair.private_jwk,
            expected_bindings={**bindings, "card_revision": 8},
        )
    assert changed.value.reason == "device_package_binding_mismatch"

    with pytest.raises(DeviceCryptoError) as wrong_key:
        decrypt_device_package(
            package,
            private_jwk=generate_device_key().private_jwk,
            expected_bindings=bindings,
        )
    assert wrong_key.value.reason == "device_package_decryption_failed"


def test_device_package_refuses_off_curve_ephemeral_key_before_decryption() -> None:
    pair = generate_device_key()
    bindings = {"access_id": "card-1", "package_id": "package-1"}
    package = encrypt_device_package(
        {"refresh_token": "secret-refresh"},
        public_jwk=pair.public_jwk,
        bindings=bindings,
    )

    def off_curve(header: dict[str, object]) -> dict[str, object]:
        embedded = dict(header["epk"])
        embedded["x"] = _b64url(bytes(32))
        embedded["y"] = _b64url(bytes(32))
        return {**header, "epk": embedded}

    changed = _replace_compact_header(package, off_curve)
    with pytest.raises(DeviceCryptoError) as captured:
        decrypt_device_package(
            changed,
            private_jwk=pair.private_jwk,
            expected_bindings=bindings,
        )

    assert captured.value.reason == "device_public_jwk_invalid"


@pytest.mark.parametrize(
    "change",
    [
        {"alg": "dir"},
        {"enc": "A128GCM"},
        {"chb": {"access_id": "card-1", "package_id": "changed"}},
    ],
)
def test_device_package_refuses_changed_protected_header(
    change: dict[str, object],
) -> None:
    pair = generate_device_key()
    bindings = {"access_id": "card-1", "package_id": "package-1"}
    package = encrypt_device_package(
        {"refresh_token": "secret-refresh"},
        public_jwk=pair.public_jwk,
        bindings=bindings,
    )
    changed = _replace_compact_header(package, lambda header: {**header, **change})

    with pytest.raises(DeviceCryptoError) as captured:
        decrypt_device_package(
            changed,
            private_jwk=pair.private_jwk,
            expected_bindings=bindings,
        )

    assert captured.value.reason == "device_package_binding_mismatch"


def test_proof_uri_normalization_rejects_query_in_claim() -> None:
    assert normalize_proof_uri("https://Example.test:443") == "https://example.test/"
    with pytest.raises(DeviceCryptoError) as captured:
        normalize_proof_uri("https://example.test/path?query=yes", claim=True)
    assert captured.value.reason == "device_proof_uri_invalid"


def test_proof_uri_normalization_refuses_an_invalid_idna_hostname() -> None:
    with pytest.raises(DeviceCryptoError) as captured:
        normalize_proof_uri("https://\ud800.example.test/token")

    assert captured.value.reason == "device_proof_uri_invalid"


@pytest.mark.parametrize(
    ("payload", "bindings", "reason"),
    [
        (
            {"value": object()},
            {"package_id": "package-1"},
            "device_package_payload_invalid",
        ),
        (
            {"value": "ok"},
            {"value": object()},
            "device_package_bindings_invalid",
        ),
    ],
)
def test_device_package_refuses_non_json_values(
    payload: dict[str, object],
    bindings: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(DeviceCryptoError) as captured:
        encrypt_device_package(
            payload,
            public_jwk=generate_device_key().public_jwk,
            bindings=bindings,
        )

    assert captured.value.reason == reason
