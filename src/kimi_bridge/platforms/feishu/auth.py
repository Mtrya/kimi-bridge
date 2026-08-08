"""Headless Feishu/Lark QR application-registration controls."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TextIO

from ...config import FeishuConfig
from .storage import (
    FEISHU_API_DOMAIN,
    LARK_API_DOMAIN,
    FeishuCredential,
    FeishuStorage,
    FeishuStorageError,
    redact_app_id,
    redact_open_id,
)


FEISHU_ACCOUNT_DOMAIN = "https://accounts.feishu.cn"
LARK_ACCOUNT_DOMAIN = "https://accounts.larksuite.com"


class FeishuControlError(RuntimeError):
    """An expected Feishu onboarding or local-control failure."""


class RegistrationResult:
    """Confirmed managed credential and whether it replaced an existing one."""

    __slots__ = ("credential", "replaced")

    def __init__(self, credential: FeishuCredential, *, replaced: bool) -> None:
        self.credential = credential
        self.replaced = replaced


RegistrationFlow = Callable[
    [Callable[[dict[str, Any]], None], Callable[[dict[str, Any]], None]],
    Awaitable[dict[str, Any]],
]


async def authorize_with_qr(
    storage: FeishuStorage,
    *,
    replace: bool = False,
    stream: TextIO = sys.stdout,
    register_app: RegistrationFlow | None = None,
) -> RegistrationResult:
    """Complete one QR app-registration flow and persist only its result."""

    replaced = False
    if storage.has_credential():
        if not replace:
            raise FeishuControlError(
                "Feishu managed credentials already exist; use "
                "feishu login --replace to register a replacement"
            )
        replaced = True

    def on_qr_code(info: dict[str, Any]) -> None:
        url = info.get("url")
        expire_in = info.get("expire_in")
        if not isinstance(url, str) or not url:
            raise FeishuControlError("Feishu registration did not provide an authorization URL")
        stream.write("Feishu application registration URL:\n")
        stream.write(f"{url}\n")
        if isinstance(expire_in, int) and expire_in > 0:
            stream.write(f"This URL expires in about {expire_in} seconds.\n")
        stream.write("Open the URL and approve application registration in Feishu or Lark.\n")
        stream.flush()

    def on_status_change(info: dict[str, Any]) -> None:
        status = info.get("status")
        if status == "slow_down":
            interval = info.get("interval")
            if isinstance(interval, (int, float)):
                stream.write(
                    f"Feishu registration is still waiting for approval; "
                    f"polling every {interval:g} seconds.\n"
                )
                stream.flush()
        elif status == "domain_switched":
            stream.write("The registration flow switched to the Lark account domain.\n")
            stream.flush()

    flow = register_app or _register_app
    try:
        result = await flow(on_qr_code, on_status_change)
    except FeishuControlError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise FeishuControlError(
            "Feishu application registration failed; retry the QR flow"
        ) from exc

    credential = _credential_from_registration(result)
    storage.save_credential(credential)
    stream.write(
        "Feishu application credentials were saved in private local storage.\n"
    )
    stream.write(f"Application identity: {redact_app_id(credential.app_id)}\n")
    stream.write(f"Tenant brand: {credential.tenant_brand}\n")
    if credential.operator_open_id:
        stream.write(
            "Registration user identity: "
            f"{redact_open_id(credential.operator_open_id)}\n"
        )
    stream.write(
        "Configure at least one intended sender in [feishu].allowed_users before startup.\n"
    )
    stream.flush()
    return RegistrationResult(credential, replaced=replaced)


async def _register_app(
    on_qr_code: Callable[[dict[str, Any]], None],
    on_status_change: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    try:
        import lark_oapi as lark
    except ImportError as exc:
        raise FeishuControlError(
            "Feishu support is not installed; reinstall kimi-bridge"
        ) from exc
    return await lark.aregister_app(
        on_qr_code,
        on_status_change=on_status_change,
        source="kimi-bridge",
        domain=FEISHU_ACCOUNT_DOMAIN,
        lark_domain=LARK_ACCOUNT_DOMAIN,
        create_only=True,
    )


def _credential_from_registration(result: object) -> FeishuCredential:
    if not isinstance(result, dict):
        raise FeishuControlError("Feishu registration returned an invalid result")
    app_id = result.get("client_id")
    app_secret = result.get("client_secret")
    user_info = result.get("user_info")
    if not isinstance(app_id, str) or not app_id:
        raise FeishuControlError("Feishu registration returned no application ID")
    if not isinstance(app_secret, str) or not app_secret:
        raise FeishuControlError("Feishu registration returned no application secret")
    if user_info is None:
        user_info = {}
    if not isinstance(user_info, dict):
        raise FeishuControlError("Feishu registration returned invalid user metadata")
    brand = user_info.get("tenant_brand", "feishu")
    if not isinstance(brand, str) or brand.lower() not in {"feishu", "lark"}:
        raise FeishuControlError("Feishu registration returned an unknown tenant brand")
    brand = brand.lower()
    operator_open_id = user_info.get("open_id")
    if operator_open_id is not None and (
        not isinstance(operator_open_id, str) or not operator_open_id.strip()
    ):
        raise FeishuControlError("Feishu registration returned invalid user identity")
    return FeishuCredential(
        app_id=app_id,
        app_secret=app_secret,
        api_domain=LARK_API_DOMAIN if brand == "lark" else FEISHU_API_DOMAIN,
        tenant_brand=brand,
        authorized_at=datetime.now(timezone.utc).isoformat(),
        operator_open_id=operator_open_id,
    )


def run_login(
    config: FeishuConfig,
    *,
    replace: bool = False,
    stream: TextIO = sys.stdout,
) -> int:
    """Run QR application registration without starting bridge services."""

    try:
        asyncio.run(authorize_with_qr(FeishuStorage(config.storage_path), replace=replace, stream=stream))
    except FeishuControlError:
        raise
    except (OSError, TypeError, ValueError, FeishuStorageError) as exc:
        raise FeishuControlError(str(exc)) from exc
    return 0


def run_status(
    config: FeishuConfig,
    *,
    stream: TextIO = sys.stdout,
    platform_name: str = sys.platform,
) -> int:
    """Render local Feishu credential state without network access."""

    storage = FeishuStorage(config.storage_path)
    inspection = storage.inspect(platform_name=platform_name)
    stream.write(f"Feishu managed storage: {storage.path}\n")
    if inspection.directory_error:
        stream.write(f"Storage error: {inspection.directory_error}.\n")
    elif inspection.directory_exists:
        stream.write("Storage directory: locally usable.\n")
    else:
        stream.write("Storage directory: not created.\n")

    if inspection.credential_error:
        stream.write(f"Managed authorization error: {inspection.credential_error}.\n")
    elif inspection.credential is not None:
        credential = inspection.credential
        stream.write("Managed authorization: present locally; network status was not checked.\n")
        stream.write(f"Application identity: {redact_app_id(credential.app_id)}\n")
        stream.write(f"Tenant brand: {credential.tenant_brand}\n")
        stream.write(f"API domain: {credential.api_domain}\n")
        stream.write(f"Authorized at: {credential.authorized_at}\n")
    elif config.app_id and config.app_secret:
        stream.write("Managed authorization: not present locally.\n")
        stream.write(
            f"Legacy TOML authorization: present locally for {redact_app_id(config.app_id)}; "
            "network status was not checked.\n"
        )
    else:
        stream.write("Authorization: not configured locally.\n")
    stream.flush()
    return int(
        inspection.directory_error is not None
        or inspection.credential_error is not None
        or (inspection.credential is None and not (config.app_id and config.app_secret))
    )


def run_logout(
    config: FeishuConfig,
    *,
    stream: TextIO = sys.stdout,
) -> int:
    """Remove only the adapter-owned managed Feishu credential."""

    removed = FeishuStorage(config.storage_path).clear_owned_files()
    if removed:
        stream.write("Removed Feishu managed credential from local storage.\n")
        if config.app_id and config.app_secret:
            stream.write(
                "The complete [feishu] app_id/app_secret in config.toml remain available as a fallback.\n"
            )
    else:
        stream.write("No Feishu managed credential was present locally.\n")
    stream.flush()
    return 0
