from __future__ import annotations

import ctypes
from ctypes import (
    POINTER,
    byref,
    c_byte,
    c_char_p,
    c_int32,
    c_long,
    c_ubyte,
    c_uint32,
    c_ulong,
    c_void_p,
)
from ctypes.util import find_library
from typing import Protocol

from connection_hub_cli.user_presence.errors import UserPresenceError

KEYCHAIN_SERVICE = "tech.kdcube.connection-hub.human-presence"
MAX_PROTECTED_CREDENTIAL_BYTES = 16 * 1024

_SECURITY_PATH = "/System/Library/Frameworks/Security.framework/Security"
_CORE_FOUNDATION_PATH = (
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_UTF8 = 0x08000100
_USER_PRESENCE = 1 << 0

_ERR_SEC_SUCCESS = 0
_ERR_SEC_USER_CANCELED = -128
_ERR_SEC_NOT_AVAILABLE = -25291
_ERR_SEC_AUTH_FAILED = -25293
_ERR_SEC_DUPLICATE_ITEM = -25299
_ERR_SEC_ITEM_NOT_FOUND = -25300
_ERR_SEC_INTERACTION_NOT_ALLOWED = -25308
_ERR_SEC_INTERACTION_REQUIRED = -25315


class SecretLease:
    """A short-lived mutable secret buffer that wipes its Python copy."""

    __slots__ = ("_buffer", "_closed")

    def __init__(self, value: bytearray) -> None:
        self._buffer = value
        self._closed = False

    def __enter__(self) -> SecretLease:  # noqa: PYI034 - supports Python 3.10
        if self._closed:
            raise UserPresenceError(
                "protected_credential_unavailable",
                "The protected credential lease is no longer available.",
            )
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    def __repr__(self) -> str:
        return "<SecretLease redacted>"

    def __str__(self) -> str:
        return "<redacted>"

    def view(self) -> memoryview:
        if self._closed:
            raise UserPresenceError(
                "protected_credential_unavailable",
                "The protected credential lease is no longer available.",
            )
        return memoryview(self._buffer).toreadonly()

    def close(self) -> None:
        if self._closed:
            return
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        self._closed = True


class NativeMacOSKeychain(Protocol):
    def available(self) -> bool: ...

    def store(self, account: str, secret: bytearray) -> None: ...

    def read(self, account: str, *, prompt: str) -> SecretLease: ...

    def delete(self, account: str, *, prompt: str) -> bool: ...


def _status_error(status: int, *, action: str) -> UserPresenceError:
    if status == _ERR_SEC_USER_CANCELED:
        return UserPresenceError(
            "user_presence_cancelled",
            "The user cancelled system authentication.",
            native_status=status,
        )
    if status == _ERR_SEC_AUTH_FAILED:
        return UserPresenceError(
            "user_presence_authentication_failed",
            "System authentication failed.",
            native_status=status,
        )
    if status == _ERR_SEC_ITEM_NOT_FOUND:
        return UserPresenceError(
            "user_presence_item_missing",
            "The user-presence-protected credential does not exist.",
            native_status=status,
        )
    if status in {_ERR_SEC_INTERACTION_NOT_ALLOWED, _ERR_SEC_INTERACTION_REQUIRED}:
        return UserPresenceError(
            "user_presence_interaction_unavailable",
            "System authentication cannot be displayed in this session.",
            native_status=status,
        )
    if status == _ERR_SEC_NOT_AVAILABLE:
        return UserPresenceError(
            "user_presence_unavailable",
            "The macOS Keychain service is unavailable.",
            native_status=status,
        )
    if status == _ERR_SEC_DUPLICATE_ITEM:
        return UserPresenceError(
            "user_presence_item_exists",
            "A user-presence-protected credential already exists for this reference.",
            native_status=status,
        )
    return UserPresenceError(
        "user_presence_security_error",
        f"The macOS Security framework could not {action} the protected item.",
        native_status=status,
    )


def _copy_protected_data(core_foundation: ctypes.CDLL, reference: int) -> bytearray:
    length = int(core_foundation.CFDataGetLength(reference))
    if not 0 < length <= MAX_PROTECTED_CREDENTIAL_BYTES:
        raise UserPresenceError(
            "protected_credential_invalid",
            "The protected credential is invalid.",
        )
    pointer = core_foundation.CFDataGetBytePtr(reference)
    if not pointer:
        raise UserPresenceError(
            "protected_credential_invalid",
            "The protected credential is invalid.",
        )
    return bytearray(pointer[index] for index in range(length))


class _CFDictionary:
    def __init__(self, api: SecurityFrameworkKeychain) -> None:
        self._api = api
        key_callbacks = ctypes.addressof(
            c_byte.in_dll(api._core_foundation, "kCFTypeDictionaryKeyCallBacks")
        )
        value_callbacks = ctypes.addressof(
            c_byte.in_dll(api._core_foundation, "kCFTypeDictionaryValueCallBacks")
        )
        self._value = api._core_foundation.CFDictionaryCreateMutable(
            None, 0, key_callbacks, value_callbacks
        )
        if not self._value:
            raise UserPresenceError(
                "user_presence_unavailable",
                "CoreFoundation could not create a Keychain query.",
            )

    def __enter__(self) -> _CFDictionary:  # noqa: PYI034 - supports Python 3.10
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self._api._core_foundation.CFRelease(self._value)

    @property
    def value(self) -> int:
        return self._value

    def set_constant(self, key: str, value: str) -> None:
        self._api._core_foundation.CFDictionarySetValue(
            self._value,
            self._api._constant(self._api._security, key),
            self._api._constant(self._api._security, value),
        )

    def set_boolean(self, key: str, value: bool) -> None:
        boolean_name = "kCFBooleanTrue" if value else "kCFBooleanFalse"
        self._api._core_foundation.CFDictionarySetValue(
            self._value,
            self._api._constant(self._api._security, key),
            self._api._constant(self._api._core_foundation, boolean_name),
        )

    def set_reference(self, key: str, value: int) -> None:
        self._api._core_foundation.CFDictionarySetValue(
            self._value,
            self._api._constant(self._api._security, key),
            value,
        )

    def set_string(self, key: str, value: str) -> None:
        encoded = value.encode("utf-8")
        reference = self._api._core_foundation.CFStringCreateWithBytes(
            None, encoded, len(encoded), _UTF8, False
        )
        if not reference:
            raise UserPresenceError(
                "user_presence_unavailable",
                "CoreFoundation could not encode a Keychain query value.",
            )
        try:
            self.set_reference(key, reference)
        finally:
            self._api._core_foundation.CFRelease(reference)

    def set_data(self, key: str, value: bytearray) -> None:
        native = (c_ubyte * len(value)).from_buffer(value)
        reference = self._api._core_foundation.CFDataCreate(
            None, native, len(value)
        )
        if not reference:
            raise UserPresenceError(
                "user_presence_unavailable",
                "CoreFoundation could not encode protected credential data.",
            )
        try:
            self.set_reference(key, reference)
        finally:
            self._api._core_foundation.CFRelease(reference)


class SecurityFrameworkKeychain:
    """Minimal Security.framework binding for user-presence Keychain items."""

    def __init__(
        self,
        *,
        security_path: str | None = None,
        core_foundation_path: str | None = None,
    ) -> None:
        try:
            self._security = ctypes.CDLL(
                security_path or find_library("Security") or _SECURITY_PATH
            )
            self._core_foundation = ctypes.CDLL(
                core_foundation_path
                or find_library("CoreFoundation")
                or _CORE_FOUNDATION_PATH
            )
            self._configure_signatures()
            self._verify_constants()
        except (AttributeError, OSError, ValueError):
            raise UserPresenceError(
                "user_presence_unavailable",
                "The macOS Security framework user-presence API is unavailable.",
            ) from None

    @staticmethod
    def _constant(library: ctypes.CDLL, name: str) -> int:
        value = c_void_p.in_dll(library, name).value
        if not value:
            raise ValueError(f"native constant {name} is unavailable")
        return value

    def _verify_constants(self) -> None:
        for name in (
            "kSecClass",
            "kSecClassGenericPassword",
            "kSecAttrService",
            "kSecAttrAccount",
            "kSecAttrAccessControl",
            "kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly",
            "kSecValueData",
            "kSecReturnData",
            "kSecMatchLimit",
            "kSecMatchLimitOne",
            "kSecUseOperationPrompt",
            "kSecUseDataProtectionKeychain",
        ):
            self._constant(self._security, name)
        self._constant(self._core_foundation, "kCFBooleanTrue")

    def _configure_signatures(self) -> None:
        cf = self._core_foundation
        sec = self._security

        cf.CFDictionaryCreateMutable.argtypes = [
            c_void_p,
            c_long,
            c_void_p,
            c_void_p,
        ]
        cf.CFDictionaryCreateMutable.restype = c_void_p
        cf.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]
        cf.CFDictionarySetValue.restype = None
        cf.CFStringCreateWithBytes.argtypes = [
            c_void_p,
            c_char_p,
            c_long,
            c_uint32,
            ctypes.c_bool,
        ]
        cf.CFStringCreateWithBytes.restype = c_void_p
        cf.CFDataCreate.argtypes = [c_void_p, POINTER(c_ubyte), c_long]
        cf.CFDataCreate.restype = c_void_p
        cf.CFDataGetLength.argtypes = [c_void_p]
        cf.CFDataGetLength.restype = c_long
        cf.CFDataGetBytePtr.argtypes = [c_void_p]
        cf.CFDataGetBytePtr.restype = POINTER(c_ubyte)
        cf.CFErrorGetCode.argtypes = [c_void_p]
        cf.CFErrorGetCode.restype = c_long
        cf.CFRelease.argtypes = [c_void_p]
        cf.CFRelease.restype = None

        sec.SecAccessControlCreateWithFlags.argtypes = [
            c_void_p,
            c_void_p,
            c_ulong,
            POINTER(c_void_p),
        ]
        sec.SecAccessControlCreateWithFlags.restype = c_void_p
        sec.SecItemAdd.argtypes = [c_void_p, POINTER(c_void_p)]
        sec.SecItemAdd.restype = c_int32
        sec.SecItemCopyMatching.argtypes = [c_void_p, POINTER(c_void_p)]
        sec.SecItemCopyMatching.restype = c_int32
        sec.SecItemDelete.argtypes = [c_void_p]
        sec.SecItemDelete.restype = c_int32

    def available(self) -> bool:
        error = c_void_p()
        access_control = self._security.SecAccessControlCreateWithFlags(
            None,
            self._constant(
                self._security, "kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly"
            ),
            _USER_PRESENCE,
            byref(error),
        )
        if access_control:
            self._core_foundation.CFRelease(access_control)
        if error.value:
            self._core_foundation.CFRelease(error.value)
        return bool(access_control)

    def _access_control(self) -> int:
        error = c_void_p()
        access_control = self._security.SecAccessControlCreateWithFlags(
            None,
            self._constant(
                self._security, "kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly"
            ),
            _USER_PRESENCE,
            byref(error),
        )
        status = (
            int(self._core_foundation.CFErrorGetCode(error.value))
            if error.value
            else _ERR_SEC_NOT_AVAILABLE
        )
        if error.value:
            self._core_foundation.CFRelease(error.value)
        if not access_control:
            raise _status_error(status, action="create access control for")
        return access_control

    def _base_query(self, account: str) -> _CFDictionary:
        query = _CFDictionary(self)
        query.set_constant("kSecClass", "kSecClassGenericPassword")
        query.set_string("kSecAttrService", KEYCHAIN_SERVICE)
        query.set_string("kSecAttrAccount", account)
        query.set_boolean("kSecUseDataProtectionKeychain", True)
        return query

    def store(self, account: str, secret: bytearray) -> None:
        access_control = self._access_control()
        try:
            with self._base_query(account) as query:
                query.set_reference("kSecAttrAccessControl", access_control)
                query.set_data("kSecValueData", secret)
                status = int(self._security.SecItemAdd(query.value, None))
        finally:
            self._core_foundation.CFRelease(access_control)
        if status != _ERR_SEC_SUCCESS:
            raise _status_error(status, action="store")

    def read(self, account: str, *, prompt: str) -> SecretLease:
        result = c_void_p()
        with self._base_query(account) as query:
            query.set_constant("kSecMatchLimit", "kSecMatchLimitOne")
            query.set_boolean("kSecReturnData", True)
            query.set_string("kSecUseOperationPrompt", prompt)
            status = int(
                self._security.SecItemCopyMatching(query.value, byref(result))
            )
        if status != _ERR_SEC_SUCCESS:
            if result.value:
                self._core_foundation.CFRelease(result.value)
            raise _status_error(status, action="read")
        if not result.value:
            raise UserPresenceError(
                "user_presence_security_error",
                "The macOS Security framework returned no protected credential data.",
            )
        try:
            value = _copy_protected_data(self._core_foundation, result.value)
        finally:
            self._core_foundation.CFRelease(result.value)
        return SecretLease(value)

    def delete(self, account: str, *, prompt: str) -> bool:
        with self._base_query(account) as query:
            query.set_string("kSecUseOperationPrompt", prompt)
            status = int(self._security.SecItemDelete(query.value))
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return False
        if status != _ERR_SEC_SUCCESS:
            raise _status_error(status, action="delete")
        return True
