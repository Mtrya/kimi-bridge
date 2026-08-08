"""Private, versioned local storage for the WeChat adapter."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .api import normalize_https_base_url
from .types import WeChatAPIError, WeChatCredential, WeChatStorageError


CREDENTIAL_VERSION = 1
RUNTIME_STATE_VERSION = 2
CREDENTIAL_FILE_NAME = "credentials.json"
RUNTIME_STATE_FILE_NAME = "runtime-state.json"
_OWNED_FILE_NAMES = (CREDENTIAL_FILE_NAME, RUNTIME_STATE_FILE_NAME)


def _require_private_mode(path: Path, expected: int, label: str) -> None:
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise WeChatStorageError(f"WeChat {label} mode could not be inspected") from exc
    if mode != expected:
        raise WeChatStorageError(f"WeChat {label} mode must be {expected:o}")


@dataclass(frozen=True, slots=True)
class StorageInspection:
    """Secret-safe local storage inspection used by status and doctor."""

    directory_exists: bool
    credential_exists: bool
    credential: WeChatCredential | None
    directory_error: str | None = None
    credential_error: str | None = None
    runtime_state_exists: bool = False
    runtime_state_error: str | None = None


@dataclass(frozen=True, slots=True)
class WeChatRuntimeState:
    """Adapter-private cursor, durable dedupe window, and reply contexts."""

    get_updates_buf: str = field(default="", repr=False)
    context_tokens: dict[tuple[str, str], str] = field(
        default_factory=dict, repr=False
    )
    processed_message_ids: tuple[tuple[str, str, int], ...] = field(
        default=(), repr=False
    )


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
        _require_private_mode(self.path, 0o700, "storage directory")
        path = self.credential_path
        if path.is_symlink() or not path.is_file():
            raise WeChatStorageError("WeChat credential file is missing or unsafe")
        _require_private_mode(path, 0o600, "credential file")
        try:
            with path.open(encoding="utf-8") as credential_file:
                payload = json.load(credential_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise WeChatStorageError("WeChat credential file is unreadable or invalid") from exc
        return _credential_from_payload(payload)

    def save_credential(self, credential: WeChatCredential) -> None:
        validated = _validate_credential(credential)
        payload: dict[str, Any] = {
            "version": CREDENTIAL_VERSION,
            "bot_token": validated.bot_token,
            "bot_id": validated.bot_id,
            "base_url": validated.base_url,
            "authorized_at": validated.authorized_at,
        }
        self._write_private_json(self.credential_path, payload)

    def load_runtime_state(self) -> WeChatRuntimeState:
        if not os.path.lexists(self.path):
            return WeChatRuntimeState()
        if self.path.is_symlink() or not self.path.is_dir():
            raise WeChatStorageError("WeChat storage path is missing or unsafe")
        _require_private_mode(self.path, 0o700, "storage directory")
        path = self.runtime_state_path
        if not os.path.lexists(path):
            return WeChatRuntimeState()
        if path.is_symlink() or not path.is_file():
            raise WeChatStorageError("WeChat runtime state is missing or unsafe")
        _require_private_mode(path, 0o600, "runtime-state file")
        try:
            with path.open(encoding="utf-8") as state_file:
                payload = json.load(state_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise WeChatStorageError(
                "WeChat runtime state is unreadable or invalid"
            ) from exc
        return _runtime_state_from_payload(payload)

    def save_runtime_state(self, state: WeChatRuntimeState) -> None:
        validated = _validate_runtime_state(state)
        contexts = [
            {
                "bot_id": bot_id,
                "conversation_id": conversation_id,
                "context_token": token,
            }
            for (bot_id, conversation_id), token in sorted(
                validated.context_tokens.items()
            )
        ]
        self._write_private_json(
            self.runtime_state_path,
            {
                "version": RUNTIME_STATE_VERSION,
                "get_updates_buf": validated.get_updates_buf,
                "context_tokens": contexts,
                "processed_message_ids": [
                    {
                        "bot_id": bot_id,
                        "sender_id": sender_id,
                        "message_id": message_id,
                    }
                    for bot_id, sender_id, message_id in validated.processed_message_ids
                ],
            },
        )

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

        runtime_state_exists = (
            directory_error is None and os.path.lexists(self.runtime_state_path)
        )
        runtime_state_error: str | None = None
        if runtime_state_exists:
            if (
                self.runtime_state_path.is_symlink()
                or not self.runtime_state_path.is_file()
            ):
                runtime_state_error = "runtime-state path is not a safe regular file"
            elif not platform_name.startswith("win"):
                try:
                    mode = stat.S_IMODE(self.runtime_state_path.stat().st_mode)
                except OSError:
                    runtime_state_error = (
                        "runtime-state file mode could not be inspected"
                    )
                else:
                    if mode != 0o600:
                        runtime_state_error = "runtime-state file mode must be 600"
            if runtime_state_error is None:
                try:
                    self.load_runtime_state()
                except (OSError, TypeError, ValueError, WeChatStorageError):
                    runtime_state_error = (
                        "runtime-state file is unreadable or invalid"
                    )

        return StorageInspection(
            directory_exists=directory_exists,
            credential_exists=credential_exists,
            credential=credential,
            directory_error=directory_error,
            credential_error=credential_error,
            runtime_state_exists=runtime_state_exists,
            runtime_state_error=runtime_state_error,
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


def _runtime_state_from_payload(payload: object) -> WeChatRuntimeState:
    if not isinstance(payload, dict):
        raise WeChatStorageError("unsupported WeChat runtime state format")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise WeChatStorageError("unsupported WeChat runtime state format")
    if version not in {1, RUNTIME_STATE_VERSION}:
        raise WeChatStorageError("unsupported WeChat runtime state version")
    expected = {"version", "get_updates_buf", "context_tokens"}
    if version == RUNTIME_STATE_VERSION:
        expected.add("processed_message_ids")
    if set(payload) != expected:
        raise WeChatStorageError("unsupported WeChat runtime state format")
    cursor = payload.get("get_updates_buf")
    contexts = payload.get("context_tokens")
    if not isinstance(cursor, str) or not isinstance(contexts, list):
        raise WeChatStorageError("unsupported WeChat runtime state format")
    context_tokens: dict[tuple[str, str], str] = {}
    for entry in contexts:
        if not isinstance(entry, dict) or set(entry) != {
            "bot_id",
            "conversation_id",
            "context_token",
        }:
            raise WeChatStorageError("unsupported WeChat runtime state format")
        bot_id = _required_string(entry, "bot_id")
        conversation_id = _required_string(entry, "conversation_id")
        token = _required_string(entry, "context_token")
        key = (bot_id, conversation_id)
        if key in context_tokens:
            raise WeChatStorageError("unsupported WeChat runtime state format")
        context_tokens[key] = token
    processed_message_ids: list[tuple[str, str, int]] = []
    raw_processed = payload.get("processed_message_ids", [])
    if not isinstance(raw_processed, list):
        raise WeChatStorageError("unsupported WeChat runtime state format")
    for entry in raw_processed:
        if not isinstance(entry, dict) or set(entry) != {
            "bot_id",
            "sender_id",
            "message_id",
        }:
            raise WeChatStorageError("unsupported WeChat runtime state format")
        bot_id = _required_string(entry, "bot_id")
        sender_id = _required_string(entry, "sender_id")
        message_id = entry.get("message_id")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            raise WeChatStorageError("unsupported WeChat runtime state format")
        processed_message_ids.append((bot_id, sender_id, message_id))
    return _validate_runtime_state(
        WeChatRuntimeState(
            get_updates_buf=cursor,
            context_tokens=context_tokens,
            processed_message_ids=tuple(processed_message_ids),
        )
    )


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


def _validate_runtime_state(state: WeChatRuntimeState) -> WeChatRuntimeState:
    if not isinstance(state.get_updates_buf, str):
        raise WeChatStorageError("WeChat runtime cursor must be a string")
    contexts: dict[tuple[str, str], str] = {}
    for key, token in state.context_tokens.items():
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or not all(isinstance(value, str) and value.strip() for value in key)
            or not isinstance(token, str)
            or not token.strip()
        ):
            raise WeChatStorageError("WeChat runtime context fields are invalid")
        contexts[(key[0].strip(), key[1].strip())] = token.strip()
    processed: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()
    for identity in state.processed_message_ids:
        if (
            not isinstance(identity, tuple)
            or len(identity) != 3
            or not isinstance(identity[0], str)
            or not identity[0].strip()
            or not isinstance(identity[1], str)
            or not identity[1].strip()
            or isinstance(identity[2], bool)
            or not isinstance(identity[2], int)
            or identity[2] <= 0
        ):
            raise WeChatStorageError("WeChat processed-message identity is invalid")
        normalized = (identity[0].strip(), identity[1].strip(), identity[2])
        if normalized in seen:
            raise WeChatStorageError(
                "WeChat processed-message identities must be unique"
            )
        seen.add(normalized)
        processed.append(normalized)
    return WeChatRuntimeState(
        get_updates_buf=state.get_updates_buf,
        context_tokens=contexts,
        processed_message_ids=tuple(processed),
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WeChatStorageError("WeChat stored fields are invalid")
    return value
