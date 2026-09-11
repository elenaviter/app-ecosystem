# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""An ``IdTokenVerifier`` on PyJWT and the issuer's JWKS.

PyJWT is not a dependency of this package; a host that already carries it
(KDCube does) uses this verifier, another host injects its own. The JWKS
client is PyJWT's, which caches keys and refetches on an unknown key id;
its blocking fetch runs in a worker thread.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from connection_hub.server_side_login.protocols import UpstreamRejected

DEFAULT_ALGORITHMS = ("RS256", "ES256")


class PyJwtVerifier:
    def __init__(
        self,
        jwks_uri: str,
        *,
        algorithms: tuple[str, ...] = DEFAULT_ALGORITHMS,
        leeway_seconds: int = 60,
        require_issuer: bool = True,
    ) -> None:
        try:
            import jwt  # noqa: F401 - presence check
            from jwt import PyJWKClient
        except ImportError as exc:  # pragma: no cover - depends on the host's environment
            raise RuntimeError("PyJwtVerifier needs PyJWT with the crypto extra installed") from exc
        if not str(jwks_uri or "").strip():
            raise ValueError("PyJwtVerifier needs the issuer's jwks_uri")
        self._jwks = PyJWKClient(jwks_uri, cache_keys=True)
        self._algorithms = tuple(algorithms)
        self._leeway = int(leeway_seconds)
        self._require_issuer = bool(require_issuer)

    def _decode(self, id_token: str, *, audience: str, issuer: str) -> dict[str, Any]:
        import jwt

        try:
            key = self._jwks.get_signing_key_from_jwt(id_token).key
            options = {"require": ["exp", "iat", "sub"], "verify_aud": True}
            kwargs: dict[str, Any] = {
                "algorithms": list(self._algorithms),
                "audience": audience,
                "leeway": self._leeway,
                "options": options,
            }
            if issuer and self._require_issuer:
                kwargs["issuer"] = issuer
            claims = jwt.decode(id_token, key, **kwargs)
        except jwt.PyJWTError as exc:
            raise UpstreamRejected("token_invalid", str(exc)) from exc
        return dict(claims)

    async def verify(self, id_token: str, *, audience: str, issuer: str = "") -> Mapping[str, Any]:
        return await asyncio.to_thread(self._decode, id_token, audience=audience, issuer=issuer)


__all__ = ["DEFAULT_ALGORITHMS", "PyJwtVerifier"]
