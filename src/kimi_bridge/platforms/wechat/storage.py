"""Private, versioned local storage for the WeChat adapter."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .api import normalize_https_base_url
from .types import WeChatAPIError, WeChatCredential, WeChatStorageError


CREDENTIAL_VERSION = 1
CREDENTIAL_FILE_NAME = "credentials.json"
RUNTIME_STATE_FILE_NAME = "runtime-state.json"
_OWNED_FILE_NAMES = (CREDENTIAL_FILE_NAME, RUNTIME_STATE_FILE_NAME)


@dataclass(frozen=True, slots=True)
class StorageInspection:
    """Secret-safe local storage inspection used by status and doctor."""

    directory_exists: bool
    credential_exists: bool
    credential: WeChatCredential | None
    directory_error: str | None = None
    credential_error: str | None = None


class WeChatStorage:
    """Atomically load and replace one WeChat authorization."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    @property
    def credential_path(self) -> Path:
        return self.path / CREDENTIAL_FILE_NAME

    @property
    def runtime_state_path(self) -> Path:
        return self.path / RUNTIME_STATE_FILE_NAME

    def has_credential(self) -> bool:
        if os.path.lexists(self.path) and (
            self.path.is_symlink() or not self.path.is_dir()
        ):
            return False
        return os.path.lexists(self.credential_path)

    def load_credential(self) -> WeChatCredential:
        if self.path.is_symlink() or not self.path.is_dir():
            raise WeChatStorageError("WeChat storage path is missing or unsafe")
        path = self.credential_path
        if path.is_symlink() or not path.is_file():
            raise WeChatStorageError("WeChat credential file is missing or unsafe")
        try:
            with path.open(encoding="utf-8") as credential_file:
                payload = json.load(credential_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise WeChatStorageError("WeChat credential file is unreadable or invalid") from exc
        return _credential_from_payload(payload)

    def save_credential(self, credential: WeChatCredential) -> None:
        validated = _validate_credential(credential)
        self._ensure_directory()
        payload: dict[str, Any] = {
            "version": CREDENTIAL_VERSION,
            "bot_token": validated.bot_token,
            "bot_id": validated.bot_id,
            "base_url": validated.base_url,
            "authorized_at": validated.authorized_at,
        }
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path,
            prefix=f".{CREDENTIAL_FILE_NAME}.",
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
            os.replace(temporary_path, self.credential_path)
            if os.name == "posix":
                self.credential_path.chmod(0o600)
        finally:
            temporary_path.unlink(missing_ok=True)

    def clear_owned_files(self) -> tuple[str, ...]:
        if os.path.lexists(self.path) and (
            self.path.is_symlink() or not self.path.is_dir()
        ):
            raise WeChatStorageError("WeChat storage path is not a safe directory")
        removed: list[str] = []
        for name in _OWNED_FILE_NAMES:
            path = self.path / name
            if not os.path.lexists(path):
                continue
            try:
                path.unlink()
            except OSError as exc:
                raise WeChatStorageError(
                    f"could not remove adapter-owned WeChat file: {name}"
                ) from exc
            removed.append(name)
        return tuple(removed)

    def inspect(self, *, platform_name: str) -> StorageInspection:
        directory_exists = os.path.lexists(self.path)
        directory_error: str | None = None
        credential_error: str | None = None
        credential: WeChatCredential | None = None

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
                except (OSError, TypeError, ValueError, WeChatStorageError):
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
                raise WeChatStorageError("WeChat storage path is not a safe directory")
        else:
            self.path.mkdir(parents=True, mode=0o700)
        if os.name == "posix":
            self.path.chmod(0o700)


def redact_bot_id(bot_id: str) -> str:
    """Return a stable, non-secret projection of an iLink bot identity."""

    if len(bot_id) <= 8:
        return "***"
    return f"{bot_id[:4]}…{bot_id[-4:]}"


def _credential_from_payload(payload: object) -> WeChatCredential:
    if not isinstance(payload, dict):
        raise WeChatStorageError("unsupported WeChat credential format")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise WeChatStorageError("unsupported WeChat credential format")
    if version != CREDENTIAL_VERSION:
        raise WeChatStorageError("unsupported WeChat credential version")
    expected = {
        "version",
        "bot_token",
        "bot_id",
        "base_url",
        "authorized_at",
    }
    if set(payload) != expected:
        raise WeChatStorageError("unsupported WeChat credential format")
    credential = WeChatCredential(
        bot_token=_required_string(payload, "bot_token"),
        bot_id=_required_string(payload, "bot_id"),
        base_url=_required_string(payload, "base_url"),
        authorized_at=_required_string(payload, "authorized_at"),
    )
    return _validate_credential(credential)


def _validate_credential(credential: WeChatCredential) -> WeChatCredential:
    bot_token = credential.bot_token.strip()
    bot_id = credential.bot_id.strip()
    authorized_at = credential.authorized_at.strip()
    if not bot_token or not bot_id or not authorized_at:
        raise WeChatStorageError("WeChat credential fields must be non-empty strings")
    try:
        parsed_time = datetime.fromisoformat(authorized_at)
    except ValueError as exc:
        raise WeChatStorageError("WeChat authorization time is invalid") from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise WeChatStorageError("WeChat authorization time must include a timezone")
    try:
        base_url = normalize_https_base_url(
            credential.base_url, field="stored WeChat base URL"
        )
    except WeChatAPIError as exc:
        raise WeChatStorageError("stored WeChat base URL is invalid") from exc
    return WeChatCredential(
        bot_token=bot_token,
        bot_id=bot_id,
        base_url=base_url,
        authorized_at=authorized_at,
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WeChatStorageError("WeChat credential fields are invalid")
    return value
