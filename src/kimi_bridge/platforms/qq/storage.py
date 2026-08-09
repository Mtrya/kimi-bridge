"""Private, versioned local storage for the QQ QR bootstrap flow.

The managed credential is the final output of the QR registration flow only:
the bot ``app_id`` and the decrypted ``app_secret``, plus when it was
authorized. The AES key that decrypts the server secret, the short-lived
``task_id``/QR URL, and the encrypted secret blob are never persisted here.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

CREDENTIAL_VERSION = 1
CREDENTIAL_FILE_NAME = "credentials.json"
_OWNED_FILE_NAMES = (CREDENTIAL_FILE_NAME,)


class QQControlError(RuntimeError):
    """Expected, secret-safe QQ control-plane failure (storage and auth)."""


class QQStorageError(QQControlError):
    """Invalid or unsafe adapter-owned local state."""


@dataclass(frozen=True, slots=True)
class QQManagedCredential:
    """One locally stored QQ official-bot authorization."""

    app_id: str
    app_secret: str = field(repr=False)
    authorized_at: str


@dataclass(frozen=True, slots=True)
class StorageInspection:
    """Secret-safe local storage inspection used by status and doctor."""

    directory_exists: bool
    credential_exists: bool
    credential: QQManagedCredential | None
    directory_error: str | None = None
    credential_error: str | None = None


def _require_private_mode(path: Path, expected: int, label: str) -> None:
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise QQStorageError(f"QQ {label} mode could not be inspected") from exc
    if mode != expected:
        raise QQStorageError(f"QQ {label} mode must be {expected:o}")


class QQStorage:
    """Atomically load and replace one QQ managed credential."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    @property
    def credential_path(self) -> Path:
        return self.path / CREDENTIAL_FILE_NAME

    def has_credential(self) -> bool:
        if os.path.lexists(self.path) and (
            self.path.is_symlink() or not self.path.is_dir()
        ):
            return False
        return os.path.lexists(self.credential_path)

    def load_credential(self) -> QQManagedCredential:
        if self.path.is_symlink() or not self.path.is_dir():
            raise QQStorageError("QQ storage path is missing or unsafe")
        _require_private_mode(self.path, 0o700, "storage directory")
        path = self.credential_path
        if path.is_symlink() or not path.is_file():
            raise QQStorageError("QQ credential file is missing or unsafe")
        _require_private_mode(path, 0o600, "credential file")
        try:
            with path.open(encoding="utf-8") as credential_file:
                payload = json.load(credential_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise QQStorageError("QQ credential file is unreadable or invalid") from exc
        return _credential_from_payload(payload)

    def save_credential(self, credential: QQManagedCredential) -> None:
        validated = _validate_credential(credential)
        payload: dict[str, Any] = {
            "version": CREDENTIAL_VERSION,
            "app_id": validated.app_id,
            "app_secret": validated.app_secret,
            "authorized_at": validated.authorized_at,
        }
        self._write_private_json(self.credential_path, payload)

    def _write_private_json(self, path: Path, payload: dict[str, Any]) -> None:
        self._ensure_directory()
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as credential_file:
                json.dump(payload, credential_file, ensure_ascii=False, indent=2)
                credential_file.write("\n")
                credential_file.flush()
                os.fsync(credential_file.fileno())
            os.replace(temporary_path, path)
            if os.name == "posix":
                path.chmod(0o600)
        finally:
            temporary_path.unlink(missing_ok=True)

    def clear_owned_files(self) -> tuple[str, ...]:
        if os.path.lexists(self.path) and (
            self.path.is_symlink() or not self.path.is_dir()
        ):
            raise QQStorageError("QQ storage path is not a safe directory")
        removed: list[str] = []
        for name in _OWNED_FILE_NAMES:
            path = self.path / name
            if not os.path.lexists(path):
                continue
            try:
                path.unlink()
            except OSError as exc:
                raise QQStorageError(
                    f"could not remove adapter-owned QQ file: {name}"
                ) from exc
            removed.append(name)
        return tuple(removed)

    def inspect(self, *, platform_name: str) -> StorageInspection:
        directory_exists = os.path.lexists(self.path)
        directory_error: str | None = None
        credential_error: str | None = None
        credential: QQManagedCredential | None = None

        if directory_exists:
            if self.path.is_symlink() or not self.path.is_dir():
                directory_error = "storage path is not a safe directory"
            elif not os.access(self.path, os.W_OK | os.X_OK):
                directory_error = "storage directory is not writable"
            elif not platform_name.startswith("win"):
                try:
                    mode = stat.S_IMODE(self.path.stat().st_mode)
                except OSError:
                    directory_error = "storage directory mode could not be inspected"
                else:
                    if mode != 0o700:
                        directory_error = "storage directory mode must be 700"

        credential_exists = (
            directory_error is None and os.path.lexists(self.credential_path)
        )
        if credential_exists:
            if self.credential_path.is_symlink() or not self.credential_path.is_file():
                credential_error = "credential path is not a safe regular file"
            elif not platform_name.startswith("win"):
                try:
                    mode = stat.S_IMODE(self.credential_path.stat().st_mode)
                except OSError:
                    credential_error = "credential file mode could not be inspected"
                else:
                    if mode != 0o600:
                        credential_error = "credential file mode must be 600"
            if credential_error is None:
                try:
                    credential = self.load_credential()
                except (OSError, TypeError, ValueError, QQStorageError):
                    credential_error = "credential file is unreadable or invalid"

        return StorageInspection(
            directory_exists=directory_exists,
            credential_exists=credential_exists,
            credential=credential,
            directory_error=directory_error,
            credential_error=credential_error,
        )

    def _ensure_directory(self) -> None:
        if os.path.lexists(self.path):
            if self.path.is_symlink() or not self.path.is_dir():
                raise QQStorageError("QQ storage path is not a safe directory")
        else:
            self.path.mkdir(parents=True, mode=0o700)
        if os.name == "posix":
            self.path.chmod(0o700)


def redact_app_id(app_id: str) -> str:
    """Return a stable, non-secret projection of a QQ bot app_id."""

    if len(app_id) <= 8:
        return "***"
    return f"{app_id[:4]}…{app_id[-4:]}"


def redact_openid(openid: str) -> str:
    """Return a stable, non-secret projection of a QQ user openid."""

    if len(openid) <= 8:
        return "***"
    return f"{openid[:4]}…{openid[-4:]}"


def _credential_from_payload(payload: object) -> QQManagedCredential:
    if not isinstance(payload, dict):
        raise QQStorageError("unsupported QQ credential format")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise QQStorageError("unsupported QQ credential format")
    if version != CREDENTIAL_VERSION:
        raise QQStorageError("unsupported QQ credential version")
    expected = {"version", "app_id", "app_secret", "authorized_at"}
    if set(payload) != expected:
        raise QQStorageError("unsupported QQ credential format")
    credential = QQManagedCredential(
        app_id=_required_string(payload, "app_id"),
        app_secret=_required_string(payload, "app_secret"),
        authorized_at=_required_string(payload, "authorized_at"),
    )
    return _validate_credential(credential)


def _validate_credential(credential: QQManagedCredential) -> QQManagedCredential:
    app_id = credential.app_id.strip()
    app_secret = credential.app_secret.strip()
    authorized_at = credential.authorized_at.strip()
    if not app_id or not app_secret or not authorized_at:
        raise QQStorageError("QQ credential fields must be non-empty strings")
    try:
        parsed_time = datetime.fromisoformat(authorized_at)
    except ValueError as exc:
        raise QQStorageError("QQ authorization time is invalid") from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise QQStorageError("QQ authorization time must include a timezone")
    return QQManagedCredential(
        app_id=app_id,
        app_secret=app_secret,
        authorized_at=authorized_at,
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise QQStorageError("QQ stored fields are invalid")
    return value
