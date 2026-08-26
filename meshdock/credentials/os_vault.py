from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from ctypes import wintypes
from typing import Protocol


class OsCredentialBackend(Protocol):
    @property
    def available(self) -> bool: ...
    def get(self, provider: str) -> str | None: ...
    def set(self, provider: str, secret: str) -> None: ...
    def delete(self, provider: str) -> None: ...


class UnavailableCredentialBackend:
    available = False

    def get(self, provider: str) -> str | None:
        return None

    def set(self, provider: str, secret: str) -> None:
        raise RuntimeError("OS credential storage is unavailable")

    def delete(self, provider: str) -> None:
        return


if os.name == "nt":
    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


class WindowsCredentialBackend:
    """Generic credentials stored under the current Windows user."""

    available = os.name == "nt"
    _TYPE_GENERIC = 1
    _PERSIST_LOCAL_MACHINE = 2
    _NOT_FOUND = 1168

    def __init__(self) -> None:
        if not self.available:
            raise RuntimeError("Windows Credential Manager is unavailable")
        self._api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        credential_pointer = ctypes.POINTER(_CREDENTIALW)
        self._api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(credential_pointer)]
        self._api.CredReadW.restype = wintypes.BOOL
        self._api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._api.CredWriteW.restype = wintypes.BOOL
        self._api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._api.CredDeleteW.restype = wintypes.BOOL
        self._api.CredFree.argtypes = [ctypes.c_void_p]
        self._api.CredFree.restype = None

    @staticmethod
    def _target(provider: str) -> str:
        return f"MeshDock/{provider}"

    @staticmethod
    def _legacy_target(provider: str) -> str:
        return f"AI3DAssetPipeline/{provider}"

    def _read(self, target: str) -> str | None:
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._api.CredReadW(target, self._TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == self._NOT_FOUND:
                return None
            raise OSError(error, "CredReadW failed")
        try:
            credential = pointer.contents
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return raw.decode("utf-8")
        finally:
            self._api.CredFree(pointer)

    def get(self, provider: str) -> str | None:
        return self._read(self._target(provider)) or self._read(self._legacy_target(provider))

    def set(self, provider: str, secret: str) -> None:
        raw = secret.encode("utf-8")
        if len(raw) > 2560:
            raise ValueError("credential is too large for Windows Credential Manager")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = _CREDENTIALW()
        credential.Type = self._TYPE_GENERIC
        credential.TargetName = self._target(provider)
        credential.CredentialBlobSize = len(raw)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self._PERSIST_LOCAL_MACHINE
        credential.UserName = "MeshDock"
        if not self._api.CredWriteW(ctypes.byref(credential), 0):
            error = ctypes.get_last_error()
            raise OSError(error, "CredWriteW failed")

    def delete(self, provider: str) -> None:
        for target in (self._target(provider), self._legacy_target(provider)):
            if not self._api.CredDeleteW(target, self._TYPE_GENERIC, 0):
                error = ctypes.get_last_error()
                if error != self._NOT_FOUND:
                    raise OSError(error, "CredDeleteW failed")


class MacOSKeychainCredentialBackend:
    """Generic passwords in the current user's login Keychain."""

    available = sys.platform == "darwin"
    _NOT_FOUND = -25300
    _SERVICE = b"MeshDock"
    _LEGACY_SERVICE = b"AI3DAssetPipeline"

    def __init__(self) -> None:
        if not self.available:
            raise RuntimeError("macOS Keychain is unavailable")
        self._security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        self._core = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        u32, pointer, chars = ctypes.c_uint32, ctypes.c_void_p, ctypes.c_char_p
        self._security.SecKeychainFindGenericPassword.argtypes = [
            pointer, u32, chars, u32, chars,
            ctypes.POINTER(u32), ctypes.POINTER(pointer), ctypes.POINTER(pointer),
        ]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainAddGenericPassword.argtypes = [
            pointer, u32, chars, u32, chars, u32, chars, ctypes.POINTER(pointer),
        ]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemModifyAttributesAndData.argtypes = [
            pointer, pointer, u32, chars,
        ]
        self._security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self._security.SecKeychainItemDelete.argtypes = [pointer]
        self._security.SecKeychainItemDelete.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._core.CFRelease.argtypes = [pointer]

    @staticmethod
    def _account(provider: str) -> bytes:
        return provider.encode("utf-8")

    def _find(self, provider: str, service: bytes | None = None):
        service = service or self._SERVICE
        account = self._account(provider)
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None, len(service), service, len(account), account,
            ctypes.byref(length), ctypes.byref(data), ctypes.byref(item),
        )
        return status, length, data, item

    def get(self, provider: str) -> str | None:
        for service in (self._SERVICE, self._LEGACY_SERVICE):
            status, length, data, item = self._find(provider, service)
            if status == self._NOT_FOUND:
                continue
            if status != 0:
                raise OSError(status, "Keychain lookup failed")
            try:
                return ctypes.string_at(data, length.value).decode("utf-8")
            finally:
                self._security.SecKeychainItemFreeContent(None, data)
                if item:
                    self._core.CFRelease(item)
        return None

    def set(self, provider: str, secret: str) -> None:
        raw = secret.encode("utf-8")
        status, _length, data, item = self._find(provider)
        if status == 0:
            try:
                result = self._security.SecKeychainItemModifyAttributesAndData(
                    item, None, len(raw), raw
                )
            finally:
                self._security.SecKeychainItemFreeContent(None, data)
                if item:
                    self._core.CFRelease(item)
        elif status == self._NOT_FOUND:
            account = self._account(provider)
            result = self._security.SecKeychainAddGenericPassword(
                None, len(self._SERVICE), self._SERVICE, len(account), account,
                len(raw), raw, None,
            )
        else:
            raise OSError(status, "Keychain lookup failed")
        if result != 0:
            raise OSError(result, "Keychain write failed")

    def delete(self, provider: str) -> None:
        for service in (self._SERVICE, self._LEGACY_SERVICE):
            status, _length, data, item = self._find(provider, service)
            if status == self._NOT_FOUND:
                continue
            if status != 0:
                raise OSError(status, "Keychain lookup failed")
            try:
                result = self._security.SecKeychainItemDelete(item)
            finally:
                self._security.SecKeychainItemFreeContent(None, data)
                if item:
                    self._core.CFRelease(item)
            if result != 0:
                raise OSError(result, "Keychain delete failed")


class LinuxSecretServiceCredentialBackend:
    """Freedesktop Secret Service via secret-tool; secrets travel only on stdin."""

    _SERVICE = "MeshDock"
    _LEGACY_SERVICE = "AI3DAssetPipeline"

    def __init__(self) -> None:
        self._tool = shutil.which("secret-tool")
        self.available = bool(self._tool)
        if not self.available:
            raise RuntimeError("Secret Service client is unavailable")

    def _run(self, args: list[str], *, secret: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [str(self._tool), *args], input=secret, text=True,
                capture_output=True, timeout=20, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Secret Service operation failed") from exc

    def get(self, provider: str) -> str | None:
        for service in (self._SERVICE, self._LEGACY_SERVICE):
            result = self._run(["lookup", "service", service, "provider", provider])
            if result.returncode == 0:
                value = result.stdout.rstrip("\r\n")
                if value:
                    return value
        return None

    def set(self, provider: str, secret: str) -> None:
        result = self._run(
            ["store", "--label", "MeshDock", "service", self._SERVICE,
             "provider", provider],
            secret=secret,
        )
        if result.returncode != 0:
            raise RuntimeError("Secret Service write failed")

    def delete(self, provider: str) -> None:
        for service in (self._SERVICE, self._LEGACY_SERVICE):
            result = self._run(["clear", "service", service, "provider", provider])
            if result.returncode not in {0, 1}:
                raise RuntimeError("Secret Service delete failed")


def default_os_backend() -> OsCredentialBackend:
    if os.name == "nt":
        try:
            return WindowsCredentialBackend()
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            return MacOSKeychainCredentialBackend()
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        try:
            return LinuxSecretServiceCredentialBackend()
        except Exception:
            pass
    return UnavailableCredentialBackend()
