"""Deterministic tests for Feishu/Lark QR registration, storage, and controls."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from kimi_bridge.config import FeishuConfig, load_config
from kimi_bridge.platforms.feishu.auth import (
    FEISHU_REGISTRATION_ADDONS,
    FeishuControlError,
    RegistrationResult,
    _register_app,
    authorize_with_qr,
    run_login,
    run_logout,
    run_status,
)
from kimi_bridge.platforms.feishu.storage import (
    CREDENTIAL_FILE_NAME,
    FEISHU_API_DOMAIN,
    LARK_API_DOMAIN,
    FeishuCredential,
    FeishuStorage,
    FeishuStorageError,
    redact_app_id,
    redact_open_id,
)

requires_posix_modes = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file modes are not enforceable here"
)

APP_ID = "cli_ab12cd34ef56"
APP_SECRET = "FEISHU_SECRET_12345"
AUTHORIZED_AT = "2026-08-08T12:00:00+00:00"


def _credential(
    app_secret: str = APP_SECRET,
    *,
    brand: str = "feishu",
    api_domain: str = FEISHU_API_DOMAIN,
    operator_open_id: str | None = "ou_operator_12345",
) -> FeishuCredential:
    return FeishuCredential(
        app_id=APP_ID,
        app_secret=app_secret,
        api_domain=api_domain,
        tenant_brand=brand,
        authorized_at=AUTHORIZED_AT,
        operator_open_id=operator_open_id,
    )


async def _registration_flow(result: dict[str, Any]):
    async def flow(
        on_qr_code: Callable[[dict[str, Any]], None],
        on_status_change: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        on_qr_code({"url": "https://accounts.feishu.cn/qr", "expire_in": 300})
        return result

    return flow


async def _authorize(
    storage: FeishuStorage,
    result: dict[str, Any],
    *,
    replace: bool = False,
    stream: StringIO | None = None,
) -> RegistrationResult:
    return await authorize_with_qr(
        storage,
        replace=replace,
        stream=stream or StringIO(),
        register_app=await _registration_flow(result),
    )


def _registration(
    *,
    app_id: str = APP_ID,
    app_secret: str = APP_SECRET,
    brand: str = "feishu",
) -> dict[str, Any]:
    return {
        "client_id": app_id,
        "client_secret": app_secret,
        "user_info": {"tenant_brand": brand, "open_id": "ou_operator_12345"},
    }


def _write_credential_json(storage: FeishuStorage, payload: dict[str, Any]) -> None:
    storage.path.mkdir(mode=0o700, parents=True)
    storage.credential_path.write_text(json.dumps(payload), encoding="utf-8")
    storage.credential_path.chmod(0o600)


# ---------------------------------------------------------------------------
# Registration flow
# ---------------------------------------------------------------------------


async def test_sdk_registration_requests_bridge_addons_and_allows_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lark = pytest.importorskip("lark_oapi")
    captured: dict[str, Any] = {}

    async def fake_aregister_app(
        _on_qr_code: Callable[[dict[str, Any]], None],
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured.update(kwargs)
        return _registration()

    monkeypatch.setattr(lark, "aregister_app", fake_aregister_app)

    await _register_app(lambda _info: None, lambda _info: None)

    assert captured["source"] == "kimi-bridge"
    assert captured["domain"] == "https://accounts.feishu.cn"
    assert captured["lark_domain"] == "https://accounts.larksuite.com"
    assert captured["addons"] == FEISHU_REGISTRATION_ADDONS
    assert "create_only" not in captured
    assert "app_id" not in captured


async def test_registration_success_persists_feishu_credential(
    tmp_path: Path,
) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    output = StringIO()

    result = await _authorize(storage, _registration(), stream=output)

    assert result.replaced is False
    credential = storage.load_credential()
    assert credential.app_id == APP_ID
    assert credential.app_secret == APP_SECRET
    assert credential.api_domain == FEISHU_API_DOMAIN
    assert credential.tenant_brand == "feishu"
    assert credential.operator_open_id == "ou_operator_12345"

    rendered = output.getvalue()
    assert "\nFeishu application registration URL\n\n" in rendered
    assert "  https://accounts.feishu.cn/qr\n" in rendered
    assert "create or select an application" in rendered
    assert "required permissions, message event, and card callback" in rendered
    assert "Application identity: cli_…ef56" in rendered
    assert APP_SECRET not in rendered


def test_run_login_adds_operator_open_id_to_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from kimi_bridge.platforms.feishu import auth as auth_module

    async def fake_authorize(*_args: Any, **_kwargs: Any) -> RegistrationResult:
        return RegistrationResult(_credential(), replaced=False)

    monkeypatch.setattr(auth_module, "authorize_with_qr", fake_authorize)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'platform = "feishu"\n[feishu]\nallowed_users = []\n',
        encoding="utf-8",
    )
    output = StringIO()

    assert (
        run_login(
            FeishuConfig(storage_path=tmp_path / "feishu"),
            stream=output,
            config_path=config_path,
        )
        == 0
    )

    assert load_config(config_path).feishu.allowed_users == frozenset(
        {"ou_operator_12345"}
    )
    assert (
        "Added the registration user open_id to feishu.allowed_users."
        in output.getvalue()
    )


async def test_registration_lark_tenant_uses_lark_domain(tmp_path: Path) -> None:
    storage = FeishuStorage(tmp_path / "feishu")

    result = await _authorize(
        storage, _registration(brand="lark"), stream=StringIO()
    )

    assert result.credential.api_domain == LARK_API_DOMAIN
    assert result.credential.tenant_brand == "lark"
    assert storage.load_credential().api_domain == LARK_API_DOMAIN


async def test_registration_missing_fields_fail_without_persisting(
    tmp_path: Path,
) -> None:
    storage = FeishuStorage(tmp_path / "feishu")

    with pytest.raises(FeishuControlError, match="no application ID"):
        await _authorize(storage, _registration(app_id=""))
    assert not storage.has_credential()

    with pytest.raises(FeishuControlError, match="no application secret"):
        await _authorize(storage, _registration(app_secret=""))
    assert not storage.has_credential()

    with pytest.raises(FeishuControlError, match="unknown tenant brand"):
        await _authorize(storage, _registration(brand="other"))
    assert not storage.has_credential()


async def test_registration_requires_a_qr_url(tmp_path: Path) -> None:
    storage = FeishuStorage(tmp_path / "feishu")

    async def flow_without_url(
        on_qr_code: Callable[[dict[str, Any]], None],
        on_status_change: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        on_qr_code({"expire_in": 300})
        return _registration()

    with pytest.raises(FeishuControlError, match="did not provide"):
        await authorize_with_qr(
            storage,
            stream=StringIO(),
            register_app=flow_without_url,
        )
    assert not storage.has_credential()


async def test_existing_credential_blocks_login_without_replace(
    tmp_path: Path,
) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    storage.save_credential(_credential())

    async def forbidden_flow(
        on_qr_code: Callable[[dict[str, Any]], None],
        on_status_change: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        raise AssertionError("no registration flow should start when blocked")

    with pytest.raises(FeishuControlError, match="--replace"):
        await authorize_with_qr(
            storage, stream=StringIO(), register_app=forbidden_flow
        )
    assert storage.load_credential() == _credential()


async def test_replace_with_corrupt_existing_credential_still_registers(
    tmp_path: Path,
) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    _write_credential_json(storage, {"version": 999, "broken": True})

    result = await _authorize(storage, _registration(), replace=True)

    assert result.replaced is True
    assert storage.load_credential().app_secret == APP_SECRET


async def test_replace_keeps_old_credential_when_flow_fails(
    tmp_path: Path,
) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    storage.save_credential(_credential(app_secret="ORIGINAL_SECRET"))

    async def failing_flow(
        on_qr_code: Callable[[dict[str, Any]], None],
        on_status_change: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        raise RuntimeError("network exploded")

    with pytest.raises(FeishuControlError, match="retry the QR flow"):
        await authorize_with_qr(
            storage, replace=True, stream=StringIO(), register_app=failing_flow
        )

    assert storage.load_credential().app_secret == "ORIGINAL_SECRET"


async def test_cancellation_does_not_write_credential(tmp_path: Path) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    started = asyncio.Event()

    async def blocked_flow(
        on_qr_code: Callable[[dict[str, Any]], None],
        on_status_change: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        authorize_with_qr(
            storage, stream=StringIO(), register_app=blocked_flow
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not storage.has_credential()


# ---------------------------------------------------------------------------
# Storage: schema, permissions, hygiene
# ---------------------------------------------------------------------------


def test_save_credential_writes_versioned_schema(tmp_path: Path) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    storage.save_credential(
        _credential(operator_open_id="ou_operator_12345")
    )

    payload = json.loads(storage.credential_path.read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "app_id": APP_ID,
        "app_secret": APP_SECRET,
        "api_domain": FEISHU_API_DOMAIN,
        "tenant_brand": "feishu",
        "authorized_at": AUTHORIZED_AT,
        "operator_open_id": "ou_operator_12345",
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("app_secret", "", "non-empty"),
        ("authorized_at", "not-a-timestamp", "authorization time is invalid"),
        ("authorized_at", "2026-08-08T12:00:00", "must include a timezone"),
        ("tenant_brand", "other", "tenant brand is invalid"),
        ("api_domain", "https://evil.example", "API domain is invalid"),
    ],
)
def test_save_credential_rejects_invalid_fields(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    credential = replace(_credential(), **{field: value})

    with pytest.raises(FeishuStorageError, match=message):
        storage.save_credential(credential)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "app_id": "a", "app_secret": "s", "api_domain": FEISHU_API_DOMAIN, "tenant_brand": "feishu", "authorized_at": AUTHORIZED_AT},
        {"version": 999, "app_id": "a", "app_secret": "s", "api_domain": FEISHU_API_DOMAIN, "tenant_brand": "feishu", "authorized_at": AUTHORIZED_AT, "operator_open_id": None},
        {"version": True, "app_id": "a", "app_secret": "s", "api_domain": FEISHU_API_DOMAIN, "tenant_brand": "feishu", "authorized_at": AUTHORIZED_AT, "operator_open_id": None},
        {"version": 1, "app_id": "a", "app_secret": 123, "api_domain": FEISHU_API_DOMAIN, "tenant_brand": "feishu", "authorized_at": AUTHORIZED_AT, "operator_open_id": None},
        {"version": 1, "app_id": "a", "app_secret": "s", "api_domain": "https://other.example", "tenant_brand": "feishu", "authorized_at": AUTHORIZED_AT, "operator_open_id": None},
    ],
)
def test_load_credential_rejects_unsupported_schemas(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    _write_credential_json(storage, payload)

    with pytest.raises(FeishuStorageError, match="unsupported|invalid"):
        storage.load_credential()


def test_load_credential_rejects_unknown_versions_without_exposing_secret(
    tmp_path: Path,
) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    _write_credential_json(
        storage,
        {
            "version": 999,
            "app_id": "a",
            "app_secret": "FUTURE_SECRET",
            "api_domain": FEISHU_API_DOMAIN,
            "tenant_brand": "feishu",
            "authorized_at": AUTHORIZED_AT,
            "operator_open_id": None,
        },
    )

    with pytest.raises(FeishuStorageError) as caught:
        storage.load_credential()

    assert "unsupported" in str(caught.value)
    assert "FUTURE_SECRET" not in str(caught.value)


def test_load_credential_roundtrip_and_secret_safe_repr(tmp_path: Path) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    storage.save_credential(_credential())

    assert storage.load_credential() == _credential()
    assert APP_SECRET not in repr(storage.load_credential())
    assert "app_secret" not in repr(storage.load_credential())


@requires_posix_modes
def test_storage_is_atomic_private_and_secret_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kimi_bridge.platforms.feishu import storage as storage_module

    storage = FeishuStorage(tmp_path / "feishu")
    original = _credential(app_secret="ORIGINAL_TOKEN_SECRET")
    storage.save_credential(original)

    assert oct(storage.path.stat().st_mode & 0o777) == "0o700"
    assert oct(storage.credential_path.stat().st_mode & 0o777) == "0o600"
    assert "ORIGINAL_TOKEN_SECRET" not in repr(original)

    before = storage.credential_path.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(storage_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        storage.save_credential(_credential(app_secret="REPLACEMENT_TOKEN_SECRET"))

    assert storage.credential_path.read_bytes() == before
    assert storage.load_credential() == original
    assert not tuple(storage.path.glob("*.tmp"))


@requires_posix_modes
def test_load_credential_rejects_unsafe_modes(tmp_path: Path) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    storage.save_credential(_credential())

    storage.path.chmod(0o755)
    with pytest.raises(FeishuStorageError, match="storage directory mode must be 700"):
        storage.load_credential()

    storage.path.chmod(0o700)
    storage.credential_path.chmod(0o644)
    with pytest.raises(FeishuStorageError, match="credential file mode must be 600"):
        storage.load_credential()


@requires_posix_modes
def test_storage_rejects_symlinked_credential_and_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_credential = outside / CREDENTIAL_FILE_NAME
    outside_credential.write_text("{}", encoding="utf-8")

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(FeishuStorageError, match="unsafe"):
        FeishuStorage(linked_root).load_credential()
    with pytest.raises(FeishuStorageError, match="safe directory"):
        FeishuStorage(linked_root).clear_owned_files()

    storage = FeishuStorage(tmp_path / "feishu")
    storage.path.mkdir(mode=0o700, parents=True)
    storage.credential_path.symlink_to(outside_credential)
    with pytest.raises(FeishuStorageError, match="unsafe"):
        storage.load_credential()


@requires_posix_modes
def test_storage_rejects_non_regular_credential_file(tmp_path: Path) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    storage.path.mkdir(mode=0o700, parents=True)
    storage.credential_path.mkdir()

    with pytest.raises(FeishuStorageError, match="unsafe"):
        storage.load_credential()


@requires_posix_modes
def test_inspect_is_secret_safe(tmp_path: Path) -> None:
    storage = FeishuStorage(tmp_path / "feishu")
    inspection = storage.inspect(platform_name="linux")
    assert inspection.directory_exists is False
    assert inspection.credential is None
    assert inspection.directory_error is None
    assert inspection.credential_error is None

    storage.save_credential(_credential())
    inspection = storage.inspect(platform_name="linux")
    assert inspection.credential_exists is True
    assert inspection.credential == _credential()
    assert APP_SECRET not in repr(inspection)

    storage.path.chmod(0o755)
    inspection = storage.inspect(platform_name="linux")
    assert inspection.directory_error == "storage directory mode must be 700"
    assert inspection.credential is None
    storage.path.chmod(0o700)


def test_redact_app_id_and_open_id() -> None:
    assert redact_app_id("cli_ab12cd34ef56") == "cli_…ef56"
    assert redact_app_id("short") == "***"
    assert redact_open_id("ou_operator_12345") == "ou_o…2345"
    assert redact_open_id(None) == "not recorded"


# ---------------------------------------------------------------------------
# CLI runners
# ---------------------------------------------------------------------------


def test_run_status_is_local_redacted_and_reports_permissions(
    tmp_path: Path,
) -> None:
    empty = FeishuConfig(storage_path=tmp_path / "empty")
    assert run_status(empty, stream=StringIO()) == 1

    config = FeishuConfig(storage_path=tmp_path / "feishu")
    FeishuStorage(config.storage_path).save_credential(_credential())
    output = StringIO()
    assert run_status(config, stream=output) == 0
    rendered = output.getvalue()
    assert "Managed authorization: present locally" in rendered
    assert "Application identity: cli_…ef56" in rendered
    assert "Tenant brand: feishu" in rendered
    assert APP_SECRET not in rendered


def test_run_status_reports_complete_toml_fallback(tmp_path: Path) -> None:
    config = FeishuConfig(
        storage_path=tmp_path / "feishu",
        app_id=APP_ID,
        app_secret=APP_SECRET,
    )
    output = StringIO()

    assert run_status(config, stream=output) == 0

    rendered = output.getvalue()
    assert "Managed authorization: not present locally." in rendered
    assert "Legacy TOML authorization: present locally for cli_…ef56" in rendered
    assert APP_SECRET not in rendered
    assert APP_ID not in rendered


def test_run_status_incomplete_toml_pair_still_fails(tmp_path: Path) -> None:
    config = FeishuConfig(storage_path=tmp_path / "feishu", app_id=APP_ID)
    output = StringIO()

    assert run_status(config, stream=output) == 1

    rendered = output.getvalue()
    assert "Authorization: not configured locally." in rendered
    assert APP_SECRET not in rendered


def test_run_status_managed_credential_takes_precedence_over_toml(
    tmp_path: Path,
) -> None:
    config = FeishuConfig(
        storage_path=tmp_path / "feishu",
        app_id="TOML-APP-ID",
        app_secret="TOML-SECRET",
    )
    FeishuStorage(config.storage_path).save_credential(_credential())
    output = StringIO()

    assert run_status(config, stream=output) == 0

    rendered = output.getvalue()
    assert "Managed authorization: present locally" in rendered
    assert "TOML-APP-ID" not in rendered
    assert "TOML-SECRET" not in rendered


def test_run_status_reports_corrupt_managed_credential_without_secrets(
    tmp_path: Path,
) -> None:
    config = FeishuConfig(
        storage_path=tmp_path / "feishu",
        app_id="TOML-APP-ID",
        app_secret="TOML-SECRET",
    )
    storage = FeishuStorage(config.storage_path)
    storage.path.mkdir(mode=0o700, parents=True)
    storage.credential_path.write_text("{not-json", encoding="utf-8")
    storage.credential_path.chmod(0o600)
    output = StringIO()

    assert run_status(config, stream=output) == 1

    rendered = output.getvalue()
    assert "Managed authorization error" in rendered
    assert "TOML-SECRET" not in rendered


def test_run_logout_removes_only_owned_file_and_is_idempotent(
    tmp_path: Path,
) -> None:
    config = FeishuConfig(storage_path=tmp_path / "feishu")
    storage = FeishuStorage(config.storage_path)
    storage.save_credential(_credential())
    unrelated = storage.path / "operator-note.txt"
    unrelated.write_text("preserve", encoding="utf-8")

    output = StringIO()
    assert run_logout(config, stream=output) == 0
    assert "Removed Feishu managed credential" in output.getvalue()
    assert not storage.has_credential()
    assert unrelated.read_text(encoding="utf-8") == "preserve"
    assert storage.path.is_dir()

    assert run_logout(config, stream=StringIO()) == 0
