"""Windows current-user DPAPI storage for personal Ed25519 key material.

DPAPI protects confidentiality and integrity at rest for the current Windows
user.  It is not hardware-backed and does not make a key non-exportable.  The
production provider always constructs the concrete PyCA Ed25519, native DPAPI,
and native ACL implementations; there is no injectable or plaintext fallback.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import threading
import time
from typing import Any, Iterator, Literal, Protocol, runtime_checkable

try:
    import msvcrt
except ImportError:  # pragma: no cover - imported only to report unavailable off Windows
    msvcrt = None  # type: ignore[assignment]

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.personal_policy_signing import (
    PersonalKeyProtection,
    PersonalKeyProviderAvailability,
    PersonalKeyProviderStatus,
    PersonalKeyRecoveryStatus,
    PersonalKeyRevocation,
    PersonalKeyRotation,
    PersonalPublicKeyDescriptor,
)
from app.services.personal_policy_signing import PersonalKeyProviderUnavailable


_MAX_PRIVATE_KEY_BYTES = 64 * 1024
_MAX_PROTECTED_BLOB_BYTES = 1024 * 1024
_MAX_RECORD_BYTES = 2 * 1024 * 1024
_DPAPI_CONTEXT = b"NimbusDesk/FRIDAY/personal-policy-key/v1"
_DPAPI_ENTROPY_SCHEMA = "friday-personal-policy-key-entropy/v2"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_LIFECYCLE_LOCK_TIMEOUT_SECONDS = 5.0
_LIFECYCLE_LOCK_NAME = ".personal-policy-key-lifecycle.lock"


class _DataBlob(ctypes.Structure):
    _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(wintypes.BYTE)))


@runtime_checkable
class Ed25519KeyBackend(Protocol):
    """Asymmetric primitive boundary; implementations require separate review."""

    @property
    def backend_id(self) -> str: ...

    def generate_key_pair(self) -> tuple[bytearray, str]: ...

    def public_key_from_private(self, private_key: bytearray) -> str: ...

    def sign(self, *, private_key: bytearray, payload: bytes) -> bytes: ...

    def verify(self, *, public_key_pem: str, payload: bytes, signature: bytes) -> bool: ...


class CryptographyEd25519Backend:
    """Reviewed-library Ed25519 primitive; private bytes remain memory-only."""

    backend_id = "PYCA_CRYPTOGRAPHY_ED25519"

    def __init__(self) -> None:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
                Ed25519PublicKey,
            )
        except Exception:
            raise PersonalKeyProviderUnavailable("Ed25519 cryptography backend is unavailable") from None
        self._serialization = serialization
        self._private_type = Ed25519PrivateKey
        self._public_type = Ed25519PublicKey

    def generate_key_pair(self) -> tuple[bytearray, str]:
        private = self._private_type.generate()
        private_bytes = bytearray(
            private.private_bytes(
                encoding=self._serialization.Encoding.Raw,
                format=self._serialization.PrivateFormat.Raw,
                encryption_algorithm=self._serialization.NoEncryption(),
            )
        )
        return private_bytes, self._public_pem(private.public_key())

    def public_key_from_private(self, private_key: bytearray) -> str:
        private = self._load_private(private_key)
        return self._public_pem(private.public_key())

    def sign(self, *, private_key: bytearray, payload: bytes) -> bytes:
        return self._load_private(private_key).sign(payload)

    def verify(self, *, public_key_pem: str, payload: bytes, signature: bytes) -> bool:
        try:
            public = self._serialization.load_pem_public_key(public_key_pem.encode("ascii"))
            if not isinstance(public, self._public_type):
                return False
            public.verify(signature, payload)
            return True
        except Exception:
            return False

    def _load_private(self, private_key: bytearray) -> Any:
        if len(private_key) != 32:
            raise PersonalKeyProviderUnavailable("Ed25519 private key material is invalid")
        try:
            return self._private_type.from_private_bytes(bytes(private_key))
        except Exception:
            raise PersonalKeyProviderUnavailable("Ed25519 private key material is invalid") from None

    def _public_pem(self, public_key: Any) -> str:
        return public_key.public_bytes(
            encoding=self._serialization.Encoding.PEM,
            format=self._serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")


class WindowsDpapiProtector:
    """Small native CryptProtectData/CryptUnprotectData wrapper with cleanup."""

    def __init__(self) -> None:
        if platform.system() != "Windows" or not hasattr(ctypes, "WinDLL"):
            raise PersonalKeyProviderUnavailable("Windows DPAPI is unavailable")
        try:
            self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except Exception:
            raise PersonalKeyProviderUnavailable("Windows DPAPI is unavailable") from None
        self._crypt32.CryptProtectData.argtypes = (
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = (
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    def protect(self, plaintext: bytearray, *, entropy: bytes) -> bytes:
        if not plaintext or len(plaintext) > _MAX_PRIVATE_KEY_BYTES:
            raise PersonalKeyProviderUnavailable("private key size is invalid")
        entropy_buffer = bytearray(entropy)
        if not entropy_buffer or len(entropy_buffer) > 1024:
            raise PersonalKeyProviderUnavailable("DPAPI entropy size is invalid")
        try:
            source_blob, source_ref = _blob_from_bytearray(plaintext)
            entropy_blob, entropy_ref = _blob_from_bytearray(entropy_buffer)
            output = _DataBlob()
            _ = source_ref, entropy_ref
            if not self._crypt32.CryptProtectData(
                ctypes.byref(source_blob),
                "NimbusDesk FRIDAY personal policy key",
                ctypes.byref(entropy_blob),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            ):
                error_code = ctypes.get_last_error()
                if output.pbData:
                    self._free_native(output, secret=False, suppress_errors=True)
                raise _native_failure("Windows DPAPI protection failed", error_code=error_code)
            return bytes(self._copy_and_free(output, secret=False))
        finally:
            _zero(entropy_buffer)

    def unprotect(self, protected_blob: bytes, *, entropy: bytes) -> bytearray:
        if not protected_blob or len(protected_blob) > _MAX_PROTECTED_BLOB_BYTES:
            raise PersonalKeyProviderUnavailable("protected key blob size is invalid")
        protected_buffer = bytearray(protected_blob)
        entropy_buffer = bytearray(entropy)
        if not entropy_buffer or len(entropy_buffer) > 1024:
            raise PersonalKeyProviderUnavailable("DPAPI entropy size is invalid")
        try:
            source_blob, source_ref = _blob_from_bytearray(protected_buffer)
            entropy_blob, entropy_ref = _blob_from_bytearray(entropy_buffer)
            output = _DataBlob()
            _ = source_ref, entropy_ref
            if not self._crypt32.CryptUnprotectData(
                ctypes.byref(source_blob),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            ):
                error_code = ctypes.get_last_error()
                if output.pbData:
                    self._free_native(output, secret=True, suppress_errors=True)
                raise _native_failure("Windows DPAPI decryption failed", error_code=error_code)
            if output.cbData == 0 or output.cbData > _MAX_PRIVATE_KEY_BYTES:
                if output.pbData:
                    self._free_native(output, secret=True, suppress_errors=True)
                raise PersonalKeyProviderUnavailable("decrypted private key size is invalid")
            return self._copy_and_free(output, secret=True)
        finally:
            _zero(protected_buffer)
            _zero(entropy_buffer)

    def _copy_and_free(self, blob: _DataBlob, *, secret: bool) -> bytearray:
        if not blob.pbData or blob.cbData == 0 or blob.cbData > _MAX_PROTECTED_BLOB_BYTES:
            if blob.pbData:
                self._free_native(blob, secret=secret, suppress_errors=True)
            raise PersonalKeyProviderUnavailable("Windows DPAPI returned an invalid buffer")
        copy_completed = False
        try:
            copied = bytearray(blob.cbData)
            destination = (wintypes.BYTE * blob.cbData).from_buffer(copied)
            ctypes.memmove(destination, blob.pbData, blob.cbData)
            copy_completed = True
        except Exception:
            raise PersonalKeyProviderUnavailable("Windows DPAPI output copy failed") from None
        finally:
            self._free_native(
                blob,
                secret=secret,
                suppress_errors=not copy_completed,
            )
        return copied

    def _free_native(
        self, blob: _DataBlob, *, secret: bool, suppress_errors: bool = False
    ) -> None:
        if not blob.pbData:
            return
        zero_error: Exception | None = None
        free_error: Exception | None = None
        try:
            if secret and 0 < blob.cbData <= _MAX_PRIVATE_KEY_BYTES:
                ctypes.memset(blob.pbData, 0, blob.cbData)
        except Exception as error:
            zero_error = error
        finally:
            try:
                result = self._kernel32.LocalFree(
                    ctypes.cast(blob.pbData, wintypes.HLOCAL)
                )
                if not result:
                    blob.pbData = ctypes.POINTER(wintypes.BYTE)()
                    blob.cbData = 0
                else:
                    free_error = PersonalKeyProviderUnavailable(
                        "Windows native buffer cleanup failed"
                    )
            except Exception as error:
                free_error = error
        if not suppress_errors and (zero_error is not None or free_error is not None):
            raise PersonalKeyProviderUnavailable(
                "Windows native buffer cleanup failed"
            ) from None


class WindowsRestrictedAcl:
    """Apply a protected DACL for the current user, SYSTEM, and Administrators."""

    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _SDDL_REVISION_1 = 1

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = (("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD))

    def __init__(self) -> None:
        if platform.system() != "Windows" or not hasattr(ctypes, "WinDLL"):
            raise PersonalKeyProviderUnavailable("Windows ACL protection is unavailable")
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_functions()
        self._current_sid = self._read_current_sid()

    @property
    def current_user_sid(self) -> str:
        return self._current_sid

    def secure(self, path: Path) -> None:
        self._apply_dacl(path, self._expected_sddl(path), protected=True)

    def verify(self, path: Path) -> bool:
        try:
            actual = self._normalize_dacl_flags(self._read_dacl_sddl(path))
            expected = self._normalize_dacl_flags(
                self._canonicalize_sddl(self._expected_sddl(path))
            )
            return actual == expected
        except Exception:
            return False

    @staticmethod
    def _normalize_dacl_flags(sddl: str) -> str:
        """Ignore Windows' informational AI flag, but no ACL/ACE semantics."""
        header, separator, entries = sddl.partition("(")
        if not separator or not header.startswith("D:"):
            raise PersonalKeyProviderUnavailable("Windows ACL serialization failed")
        flags = header[2:].replace("AI", "")
        return f"D:{flags}({entries}"

    def _expected_sddl(self, path: Path) -> str:
        inheritance = "OICI" if path.is_dir() else ""
        return (
            f"D:P(A;{inheritance};FA;;;{self._current_sid})"
            f"(A;{inheritance};FA;;;SY)(A;{inheritance};FA;;;BA)"
        )

    def _apply_dacl(self, path: Path, sddl: str, *, protected: bool) -> None:
        security_descriptor = wintypes.LPVOID()
        size = wintypes.DWORD()
        dacl = wintypes.LPVOID()
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            self._SDDL_REVISION_1,
            ctypes.byref(security_descriptor),
            ctypes.byref(size),
        ):
            raise _native_failure("Windows ACL descriptor creation failed")
        try:
            if not self._advapi32.GetSecurityDescriptorDacl(
                security_descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            ) or not present.value:
                raise _native_failure("Windows ACL descriptor validation failed")
            result = self._advapi32.SetNamedSecurityInfoW(
                str(path),
                self._SE_FILE_OBJECT,
                self._DACL_SECURITY_INFORMATION
                | (
                    self._PROTECTED_DACL_SECURITY_INFORMATION
                    if protected
                    else self._UNPROTECTED_DACL_SECURITY_INFORMATION
                ),
                None,
                None,
                dacl,
                None,
            )
            if result != 0:
                raise PersonalKeyProviderUnavailable("Windows ACL application failed")
        finally:
            if security_descriptor:
                self._local_free(security_descriptor)

    def _read_dacl_sddl(self, path: Path) -> str:
        owner = wintypes.LPVOID()
        group = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        security_descriptor = wintypes.LPVOID()
        result = self._advapi32.GetNamedSecurityInfoW(
            str(path),
            self._SE_FILE_OBJECT,
            self._DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            ctypes.byref(group),
            ctypes.byref(dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        try:
            if result != 0 or not security_descriptor or not dacl:
                raise PersonalKeyProviderUnavailable("Windows ACL inspection failed")
            return self._descriptor_to_sddl(security_descriptor)
        finally:
            if security_descriptor:
                self._local_free(security_descriptor)

    def _canonicalize_sddl(self, sddl: str) -> str:
        security_descriptor = wintypes.LPVOID()
        size = wintypes.DWORD()
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            self._SDDL_REVISION_1,
            ctypes.byref(security_descriptor),
            ctypes.byref(size),
        ):
            raise _native_failure("Windows ACL canonicalization failed")
        try:
            return self._descriptor_to_sddl(security_descriptor)
        finally:
            if security_descriptor:
                self._local_free(security_descriptor)

    def _descriptor_to_sddl(self, security_descriptor: wintypes.LPVOID) -> str:
        text = wintypes.LPWSTR()
        length = wintypes.DWORD()
        try:
            if not self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                security_descriptor,
                self._SDDL_REVISION_1,
                self._DACL_SECURITY_INFORMATION,
                ctypes.byref(text),
                ctypes.byref(length),
            ):
                raise _native_failure("Windows ACL serialization failed")
            if not text or not text.value:
                raise PersonalKeyProviderUnavailable("Windows ACL serialization failed")
            return text.value
        finally:
            if text:
                self._local_free(text)

    def _read_current_sid(self) -> str:
        token = wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(
            self._kernel32.GetCurrentProcess(), self._TOKEN_QUERY, ctypes.byref(token)
        ):
            raise _native_failure("Windows user token is unavailable")
        try:
            required = wintypes.DWORD()
            self._advapi32.GetTokenInformation(
                token, self._TOKEN_USER, None, 0, ctypes.byref(required)
            )
            if required.value == 0:
                raise _native_failure("Windows user identity is unavailable")
            buffer = ctypes.create_string_buffer(required.value)
            if not self._advapi32.GetTokenInformation(
                token,
                self._TOKEN_USER,
                buffer,
                required,
                ctypes.byref(required),
            ):
                raise _native_failure("Windows user identity is unavailable")
            token_user = ctypes.cast(
                buffer, ctypes.POINTER(self._SidAndAttributes)
            ).contents
            sid_text = wintypes.LPWSTR()
            if not self._advapi32.ConvertSidToStringSidW(
                token_user.Sid, ctypes.byref(sid_text)
            ):
                raise _native_failure("Windows user SID conversion failed")
            try:
                return sid_text.value
            finally:
                self._local_free(sid_text)
        finally:
            if token and not self._kernel32.CloseHandle(token):
                raise PersonalKeyProviderUnavailable(
                    "Windows user token cleanup failed"
                ) from None

    def _local_free(self, pointer: object) -> None:
        if pointer and self._kernel32.LocalFree(
            ctypes.cast(pointer, wintypes.HLOCAL)
        ):
            raise PersonalKeyProviderUnavailable(
                "Windows ACL native buffer cleanup failed"
            ) from None

    def _configure_functions(self) -> None:
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
        self._kernel32.LocalFree.restype = wintypes.HLOCAL
        self._advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        self._advapi32.OpenProcessToken.restype = wintypes.BOOL
        self._advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._advapi32.GetTokenInformation.restype = wintypes.BOOL
        self._advapi32.ConvertSidToStringSidW.argtypes = (
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPWSTR),
        )
        self._advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.DWORD),
        )
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self._advapi32.GetSecurityDescriptorDacl.argtypes = (
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        )
        self._advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        self._advapi32.SetNamedSecurityInfoW.argtypes = (
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.LPVOID,
        )
        self._advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        self._advapi32.GetNamedSecurityInfoW.argtypes = (
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.LPVOID),
        )
        self._advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.DWORD),
        )
        self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL


class _StoredKeyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["windows-dpapi-personal-key/v1"] = "windows-dpapi-personal-key/v1"
    descriptor: PersonalPublicKeyDescriptor
    state: Literal["active", "rotated", "revoked"]
    protected_private_key_base64: str | None = Field(default=None, min_length=1)
    state_changed_at: datetime
    state_reason: str | None = Field(default=None, min_length=1, max_length=240)
    replacement_key_id: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("state_changed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stored key timestamps require timezone awareness")
        return value.astimezone(UTC)

    @field_validator("replacement_key_id", mode="before")
    @classmethod
    def normalize_key_id(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("protected_private_key_base64")
    @classmethod
    def validate_blob(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception as error:
            raise ValueError("protected key blob must be strict base64") from error
        if not decoded or len(decoded) > _MAX_PROTECTED_BLOB_BYTES:
            raise ValueError("protected key blob size is invalid")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> "_StoredKeyRecord":
        if self.descriptor.protection is not PersonalKeyProtection.WINDOWS_DPAPI_CURRENT_USER:
            raise ValueError("stored key descriptor must use current-user DPAPI")
        if self.state == "active":
            if self.protected_private_key_base64 is None or self.state_reason is not None or self.replacement_key_id is not None:
                raise ValueError("active keys require only protected private material")
        elif self.protected_private_key_base64 is not None or self.state_reason is None:
            raise ValueError("retired keys cannot retain private material and require a reason")
        if self.state == "rotated" and self.replacement_key_id is None:
            raise ValueError("rotated keys require a replacement key ID")
        if self.state == "revoked" and self.replacement_key_id is not None:
            raise ValueError("revoked keys cannot name a replacement")
        return self


class _WindowsLifecycleLock:
    """One-byte Windows file lock shared by every provider instance/process."""

    def __init__(self, path: Path, *, timeout_seconds: float) -> None:
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._handle: Any | None = None

    def __enter__(self) -> "_WindowsLifecycleLock":
        if msvcrt is None:
            raise PersonalKeyProviderUnavailable("Windows lifecycle locking is unavailable")
        try:
            handle = self._path.open("r+b", buffering=0)
        except Exception:
            raise PersonalKeyProviderUnavailable("Windows lifecycle lock is unavailable") from None
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                if handle.read(2) != b"\x00":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    handle.close()
                    raise PersonalKeyProviderUnavailable(
                        "Windows lifecycle lock record is invalid"
                    )
                self._handle = handle
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise PersonalKeyProviderUnavailable(
                        "Windows lifecycle lock acquisition timed out"
                    ) from None
                time.sleep(0.01)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if msvcrt is not None:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            if exc is None:
                raise PersonalKeyProviderUnavailable(
                    "Windows lifecycle lock release failed"
                ) from None
        finally:
            handle.close()


class WindowsDpapiKeyProvider:
    """Persist Ed25519 private keys encrypted for the current Windows user."""

    provider_id = "WINDOWS_DPAPI_CURRENT_USER_PERSONAL_KEY_PROVIDER"

    def __init__(
        self,
        *,
        storage_dir: Path | None = None,
    ) -> None:
        self._backend: Ed25519KeyBackend | None = None
        self._lock = threading.RLock()
        self._unavailable_reason: str | None = None
        self._storage_dir = storage_dir
        self._lock_path: Path | None = None
        try:
            if platform.system() != "Windows":
                raise PersonalKeyProviderUnavailable("Windows DPAPI is unavailable")
            self._backend = CryptographyEd25519Backend()
            self._storage_dir = storage_dir or _default_storage_dir()
            self._protector = WindowsDpapiProtector()
            self._acl = WindowsRestrictedAcl()
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            self._acl.secure(self._storage_dir)
            if not self._acl.verify(self._storage_dir):
                raise PersonalKeyProviderUnavailable("key storage ACL verification failed")
            self._lock_path = self._storage_dir / _LIFECYCLE_LOCK_NAME
            self._initialize_lock_file()
        except Exception as error:
            self._protector = None
            self._acl = None
            self._unavailable_reason = _reason_for(error)

    def status(self) -> PersonalKeyProviderStatus:
        if self._unavailable_reason is not None:
            return PersonalKeyProviderStatus(
                provider_id=self.provider_id,
                availability=PersonalKeyProviderAvailability.UNAVAILABLE,
                protection=PersonalKeyProtection.UNAVAILABLE,
                platform="WINDOWS",
                reason_codes=(self._unavailable_reason,),
            )
        return PersonalKeyProviderStatus(
            provider_id=self.provider_id,
            availability=PersonalKeyProviderAvailability.AVAILABLE,
            protection=PersonalKeyProtection.WINDOWS_DPAPI_CURRENT_USER,
            platform="WINDOWS",
        )

    def create_key(self, *, key_id: str, created_at: datetime) -> PersonalPublicKeyDescriptor:
        normalized = _key_id(key_id)
        created = _utc(created_at)
        with self._exclusive_access():
            self._require_available()
            path = self._key_path(normalized)
            if path.exists():
                raise PersonalKeyProviderUnavailable("protected key already exists")
            private_key: bytearray | None = None
            try:
                private_key, public_pem = self._backend.generate_key_pair()  # type: ignore[union-attr]
                _validate_private_key(private_key)
                descriptor = PersonalPublicKeyDescriptor(
                    key_id=normalized,
                    public_key_pem=public_pem,
                    generation=1,
                    created_at=created,
                    protection=PersonalKeyProtection.WINDOWS_DPAPI_CURRENT_USER,
                )
                record = self._active_record(descriptor, private_key, changed_at=created)
                self._write_new(path, record)
                return descriptor
            except Exception:
                raise PersonalKeyProviderUnavailable("protected key creation failed") from None
            finally:
                if private_key is not None:
                    _zero(private_key)

    def export_public_key(self, *, key_id: str) -> PersonalPublicKeyDescriptor:
        normalized = _key_id(key_id)
        with self._exclusive_access():
            self._require_available()
            record = self._read_record(self._key_path(normalized), expected_key_id=normalized)
            self._validate_private_binding(record)
            return record.descriptor

    def sign(self, *, key_id: str, payload: bytes) -> bytes:
        normalized = _key_id(key_id)
        if not isinstance(payload, bytes) or not payload:
            raise PersonalKeyProviderUnavailable("signing payload is invalid")
        with self._exclusive_access():
            self._require_available()
            record = self._read_record(self._key_path(normalized), expected_key_id=normalized)
            if record.state != "active":
                raise PersonalKeyProviderUnavailable("protected key is not active")
            private_key = self._decrypt(record)
            try:
                self._assert_public_binding(record, private_key)
                signature = self._backend.sign(  # type: ignore[union-attr]
                    private_key=private_key, payload=payload
                )
                if not isinstance(signature, bytes) or len(signature) != 64:
                    raise PersonalKeyProviderUnavailable("Ed25519 backend returned an invalid signature")
                return signature
            except Exception:
                raise PersonalKeyProviderUnavailable("protected signing failed") from None
            finally:
                _zero(private_key)

    def verify(
        self, *, descriptor: PersonalPublicKeyDescriptor, payload: bytes, signature: bytes
    ) -> bool:
        self._require_available()
        if descriptor.protection is not PersonalKeyProtection.WINDOWS_DPAPI_CURRENT_USER:
            return False
        if not isinstance(payload, bytes) or not payload or not isinstance(signature, bytes) or len(signature) != 64:
            return False
        try:
            return bool(
                self._backend.verify(  # type: ignore[union-attr]
                    public_key_pem=descriptor.public_key_pem,
                    payload=payload,
                    signature=signature,
                )
            )
        except Exception:
            return False

    def rotate_key(
        self, *, previous_key_id: str, replacement_key_id: str, rotated_at: datetime
    ) -> tuple[PersonalPublicKeyDescriptor, PersonalKeyRotation]:
        previous_id = _key_id(previous_key_id)
        replacement_id = _key_id(replacement_key_id)
        changed_at = _utc(rotated_at)
        if previous_id == replacement_id:
            raise PersonalKeyProviderUnavailable("replacement key ID must be distinct")
        with self._exclusive_access():
            self._require_available()
            previous_path = self._key_path(previous_id)
            replacement_path = self._key_path(replacement_id)
            if replacement_path.exists():
                raise PersonalKeyProviderUnavailable("replacement key already exists")
            previous = self._read_record(previous_path, expected_key_id=previous_id)
            if previous.state != "active":
                raise PersonalKeyProviderUnavailable("previous key is not active")
            if changed_at <= previous.descriptor.created_at:
                raise PersonalKeyProviderUnavailable("rotation time must follow key creation")
            self._validate_private_binding(previous)
            private_key: bytearray | None = None
            try:
                private_key, public_pem = self._backend.generate_key_pair()  # type: ignore[union-attr]
                _validate_private_key(private_key)
                replacement = PersonalPublicKeyDescriptor(
                    key_id=replacement_id,
                    public_key_pem=public_pem,
                    generation=previous.descriptor.generation + 1,
                    created_at=changed_at,
                    protection=PersonalKeyProtection.WINDOWS_DPAPI_CURRENT_USER,
                )
                replacement_record = self._active_record(
                    replacement, private_key, changed_at=changed_at
                )
                retired = _StoredKeyRecord(
                    descriptor=previous.descriptor,
                    state="rotated",
                    state_changed_at=changed_at,
                    state_reason="explicit personal key rotation",
                    replacement_key_id=replacement_id,
                )
                # Retire first. A crash between writes leaves no usable old key,
                # which is safer than silently preserving two active generations.
                self._replace(previous_path, retired)
                self._write_new(replacement_path, replacement_record)
                rotation = PersonalKeyRotation(
                    previous_key_id=previous_id,
                    replacement_key_id=replacement_id,
                    replacement_generation=replacement.generation,
                    rotated_at=changed_at,
                    reason="explicit personal key rotation",
                )
                return replacement, rotation
            except Exception:
                raise PersonalKeyProviderUnavailable("protected key rotation failed") from None
            finally:
                if private_key is not None:
                    _zero(private_key)

    def revoke_key(self, *, key_id: str, revoked_at: datetime, reason: str) -> PersonalKeyRevocation:
        normalized = _key_id(key_id)
        changed_at = _utc(revoked_at)
        clean_reason = reason.strip()
        if not clean_reason or len(clean_reason) > 240:
            raise PersonalKeyProviderUnavailable("key revocation reason is invalid")
        with self._exclusive_access():
            self._require_available()
            path = self._key_path(normalized)
            record = self._read_record(path, expected_key_id=normalized)
            if record.state == "revoked":
                if record.state_reason != clean_reason:
                    raise PersonalKeyProviderUnavailable("key revocation conflicts with existing state")
                return PersonalKeyRevocation(
                    key_id=normalized, revoked_at=record.state_changed_at, reason=clean_reason
                )
            if record.state != "active":
                raise PersonalKeyProviderUnavailable("only active keys can be revoked")
            if changed_at <= record.descriptor.created_at:
                raise PersonalKeyProviderUnavailable("revocation time must follow key creation")
            self._validate_private_binding(record)
            revoked = _StoredKeyRecord(
                descriptor=record.descriptor,
                state="revoked",
                state_changed_at=changed_at,
                state_reason=clean_reason,
            )
            self._replace(path, revoked)
            return PersonalKeyRevocation(
                key_id=normalized, revoked_at=changed_at, reason=clean_reason
            )

    def recovery_status(self, *, key_id: str) -> PersonalKeyRecoveryStatus:
        try:
            normalized = _key_id(key_id)
        except PersonalKeyProviderUnavailable:
            normalized = "INVALID-KEY-ID"
        if self._unavailable_reason is not None:
            return PersonalKeyRecoveryStatus(
                key_id=normalized,
                metadata_available=False,
                signing_capability_available=False,
                requires_external_recovery=True,
                reason_codes=(self._unavailable_reason,),
            )
        with self._exclusive_access():
            path = self._key_path(normalized)
            if not path.exists():
                return PersonalKeyRecoveryStatus(
                    key_id=normalized,
                    metadata_available=False,
                    signing_capability_available=False,
                    requires_external_recovery=True,
                    reason_codes=("DPAPI_KEY_NOT_FOUND",),
                )
            try:
                record = self._read_record(path, expected_key_id=normalized)
                if record.state != "active":
                    return PersonalKeyRecoveryStatus(
                        key_id=normalized,
                        metadata_available=True,
                        signing_capability_available=False,
                        requires_external_recovery=True,
                        reason_codes=("DPAPI_KEY_NOT_ACTIVE",),
                    )
                self._validate_private_binding(record)
            except Exception:
                return PersonalKeyRecoveryStatus(
                    key_id=normalized,
                    metadata_available=True,
                    signing_capability_available=False,
                    requires_external_recovery=True,
                    reason_codes=("DPAPI_KEY_RECOVERY_FAILED",),
                )
            return PersonalKeyRecoveryStatus(
                key_id=normalized,
                metadata_available=True,
                signing_capability_available=True,
                requires_external_recovery=False,
            )

    def _active_record(
        self,
        descriptor: PersonalPublicKeyDescriptor,
        private_key: bytearray,
        *,
        changed_at: datetime,
    ) -> _StoredKeyRecord:
        provisional = _StoredKeyRecord(
            descriptor=descriptor,
            state="active",
            protected_private_key_base64=base64.b64encode(b"provisional").decode("ascii"),
            state_changed_at=changed_at,
        )
        protected = self._protector.protect(  # type: ignore[union-attr]
            private_key, entropy=_record_entropy(provisional)
        )
        return provisional.model_copy(
            update={"protected_private_key_base64": base64.b64encode(protected).decode("ascii")}
        )

    def _decrypt(self, record: _StoredKeyRecord) -> bytearray:
        if record.state != "active" or record.protected_private_key_base64 is None:
            raise PersonalKeyProviderUnavailable("protected key is not active")
        try:
            protected = base64.b64decode(record.protected_private_key_base64, validate=True)
            private_key = self._protector.unprotect(  # type: ignore[union-attr]
                protected, entropy=_record_entropy(record)
            )
            _validate_private_key(private_key)
            return private_key
        except Exception:
            raise PersonalKeyProviderUnavailable("protected key decryption failed") from None

    def _validate_private_binding(self, record: _StoredKeyRecord) -> None:
        if record.state != "active":
            return
        private_key = self._decrypt(record)
        try:
            self._assert_public_binding(record, private_key)
        finally:
            _zero(private_key)

    def _assert_public_binding(self, record: _StoredKeyRecord, private_key: bytearray) -> None:
        try:
            derived = PersonalPublicKeyDescriptor(
                key_id=record.descriptor.key_id,
                public_key_pem=self._backend.public_key_from_private(private_key),  # type: ignore[union-attr]
                generation=record.descriptor.generation,
                created_at=record.descriptor.created_at,
                expires_at=record.descriptor.expires_at,
                protection=record.descriptor.protection,
            )
        except Exception:
            raise PersonalKeyProviderUnavailable("protected key validation failed") from None
        if derived.public_key_fingerprint != record.descriptor.public_key_fingerprint:
            raise PersonalKeyProviderUnavailable("protected key does not match public metadata")

    def _read_record(self, path: Path, *, expected_key_id: str) -> _StoredKeyRecord:
        try:
            if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > _MAX_RECORD_BYTES:
                raise ValueError("invalid key record")
            if not self._acl.verify(path):  # type: ignore[union-attr]
                raise ValueError("key record ACL is not restrictive")
            raw = path.read_text(encoding="utf-8")
            record = _StoredKeyRecord.model_validate_json(raw)
            if record.descriptor.key_id != expected_key_id:
                raise ValueError("key identity mismatch")
            return record
        except Exception:
            raise PersonalKeyProviderUnavailable("protected key record is invalid") from None

    def _write_new(self, path: Path, record: _StoredKeyRecord) -> None:
        payload = record.model_dump_json(exclude_none=True, exclude_computed_fields=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._acl.secure(path)  # type: ignore[union-attr]
            if not self._acl.verify(path):  # type: ignore[union-attr]
                raise PersonalKeyProviderUnavailable("key record ACL verification failed")
        except Exception:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
            raise PersonalKeyProviderUnavailable("protected key persistence failed") from None

    def _replace(self, path: Path, record: _StoredKeyRecord) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        payload = record.model_dump_json(exclude_none=True, exclude_computed_fields=True)
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._acl.secure(temporary)  # type: ignore[union-attr]
            if not self._acl.verify(temporary):  # type: ignore[union-attr]
                raise PersonalKeyProviderUnavailable("temporary key record ACL verification failed")
            os.replace(temporary, path)
            self._acl.secure(path)  # type: ignore[union-attr]
        except Exception:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
            raise PersonalKeyProviderUnavailable("protected key state update failed") from None

    def _key_path(self, key_id: str) -> Path:
        if self._storage_dir is None:
            raise PersonalKeyProviderUnavailable("per-user key storage is unavailable")
        return self._storage_dir / f"{sha256(key_id.encode('utf-8')).hexdigest()}.key.json"

    def _initialize_lock_file(self) -> None:
        if self._lock_path is None or self._acl is None:
            raise PersonalKeyProviderUnavailable("Windows lifecycle lock is unavailable")
        try:
            try:
                with self._lock_path.open("x+b", buffering=0) as handle:
                    handle.write(b"\x00")
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                deadline = time.monotonic() + _LIFECYCLE_LOCK_TIMEOUT_SECONDS
                while self._lock_path.stat().st_size == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
            if not self._lock_record_is_valid():
                raise PersonalKeyProviderUnavailable("Windows lifecycle lock record is invalid")
            self._acl.secure(self._lock_path)
            if not self._acl.verify(self._lock_path):
                raise PersonalKeyProviderUnavailable("Windows lifecycle lock ACL is invalid")
        except Exception:
            raise PersonalKeyProviderUnavailable("Windows lifecycle lock is unavailable") from None

    @contextmanager
    def _exclusive_access(self) -> Iterator[None]:
        self._require_available()
        if (
            self._storage_dir is None
            or self._lock_path is None
            or self._acl is None
            or not self._lock_record_metadata_is_valid()
            or not self._acl.verify(self._storage_dir)
            or not self._acl.verify(self._lock_path)
        ):
            raise PersonalKeyProviderUnavailable("Windows lifecycle lock is unavailable")
        with self._lock:
            with _WindowsLifecycleLock(
                self._lock_path, timeout_seconds=_LIFECYCLE_LOCK_TIMEOUT_SECONDS
            ):
                yield

    def _lock_record_metadata_is_valid(self) -> bool:
        try:
            return bool(
                self._lock_path is not None
                and self._lock_path.is_file()
                and self._lock_path.stat().st_size == 1
            )
        except OSError:
            return False

    def _lock_record_is_valid(self) -> bool:
        try:
            return bool(
                self._lock_path is not None
                and self._lock_path.is_file()
                and self._lock_path.stat().st_size == 1
                and self._lock_path.read_bytes() == b"\x00"
            )
        except OSError:
            return False

    def _require_available(self) -> None:
        if (
            self._unavailable_reason is not None
            or self._backend is None
            or self._protector is None
            or self._acl is None
            or self._lock_path is None
        ):
            raise PersonalKeyProviderUnavailable("Windows DPAPI key provider is unavailable")


def _record_entropy(record: _StoredKeyRecord) -> bytes:
    binding = {
        "context": _DPAPI_CONTEXT.decode("ascii"),
        "entropy_schema": _DPAPI_ENTROPY_SCHEMA,
        "record_schema_version": record.schema_version,
        "descriptor_schema_version": record.descriptor.schema_version,
        "algorithm": record.descriptor.algorithm.value,
        "key_id": record.descriptor.key_id,
        "generation": record.descriptor.generation,
        "public_key_fingerprint": record.descriptor.public_key_fingerprint,
        "protection": record.descriptor.protection.value,
        "created_at": record.descriptor.created_at.isoformat(),
        "expires_at": (
            None
            if record.descriptor.expires_at is None
            else record.descriptor.expires_at.isoformat()
        ),
        "state": record.state,
        "state_changed_at": record.state_changed_at.isoformat(),
        "state_reason": record.state_reason,
        "replacement_key_id": record.replacement_key_id,
    }
    return sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).digest()


def _blob_from_bytearray(value: bytearray) -> tuple[_DataBlob, object]:
    native = (wintypes.BYTE * len(value)).from_buffer(value)
    return _DataBlob(len(value), ctypes.cast(native, ctypes.POINTER(wintypes.BYTE))), native


def _default_storage_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise PersonalKeyProviderUnavailable("per-user application data is unavailable")
    return Path(local_app_data) / "NimbusDesk" / "FRIDAY" / "PersonalPolicyKeys"


def _key_id(value: str) -> str:
    normalized = value.strip().upper() if isinstance(value, str) else ""
    if re.fullmatch(r"[A-Z0-9][A-Z0-9._:-]{0,159}", normalized) is None:
        raise PersonalKeyProviderUnavailable("key ID is invalid")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PersonalKeyProviderUnavailable("key lifecycle timestamps require timezone awareness")
    return value.astimezone(UTC)


def _validate_private_key(value: object) -> None:
    if not isinstance(value, bytearray) or not value or len(value) > _MAX_PRIVATE_KEY_BYTES:
        raise PersonalKeyProviderUnavailable("Ed25519 backend returned invalid private key material")


def _zero(value: bytearray) -> None:
    if value:
        ctypes.memset((wintypes.BYTE * len(value)).from_buffer(value), 0, len(value))


def _native_failure(
    message: str, *, error_code: int | None = None
) -> PersonalKeyProviderUnavailable:
    if error_code is None:
        error_code = ctypes.get_last_error()
    return PersonalKeyProviderUnavailable(f"{message} (Windows error {error_code})")


def _reason_for(error: Exception) -> str:
    text = str(error).lower()
    if "ed25519" in text:
        return "ED25519_BACKEND_UNAVAILABLE"
    if "acl" in text:
        return "WINDOWS_KEY_STORAGE_ACL_UNAVAILABLE"
    if "application data" in text:
        return "WINDOWS_USER_STORAGE_UNAVAILABLE"
    return "WINDOWS_DPAPI_UNAVAILABLE"
