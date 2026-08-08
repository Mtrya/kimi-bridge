"""Private, versioned local storage for Feishu QR registration."""

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
FEISHU_API_DOMAIN = "https://open.feishu.cn"
LARK_API_DOMAIN = "https://open.larksuite.com"
_VALID_DOMAINS = {FEISHU_API_DOMAIN, LARK_API_DOMAIN}
_VALID_BRANDS = {"feishu", "lark"}


class FeishuStorageError(RuntimeError):
    """A local Feishu managed credential is missing, unsafe, or invalid."""


def _require_private_mode(path: Path, expected: int, label: str) -> None:
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise FeishuStorageError(f"Feishu {label} mode could not be inspected") from exc
    if mode != expected:
        raise FeishuStorageError(f"Feishu {label} mode must be {expected:o}")


@dataclass(frozen=True, slots=True)
class FeishuCredential:
    """One application credential created by Feishu/Lark registration."""

    app_id: str
    app_secret: str = field(repr=False)
    api_domain: str = FEISHU_API_DOMAIN
    tenant_brand: str = "feishu"
    authorized_at: str = ""
    operator_open_id: str | None = None


@dataclass(frozen=True, slots=True)
class FeishuStorageInspection:
    """Secret-safe local storage inspection used by status and doctor."""

    directory_exists: bool
    credential_exists: bool
    credential: FeishuCredential | None
    directory_error: str | None = None
    credential_error: str | None = None


class FeishuStorage:
    """Atomically load and replace one Feishu QR application credential."""

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

    def load_credential(self) -> FeishuCredential:
        if self.path.is_symlink() or not self.path.is_dir():
            raise FeishuStorageError("Feishu storage path is missing or unsafe")
        _require_private_mode(self.path, 0o700, "storage directory")
        path = self.credential_path
        if path.is_symlink() or not path.is_file():
            raise FeishuStorageError("Feishu credential file is missing or unsafe")
        _require_private_mode(path, 0o600, "credential file")
        try:
            with path.open(encoding="utf-8") as credential_file:
                payload = json.load(credential_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise FeishuStorageError(
                "Feishu credential file is unreadable or invalid"
            ) from exc
        return _credential_from_payload(payload)

    def save_credential(self, credential: FeishuCredential) -> None:
        validated = _validate_credential(credential)
        self._write_private_json(
            self.credential_path,
            {
                "version": CREDENTIAL_VERSION,
                "app_id": validated.app_id,
                "app_secret": validated.app_secret,
                "api_domain": validated.api_domain,
                "tenant_brand": validated.tenant_brand,
                "authorized_at": validated.authorized_at,
                "operator_open_id": validated.operator_open_id,
            },
        )

    def inspect(self, *, platform_name: str) -> FeishuStorageInspection:
        directory_exists = os.path.lexists(self.path)
        directory_error: str | None = None
        credential_error: str | None = None
        credential: FeishuCredential | None = None

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
                except (OSError, TypeError, ValueError, FeishuStorageError):
                    credential_error = "credential file is unreadable or invalid"

        return FeishuStorageInspection(
            directory_exists=directory_exists,
            credential_exists=credential_exists,
            credential=credential,
            directory_error=directory_error,
            credential_error=credential_error,
        )

    def clear_owned_files(self) -> tuple[str, ...]:
        if os.path.lexists(self.path) and (
            self.path.is_symlink() or not self.path.is_dir()
        ):
            raise FeishuStorageError("Feishu storage path is not a safe directory")
        if not os.path.lexists(self.credential_path):
            return ()
        try:
            self.credential_path.unlink()
        except OSError as exc:
            raise FeishuStorageError(
                "could not remove adapter-owned Feishu credential"
            ) from exc
        return (CREDENTIAL_FILE_NAME,)

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

    def _ensure_directory(self) -> None:
        if os.path.lexists(self.path):
            if self.path.is_symlink() or not self.path.is_dir():
                raise FeishuStorageError("Feishu storage path is not a safe directory")
        else:
            self.path.mkdir(parents=True, mode=0o700)
        if os.name == "posix":
            self.path.chmod(0o700)


def redact_app_id(app_id: str) -> str:
    if len(app_id) <= 8:
        return "***"
    return f"{app_id[:4]}…{app_id[-4:]}"


def redact_open_id(open_id: str | None) -> str:
    if not open_id:
        return "not recorded"
    if len(open_id) <= 8:
        return "***"
    return f"{open_id[:4]}…{open_id[-4:]}"


def _credential_from_payload(payload: object) -> FeishuCredential:
    if not isinstance(payload, dict):
        raise FeishuStorageError("unsupported Feishu credential format")
    expected = {
        "version",
        "app_id",
        "app_secret",
        "api_domain",
        "tenant_brand",
        "authorized_at",
        "operator_open_id",
    }
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise FeishuStorageError("unsupported Feishu credential format")
    if version != CREDENTIAL_VERSION or set(payload) != expected:
        raise FeishuStorageError("unsupported Feishu credential format")
    operator_open_id = payload.get("operator_open_id")
    if operator_open_id is not None and not isinstance(operator_open_id, str):
        raise FeishuStorageError("Feishu stored fields are invalid")
    return _validate_credential(
        FeishuCredential(
            app_id=_required_string(payload, "app_id"),
            app_secret=_required_string(payload, "app_secret"),
            api_domain=_required_string(payload, "api_domain"),
            tenant_brand=_required_string(payload, "tenant_brand"),
            authorized_at=_required_string(payload, "authorized_at"),
            operator_open_id=operator_open_id,
        )
    )


def _validate_credential(credential: FeishuCredential) -> FeishuCredential:
    app_id = credential.app_id.strip()
    app_secret = credential.app_secret.strip()
    api_domain = credential.api_domain.rstrip("/")
    tenant_brand = credential.tenant_brand.strip().lower()
    authorized_at = credential.authorized_at.strip()
    operator_open_id = (
        credential.operator_open_id.strip() if credential.operator_open_id else None
    )
    if not app_id or not app_secret or not authorized_at:
        raise FeishuStorageError("Feishu credential fields must be non-empty strings")
    if api_domain not in _VALID_DOMAINS:
        raise FeishuStorageError("stored Feishu API domain is invalid")
    if tenant_brand not in _VALID_BRANDS:
        raise FeishuStorageError("stored Feishu tenant brand is invalid")
    expected_domain = LARK_API_DOMAIN if tenant_brand == "lark" else FEISHU_API_DOMAIN
    if api_domain != expected_domain:
        raise FeishuStorageError("stored Feishu brand and API domain do not match")
    try:
        parsed_time = datetime.fromisoformat(authorized_at)
    except ValueError as exc:
        raise FeishuStorageError("Feishu authorization time is invalid") from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise FeishuStorageError("Feishu authorization time must include a timezone")
    return FeishuCredential(
        app_id=app_id,
        app_secret=app_secret,
        api_domain=api_domain,
        tenant_brand=tenant_brand,
        authorized_at=authorized_at,
        operator_open_id=operator_open_id,
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FeishuStorageError("Feishu stored fields are invalid")
    return value
