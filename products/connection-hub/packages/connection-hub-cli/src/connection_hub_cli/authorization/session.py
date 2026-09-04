from __future__ import annotations

import hashlib
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from app_foundation.secrets import NativeSecretError, NativeSecretValueStore
from filelock import AsyncFileLock, FileLock, Timeout
from keyring.backend import KeyringBackend

from connection_hub_cli.authorization.models import (
    OAuthTokenSet,
    validate_resource_identifier,
    validate_web_url,
)
from connection_hub_cli.errors import AuthorizationError, CredentialError
from connection_hub_cli.models import utc_now
from connection_hub_cli.state import AtomicJsonState

OAUTH_SESSION_KEYRING_SERVICE = "tech.kdcube.connection-hub.oauth-session"
OAUTH_PROFILE_KEYRING_SERVICE = "tech.kdcube.connection-hub.oauth-profile"
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_CREDENTIAL_REF_RE = re.compile(r"^[0-9a-f]{32}$")


def session_id_for_target(target_key: str) -> str:
    target = str(target_key or "").strip()
    if (
        not target
        or len(target) > 8192
        or any(ord(character) < 32 or ord(character) == 127 for character in target)
    ):
        raise AuthorizationError(
            "oauth_session_target_invalid",
            "The OAuth session target is invalid.",
        )
    return hashlib.sha256(target.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class OAuthSessionRecord:
    session_id: str
    target_key: str
    resource_metadata_url: str
    resource: str
    issuer: str
    token_endpoint: str
    revocation_endpoint: str | None
    client_id: str
    scope: str
    credential_ref: str
    access_id: str | None
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        *,
        target_key: str,
        resource_metadata_url: str,
        resource: str,
        issuer: str,
        token_endpoint: str,
        revocation_endpoint: str | None,
        client_id: str,
        scope: str,
        token: OAuthTokenSet,
        now: str | None = None,
    ) -> OAuthSessionRecord:
        timestamp = now or utc_now()
        record = cls(
            session_id=session_id_for_target(target_key),
            target_key=str(target_key).strip(),
            resource_metadata_url=validate_web_url(
                resource_metadata_url,
                code="oauth_resource_metadata_url_invalid",
            ),
            resource=validate_resource_identifier(resource),
            issuer=validate_web_url(
                issuer,
                code="oauth_authorization_server_invalid",
                allow_query=False,
            ).rstrip("/"),
            token_endpoint=validate_web_url(
                token_endpoint,
                code="oauth_token_endpoint_invalid",
            ),
            revocation_endpoint=(
                validate_web_url(
                    revocation_endpoint,
                    code="oauth_revocation_endpoint_invalid",
                )
                if revocation_endpoint
                else None
            ),
            client_id=_validated_client_id(client_id),
            scope=_validated_scope(token.scope or scope),
            credential_ref=secrets.token_hex(16),
            access_id=_required_access_id(token.access_id),
            created_at=timestamp,
            updated_at=timestamp,
        )
        record.verify()
        return record

    def with_token(
        self, token: OAuthTokenSet, *, now: str | None = None
    ) -> OAuthSessionRecord:
        access_id = _required_access_id(token.access_id or self.access_id)
        if access_id != self.access_id:
            raise AuthorizationError(
                "oauth_session_access_id_changed",
                "The refreshed OAuth credential belongs to a different caller card.",
            )
        return OAuthSessionRecord(
            session_id=self.session_id,
            target_key=self.target_key,
            resource_metadata_url=self.resource_metadata_url,
            resource=self.resource,
            issuer=self.issuer,
            token_endpoint=self.token_endpoint,
            revocation_endpoint=self.revocation_endpoint,
            client_id=self.client_id,
            scope=token.scope or self.scope,
            credential_ref=self.credential_ref,
            access_id=access_id,
            created_at=self.created_at,
            updated_at=now or utc_now(),
        )

    def verify(self) -> None:
        if not _SESSION_ID_RE.fullmatch(self.session_id):
            raise AuthorizationError(
                "oauth_session_record_invalid",
                "The stored OAuth session is invalid.",
            )
        if self.session_id != session_id_for_target(self.target_key):
            raise AuthorizationError(
                "oauth_session_record_invalid",
                "The stored OAuth session is invalid.",
            )
        if not _CREDENTIAL_REF_RE.fullmatch(self.credential_ref):
            raise AuthorizationError(
                "oauth_session_record_invalid",
                "The stored OAuth session is invalid.",
            )
        validate_web_url(
            self.resource_metadata_url,
            code="oauth_session_record_invalid",
        )
        validate_resource_identifier(self.resource)
        validate_web_url(
            self.issuer,
            code="oauth_session_record_invalid",
            allow_query=False,
        )
        validate_web_url(self.token_endpoint, code="oauth_session_record_invalid")
        if self.revocation_endpoint:
            validate_web_url(
                self.revocation_endpoint,
                code="oauth_session_record_invalid",
            )
        _validated_client_id(self.client_id)
        _validated_scope(self.scope)
        _required_access_id(self.access_id)

    def to_dict(self) -> dict[str, Any]:
        self.verify()
        return {
            "session_id": self.session_id,
            "target_key": self.target_key,
            "resource_metadata_url": self.resource_metadata_url,
            "resource": self.resource,
            "issuer": self.issuer,
            "token_endpoint": self.token_endpoint,
            "revocation_endpoint": self.revocation_endpoint,
            "client_id": self.client_id,
            "scope": self.scope,
            "credential_ref": self.credential_ref,
            "access_id": self.access_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OAuthSessionRecord:
        try:
            record = cls(
                session_id=str(value["session_id"]),
                target_key=str(value["target_key"]),
                resource_metadata_url=str(value["resource_metadata_url"]),
                resource=str(value["resource"]),
                issuer=str(value["issuer"]),
                token_endpoint=str(value["token_endpoint"]),
                revocation_endpoint=(
                    str(value["revocation_endpoint"])
                    if value.get("revocation_endpoint")
                    else None
                ),
                client_id=str(value["client_id"]),
                scope=str(value.get("scope") or ""),
                credential_ref=str(value["credential_ref"]),
                access_id=(str(value["access_id"]) if value.get("access_id") else None),
                created_at=str(value["created_at"]),
                updated_at=str(value["updated_at"]),
            )
            record.verify()
            return record
        except (KeyError, TypeError, ValueError, AuthorizationError) as exc:
            raise AuthorizationError(
                "oauth_session_record_invalid",
                "The stored OAuth session is invalid.",
            ) from exc


def _validated_client_id(value: str) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or len(candidate) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        raise AuthorizationError(
            "oauth_client_id_invalid",
            "The OAuth client identifier is invalid.",
        )
    return candidate


def _validated_scope(value: str) -> str:
    candidate = str(value or "").strip()
    if len(candidate) > 8192 or any(
        not 0x20 <= ord(character) <= 0x7E for character in candidate
    ):
        raise AuthorizationError(
            "oauth_scope_invalid",
            "The OAuth scope is invalid.",
        )
    return candidate


def _validated_access_id(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if (
        not candidate
        or len(candidate) > 256
        or any(character.isspace() or ord(character) < 32 for character in candidate)
    ):
        raise AuthorizationError(
            "oauth_access_id_invalid",
            "The delegated access identifier is invalid.",
        )
    return candidate


def _required_access_id(value: str | None) -> str:
    access_id = _validated_access_id(value)
    if access_id is None:
        raise AuthorizationError(
            "oauth_access_id_missing",
            "The OAuth credential is not bound to a delegated caller card.",
        )
    return access_id


class OAuthSessionStore:
    SCHEMA = "connection_hub_cli.oauth_sessions.v1"

    def __init__(self, path: Path) -> None:
        self.document = AtomicJsonState(
            path,
            schema=self.SCHEMA,
            collection="sessions",
        )

    @property
    def path(self) -> Path:
        return self.document.path

    def list(self) -> list[OAuthSessionRecord]:
        raw = self.document.read()["sessions"]
        return [OAuthSessionRecord.from_dict(raw[key]) for key in sorted(raw)]

    def get(self, session_id: str) -> OAuthSessionRecord | None:
        raw = self.document.read()["sessions"].get(session_id)
        return OAuthSessionRecord.from_dict(raw) if isinstance(raw, dict) else None

    def require(self, session_id: str) -> OAuthSessionRecord:
        record = self.get(session_id)
        if record is None:
            raise AuthorizationError(
                "oauth_session_not_found",
                "The OAuth session does not exist.",
            )
        return record

    def add(self, record: OAuthSessionRecord) -> None:
        def add_to(value: dict[str, Any]) -> None:
            sessions = value["sessions"]
            if record.session_id in sessions:
                raise AuthorizationError(
                    "oauth_session_exists",
                    "An OAuth session already exists for this target.",
                )
            sessions[record.session_id] = record.to_dict()

        self.document.mutate(add_to)

    def update(self, record: OAuthSessionRecord) -> None:
        def update_in(value: dict[str, Any]) -> None:
            sessions = value["sessions"]
            if record.session_id not in sessions:
                raise AuthorizationError(
                    "oauth_session_not_found",
                    "The OAuth session does not exist.",
                )
            sessions[record.session_id] = record.to_dict()

        self.document.mutate(update_in)

    def remove(self, session_id: str) -> OAuthSessionRecord:
        def remove_from(value: dict[str, Any]) -> OAuthSessionRecord:
            raw = value["sessions"].pop(session_id, None)
            if not isinstance(raw, dict):
                raise AuthorizationError(
                    "oauth_session_not_found",
                    "The OAuth session does not exist.",
                )
            return OAuthSessionRecord.from_dict(raw)

        return self.document.mutate(remove_from)


class OAuthSessionCredentialStore(Protocol):
    def put(self, credential_ref: str, token: OAuthTokenSet) -> None: ...

    def get(self, credential_ref: str) -> OAuthTokenSet | None: ...

    def remove(self, credential_ref: str) -> bool: ...


class UnavailableOAuthCredentialStore:
    """Deferred OAuth-store failure paired with an unavailable native backend."""

    def __init__(self, error: CredentialError, *, error_prefix: str) -> None:
        self._message = error.message
        self._exit_code = error.exit_code
        self._code = {
            "unsupported_credential_store": f"unsupported_{error_prefix}_store",
            "insecure_keyring_backend": f"insecure_{error_prefix}_store",
            "unavailable_keyring_backend": f"unavailable_{error_prefix}_store",
        }.get(error.code, f"{error_prefix}_store_failed")

    def put(self, _credential_ref: str, _token: OAuthTokenSet) -> None:
        self._raise()

    def get(self, _credential_ref: str) -> OAuthTokenSet | None:
        self._raise()

    def remove(self, _credential_ref: str) -> bool:
        self._raise()

    def _raise(self) -> None:
        raise AuthorizationError(
            self._code,
            self._message,
            exit_code=self._exit_code,
        ) from None


class NativeOAuthSessionCredentialStore:
    def __init__(
        self,
        *,
        backend: KeyringBackend | None = None,
        platform_name: str | None = None,
        enforce_native_backend: bool = True,
        service: str = OAUTH_SESSION_KEYRING_SERVICE,
        error_prefix: str = "oauth_session",
    ) -> None:
        self._error_prefix = error_prefix
        try:
            self._values = NativeSecretValueStore(
                service=service,
                backend=backend,
                platform_name=platform_name,
                enforce_native_backend=enforce_native_backend,
            )
        except NativeSecretError as exc:
            self._raise(exc)

    @property
    def platform_name(self) -> str:
        return self._values.platform_name

    @property
    def store_name(self) -> str:
        return self._values.store_name

    def backend_name(self) -> str:
        return self._values.backend_name()

    def recovery_hint(self) -> str:
        return self._values.recovery_hint()

    def put(self, credential_ref: str, token: OAuthTokenSet) -> None:
        _validate_credential_ref(credential_ref)
        value = token.to_secret_json()
        try:
            self._values.replace(credential_ref, value)
        except NativeSecretError as exc:
            self._raise(exc)

    def get(self, credential_ref: str) -> OAuthTokenSet | None:
        _validate_credential_ref(credential_ref)
        try:
            value = self._values.get(credential_ref)
        except NativeSecretError as exc:
            self._raise(exc)
        if value is None:
            return None
        try:
            return OAuthTokenSet.from_secret_json(value)
        except AuthorizationError:
            raise AuthorizationError(
                f"{self._error_prefix}_credential_invalid",
                "The stored OAuth credential is invalid.",
            ) from None

    def remove(self, credential_ref: str) -> bool:
        _validate_credential_ref(credential_ref)
        try:
            return self._values.remove(credential_ref)
        except NativeSecretError as exc:
            self._raise(exc)

    def _raise(self, exc: NativeSecretError) -> None:
        prefix = self._error_prefix
        code = {
            "unsupported_native_secret_platform": f"unsupported_{prefix}_store",
            "insecure_native_secret_backend": f"insecure_{prefix}_store",
            "unavailable_native_secret_backend": f"unavailable_{prefix}_store",
            "native_secret_write_failed": f"{prefix}_store_write_failed",
            "native_secret_read_failed": f"{prefix}_store_read_failed",
            "native_secret_delete_failed": f"{prefix}_store_delete_failed",
            "native_secret_cleanup_failed": f"{prefix}_store_delete_failed",
            "native_secret_corrupt": f"{prefix}_credential_invalid",
            "native_secret_verification_failed": f"{prefix}_store_probe_failed",
            "native_secret_rollback_failed": f"{prefix}_store_rollback_failed",
            "native_secret_too_large": f"{prefix}_credential_invalid",
            "native_secret_key_invalid": f"{prefix}_credential_ref_invalid",
        }.get(exc.code, f"{prefix}_store_failed")
        raise AuthorizationError(code, exc.message) from None


class NativeOAuthProfileCredentialStore(NativeOAuthSessionCredentialStore):
    """OAuth token sets for generic governed MCP profiles."""

    def __init__(
        self,
        *,
        backend: KeyringBackend | None = None,
        platform_name: str | None = None,
        enforce_native_backend: bool = True,
    ) -> None:
        super().__init__(
            backend=backend,
            platform_name=platform_name,
            enforce_native_backend=enforce_native_backend,
            service=OAUTH_PROFILE_KEYRING_SERVICE,
            error_prefix="oauth_profile",
        )


class MacOSOAuthSessionCredentialStore(NativeOAuthSessionCredentialStore):
    """Compatibility name for callers that explicitly require macOS."""

    def __init__(
        self,
        *,
        backend: KeyringBackend | None = None,
        platform_name: str | None = None,
        enforce_native_backend: bool = True,
    ) -> None:
        if platform_name is not None and platform_name != "Darwin":
            raise AuthorizationError(
                "unsupported_oauth_session_store",
                "This compatibility class requires macOS Keychain.",
            )
        super().__init__(
            backend=backend,
            platform_name="Darwin",
            enforce_native_backend=enforce_native_backend,
        )


def _validate_credential_ref(value: str) -> str:
    candidate = str(value or "").strip()
    if not _CREDENTIAL_REF_RE.fullmatch(candidate):
        raise AuthorizationError(
            "oauth_session_credential_ref_invalid",
            "The OAuth session credential reference is invalid.",
        )
    return candidate


class OAuthSessionRepository:
    def __init__(
        self,
        *,
        sessions: OAuthSessionStore,
        credentials: OAuthSessionCredentialStore,
    ) -> None:
        self._sessions = sessions
        self._credentials = credentials
        self._lock_path = sessions.path.with_suffix(
            f"{sessions.path.suffix}.transaction.lock"
        )

    @asynccontextmanager
    async def authorization_slot(self, target_key: str) -> AsyncIterator[None]:
        session_id = session_id_for_target(target_key)
        lock_path = self._sessions.path.with_suffix(
            f"{self._sessions.path.suffix}.{session_id}.authorize.lock"
        )
        self._prepare_lock(lock_path)
        lock = AsyncFileLock(str(lock_path), timeout=10, mode=0o600)
        try:
            async with lock:
                try:
                    os.chmod(lock_path, 0o600)
                except OSError:
                    raise AuthorizationError(
                        "oauth_authorization_lock_failed",
                        "Connection Hub cannot secure the authorization lock.",
                    ) from None
                if self._sessions.get(session_id) is not None:
                    raise AuthorizationError(
                        "oauth_session_exists",
                        "An OAuth session already exists for this target; disconnect it before authorizing again.",
                    )
                yield
        except Timeout:
            raise AuthorizationError(
                "oauth_authorization_in_progress",
                "Another browser authorization is already active for this target.",
            ) from None

    def verify_credential_store(self) -> None:
        credential_ref = secrets.token_hex(16)
        token = OAuthTokenSet(
            access_token=secrets.token_urlsafe(32),
            access_id=f"credential-store-probe-{secrets.token_hex(8)}",
        )
        self._credentials.put(credential_ref, token)
        try:
            if self._credentials.get(credential_ref) != token:
                raise AuthorizationError(
                    "oauth_session_store_probe_failed",
                    "The OAuth credential store did not return the disposable check value.",
                )
        finally:
            if not self._credentials.remove(credential_ref):
                raise AuthorizationError(
                    "oauth_session_store_probe_cleanup_failed",
                    "The disposable OAuth credential-store check could not be removed.",
                )

    def create(self, record: OAuthSessionRecord, token: OAuthTokenSet) -> None:
        with self._locked():
            if self._sessions.get(record.session_id) is not None:
                raise AuthorizationError(
                    "oauth_session_exists",
                    "An OAuth session already exists for this target.",
                )
            bound_token = self._token_for_record(record, token)
            self._credentials.put(record.credential_ref, bound_token)
            try:
                self._sessions.add(record)
            except Exception:
                self._credentials.remove(record.credential_ref)
                raise

    def load(self, session_id: str) -> tuple[OAuthSessionRecord, OAuthTokenSet]:
        with self._locked():
            return self._load_unlocked(session_id)

    def replace_token(
        self, session_id: str, token: OAuthTokenSet
    ) -> OAuthSessionRecord:
        with self._locked():
            record, previous = self._load_unlocked(session_id)
            replacement = self._token_for_record(record, token)
            return self._replace_unlocked(record, previous, replacement)

    async def refresh_if_expiring(
        self,
        session_id: str,
        *,
        refresher: Callable[
            [OAuthSessionRecord, OAuthTokenSet],
            Awaitable[OAuthTokenSet],
        ],
        now: int | None = None,
        leeway_seconds: int = 60,
    ) -> tuple[OAuthSessionRecord, OAuthTokenSet]:
        self._prepare_lock(self._lock_path)
        lock = AsyncFileLock(str(self._lock_path), timeout=10, mode=0o600)
        try:
            async with lock:
                try:
                    os.chmod(self._lock_path, 0o600)
                except OSError:
                    raise AuthorizationError(
                        "oauth_session_lock_failed",
                        "Connection Hub cannot secure the OAuth session lock.",
                    ) from None
                record, token = self._load_unlocked(session_id)
                if not token.is_expiring(now=now, leeway_seconds=leeway_seconds):
                    return record, token
                replacement = self._token_for_record(
                    record,
                    await refresher(record, token),
                )
                updated = self._replace_unlocked(record, token, replacement)
                return updated, replacement
        except Timeout:
            raise AuthorizationError(
                "oauth_session_lock_timeout",
                "Timed out waiting for the OAuth session lock.",
            ) from None

    def remove(self, session_id: str) -> OAuthSessionRecord:
        with self._locked():
            record = self._sessions.require(session_id)
            self._credentials.remove(record.credential_ref)
            return self._sessions.remove(session_id)

    def credential_present(self, session_id: str) -> bool:
        with self._locked():
            record = self._sessions.require(session_id)
            return self._credentials.get(record.credential_ref) is not None

    def _load_unlocked(
        self,
        session_id: str,
    ) -> tuple[OAuthSessionRecord, OAuthTokenSet]:
        record = self._sessions.require(session_id)
        token = self._credentials.get(record.credential_ref)
        if token is None:
            raise AuthorizationError(
                "oauth_session_credential_missing",
                "The OAuth session credential is missing.",
            )
        self._token_for_record(record, token, require_explicit=True)
        return record, token

    @staticmethod
    def _token_for_record(
        record: OAuthSessionRecord,
        token: OAuthTokenSet,
        *,
        require_explicit: bool = False,
    ) -> OAuthTokenSet:
        token_access_id = _validated_access_id(token.access_id)
        if token_access_id is None:
            if require_explicit:
                raise AuthorizationError(
                    "oauth_session_access_id_mismatch",
                    "The stored OAuth credential does not match its caller card.",
                )
            return replace(token, access_id=_required_access_id(record.access_id))
        if token_access_id != record.access_id:
            raise AuthorizationError(
                "oauth_session_access_id_mismatch",
                "The OAuth credential does not match its caller card.",
            )
        return token

    def _replace_unlocked(
        self,
        record: OAuthSessionRecord,
        previous: OAuthTokenSet,
        replacement: OAuthTokenSet,
    ) -> OAuthSessionRecord:
        updated = record.with_token(replacement)
        self._credentials.put(record.credential_ref, replacement)
        try:
            self._sessions.update(updated)
        except Exception:
            self._credentials.put(record.credential_ref, previous)
            raise
        return updated

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._prepare_lock(self._lock_path)
        lock = FileLock(str(self._lock_path), timeout=10, mode=0o600)
        try:
            lock.acquire()
        except Timeout:
            raise AuthorizationError(
                "oauth_session_lock_timeout",
                "Timed out waiting for the OAuth session lock.",
            ) from None
        except OSError:
            raise AuthorizationError(
                "oauth_session_lock_failed",
                "Connection Hub cannot open the OAuth session lock.",
            ) from None
        try:
            try:
                os.chmod(self._lock_path, 0o600)
            except OSError:
                raise AuthorizationError(
                    "oauth_session_lock_failed",
                    "Connection Hub cannot secure the OAuth session lock.",
                ) from None
            yield
        finally:
            lock.release()

    def _prepare_lock(self, lock_path: Path) -> None:
        self._sessions.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if lock_path.is_symlink():
            raise AuthorizationError(
                "oauth_session_lock_symlink_rejected",
                "Connection Hub refuses to use a symbolic link as a session lock.",
            )
        try:
            os.chmod(self._sessions.path.parent, 0o700)
        except OSError:
            raise AuthorizationError(
                "oauth_session_directory_permissions",
                "Connection Hub cannot secure its OAuth session directory.",
            ) from None
