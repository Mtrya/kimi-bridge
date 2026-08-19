from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections import deque
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest

from kimi_bridge.config import WechatConfig, load_config
from kimi_bridge.interactions import (
    ApprovalPrompt,
    ApprovalRequest,
    InteractionOutcome,
)
from kimi_bridge.platforms.base import (
    ConversationRef,
    InboundMessage,
    MessageRef,
    OutboundFile,
)
from kimi_bridge.platforms.wechat import (
    WeChatAPI,
    WeChatAPIError,
    WeChatAPIResult,
    WeChatAdapter,
    WeChatAuthenticationExpired,
    WeChatControlError,
    WeChatCredential,
    WeChatInboundEvent,
    WeChatMediaError,
    WeChatMessageItem,
    WeChatPollResult,
    WeChatProtocolError,
    WeChatRetryableError,
    WeChatRuntimeState,
    WeChatStorage,
    WeChatStorageError,
    WeChatTypingConfig,
    WeChatUnsupportedOperation,
    authorize_with_qr,
    run_login,
    run_logout,
    run_status,
)
from kimi_bridge.platforms.wechat.api import (
    WeChatAuthAPI,
    build_base_info,
    normalize_https_base_url,
)
from kimi_bridge.platforms.wechat.formatting import sanitize_markdown
from kimi_bridge.platforms.wechat.storage import (
    RUNTIME_STATE_FILE_NAME,
    RUNTIME_STATE_VERSION,
)
from kimi_bridge.platforms.wechat.types import (
    DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    ILINK_APP_CLIENT_VERSION,
    LoginResult,
    MESSAGE_ITEM_TYPE_TEXT,
    MESSAGE_TYPE_BOT,
    MESSAGE_TYPE_USER,
    PINNED_SOURCE_COMMIT,
    PINNED_SOURCE_TAG,
    TYPING_STATUS_ACTIVE,
    TYPING_STATUS_CANCEL,
)


requires_posix_modes = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file modes are not enforceable here"
)


class QRServer:
    def __init__(
        self,
        statuses: list[dict[str, Any]],
        *,
        qr_codes: list[dict[str, str]] | None = None,
    ) -> None:
        self.statuses = deque(statuses)
        self.qr_codes = deque(
            qr_codes
            or [
                {
                    "qrcode": "QR_SESSION_SECRET",
                    "qrcode_img_content": "https://weixin.example/authorize/one",
                }
            ]
        )
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/get_bot_qrcode"):
            if not self.qr_codes:
                raise AssertionError("unexpected QR refresh")
            return httpx.Response(200, json=self.qr_codes.popleft())
        if request.url.path.endswith("/get_qrcode_status"):
            payload = self.statuses.popleft() if self.statuses else {"status": "wait"}
            return httpx.Response(200, json=payload)
        raise AssertionError(f"unexpected path: {request.url.path}")


def _confirmed(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "confirmed",
        "bot_token": "BOT_TOKEN_SECRET",
        "ilink_bot_id": "abcd1234efgh5678@im.bot",
        "baseurl": "https://ilinkai.weixin.qq.com",
        "ilink_user_id": "scanner-user@im.wechat",
    }
    payload.update(overrides)
    return payload


async def _authorize(
    server: QRServer,
    storage: WeChatStorage,
    **kwargs: Any,
):
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        api = WeChatAuthAPI(client)
        return await authorize_with_qr(
            storage,
            api,
            stream=kwargs.pop("stream", StringIO()),
            sleep=kwargs.pop("sleep", _no_sleep),
            authorized_at=kwargs.pop(
                "authorized_at", lambda: "2026-08-08T12:00:00+00:00"
            ),
            **kwargs,
        )


async def _no_sleep(_delay: float) -> None:
    return None


def _credential(token: str = "TOKEN_SECRET") -> WeChatCredential:
    return WeChatCredential(
        bot_token=token,
        bot_id="abcd1234efgh5678@im.bot",
        base_url="https://ilinkai.weixin.qq.com",
        authorized_at="2026-08-08T12:00:00+00:00",
    )


def test_pinned_authentication_identity_matches_tencent_v246() -> None:
    assert PINNED_SOURCE_TAG == "v2.4.6"
    assert PINNED_SOURCE_COMMIT == "cef0bfc390393f716903e16d50408118047f87e0"
    assert ILINK_APP_CLIENT_VERSION == 132102
    assert build_base_info("0.6.0") == {
        "channel_version": "2.4.6",
        "bot_agent": "kimi-bridge/0.6.0",
    }
    assert build_base_info("bad version") == {
        "channel_version": "2.4.6",
        "bot_agent": "kimi-bridge/unknown",
    }


async def test_qr_transport_matches_tagged_post_and_get_headers() -> None:
    server = QRServer([{"status": "wait"}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        api = WeChatAuthAPI(client)
        qr = await api.fetch_qr_code(local_tokens=("old-token",))
        await api.get_qr_status(qr.token)

    creation, status = server.requests
    assert creation.method == "POST"
    assert creation.url.params["bot_type"] == "3"
    assert json.loads(creation.content) == {"local_token_list": ["old-token"]}
    assert creation.headers["ilink-app-id"] == "bot"
    assert creation.headers["ilink-app-clientversion"] == "132102"
    assert creation.headers["authorizationtype"] == "ilink_bot_token"
    decoded_uin = base64.b64decode(
        creation.headers["x-wechat-uin"], validate=True
    ).decode("ascii")
    assert decoded_uin.isdecimal()
    assert 0 <= int(decoded_uin) <= 0xFFFFFFFF

    assert status.method == "GET"
    assert status.headers["ilink-app-id"] == "bot"
    assert status.headers["ilink-app-clientversion"] == "132102"
    assert "x-wechat-uin" not in status.headers
    assert "authorizationtype" not in status.headers
    assert status.url.params["qrcode"] == "QR_SESSION_SECRET"


@pytest.mark.parametrize(
    "value",
    [
        "http://ilinkai.weixin.qq.com",
        "https://user@example.com",
        "https://example.com/path",
        "https://example.com?secret=value",
        "not-a-url",
    ],
)
def test_rejects_unsafe_base_urls(value: str) -> None:
    with pytest.raises(WeChatAPIError, match="invalid"):
        normalize_https_base_url(value, field="test base URL")


async def test_wait_scan_and_confirm_persists_only_typed_credentials(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    output = StringIO()
    server = QRServer(
        [{"status": "wait"}, {"status": "scaned"}, _confirmed()]
    )

    result = await _authorize(server, storage, stream=output)

    assert result.credential == storage.load_credential()
    assert result.scanner_user_id == "scanner-user@im.wechat"
    assert "BOT_TOKEN_SECRET" not in repr(result)
    assert "QR_SESSION_SECRET" not in repr(result)
    rendered = output.getvalue()
    assert "https://weixin.example/authorize/one" in rendered
    assert "QR_SESSION_SECRET" not in rendered
    assert "BOT_TOKEN_SECRET" not in rendered
    payload = json.loads(storage.credential_path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "version",
        "bot_token",
        "bot_id",
        "base_url",
        "authorized_at",
    }
    assert "scanner-user" not in storage.credential_path.read_text(encoding="utf-8")


def test_run_login_adds_scanner_identity_to_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from kimi_bridge.platforms.wechat import auth as auth_module

    async def fake_authorize(*_args: Any, **_kwargs: Any) -> LoginResult:
        return LoginResult(
            credential=_credential(),
            scanner_user_id="scanner-user@im.wechat",
        )

    monkeypatch.setattr(auth_module, "authorize_with_qr", fake_authorize)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'platform = "wechat"\n[wechat]\nallowed_users = ["existing-user"]\n',
        encoding="utf-8",
    )

    assert (
        run_login(
            WechatConfig(storage_path=tmp_path / "wechat"),
            stream=StringIO(),
            config_path=config_path,
        )
        == 0
    )

    assert load_config(config_path).wechat.allowed_users == frozenset(
        {"existing-user", "scanner-user@im.wechat"}
    )


def test_run_login_creates_missing_wechat_config_after_authorization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from kimi_bridge.platforms.wechat import auth as auth_module

    async def fake_authorize(*_args: Any, **_kwargs: Any) -> LoginResult:
        return LoginResult(
            credential=_credential(),
            scanner_user_id="scanner-user@im.wechat",
        )

    monkeypatch.setattr(auth_module, "authorize_with_qr", fake_authorize)
    config_path = tmp_path / "nested" / "config.toml"

    assert (
        run_login(
            WechatConfig(storage_path=tmp_path / "wechat"),
            stream=StringIO(),
            config_path=config_path,
            create_config=True,
        )
        == 0
    )

    config = load_config(config_path)
    assert config.platform == "wechat"
    assert config.wechat.allowed_users == frozenset(
        {"scanner-user@im.wechat"}
    )


async def test_verification_code_retry_is_carried_only_in_status_query(
    tmp_path: Path,
) -> None:
    codes = iter(("111", "222"))
    server = QRServer(
        [
            {"status": "need_verifycode"},
            {"status": "need_verifycode"},
            {"status": "scaned"},
            _confirmed(),
        ]
    )

    await _authorize(
        server,
        WeChatStorage(tmp_path / "wechat"),
        read_verify_code=lambda _prompt: next(codes),
    )

    status_requests = [
        request
        for request in server.requests
        if request.url.path.endswith("/get_qrcode_status")
    ]
    assert [request.url.params.get("verify_code") for request in status_requests] == [
        None,
        "111",
        "222",
        None,
    ]
    assert all(b"111" not in request.content for request in server.requests)


@pytest.mark.parametrize("refresh_status", ["expired", "verify_code_blocked"])
async def test_expiry_and_blocked_code_refresh_the_qr(
    tmp_path: Path, refresh_status: str
) -> None:
    output = StringIO()
    server = QRServer(
        [{"status": refresh_status}, _confirmed()],
        qr_codes=[
            {
                "qrcode": "QR_ONE_SECRET",
                "qrcode_img_content": "https://weixin.example/authorize/one",
            },
            {
                "qrcode": "QR_TWO_SECRET",
                "qrcode_img_content": "https://weixin.example/authorize/two",
            },
        ],
    )

    await _authorize(server, WeChatStorage(tmp_path / "wechat"), stream=output)

    assert output.getvalue().count("authorization URL") == 2
    assert "QR_ONE_SECRET" not in output.getvalue()
    assert "QR_TWO_SECRET" not in output.getvalue()


async def test_qr_refresh_limit_fails_without_writing_credentials(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    server = QRServer(
        [{"status": "expired"}] * 3,
        qr_codes=[
            {"qrcode": f"secret-{index}", "qrcode_img_content": f"https://qr/{index}"}
            for index in range(3)
        ],
    )

    with pytest.raises(WeChatControlError, match="repeated refreshes"):
        await _authorize(server, storage, max_qr_attempts=3)

    assert not storage.has_credential()
    assert len(
        [request for request in server.requests if request.method == "POST"]
    ) == 3


async def test_redirect_switches_only_the_polling_origin(tmp_path: Path) -> None:
    server = QRServer(
        [
            {"status": "scaned_but_redirect", "redirect_host": "edge.weixin.qq.com"},
            _confirmed(baseurl="https://edge.weixin.qq.com"),
        ]
    )

    await _authorize(server, WeChatStorage(tmp_path / "wechat"))

    status_requests = [request for request in server.requests if request.method == "GET"]
    assert [request.url.host for request in status_requests] == [
        "ilinkai.weixin.qq.com",
        "edge.weixin.qq.com",
    ]


async def test_qr_refresh_restores_default_polling_origin(
    tmp_path: Path,
) -> None:
    server = QRServer(
        [
            {
                "status": "scaned_but_redirect",
                "redirect_host": "edge.weixin.qq.com",
            },
            {"status": "expired"},
            _confirmed(baseurl="https://ilinkai.weixin.qq.com"),
        ],
        qr_codes=[
            {
                "qrcode": "QR_ONE_SECRET",
                "qrcode_img_content": "https://weixin.example/authorize/one",
            },
            {
                "qrcode": "QR_TWO_SECRET",
                "qrcode_img_content": "https://weixin.example/authorize/two",
            },
        ],
    )

    await _authorize(server, WeChatStorage(tmp_path / "wechat"))

    status_requests = [request for request in server.requests if request.method == "GET"]
    assert [request.url.host for request in status_requests] == [
        "ilinkai.weixin.qq.com",
        "edge.weixin.qq.com",
        "ilinkai.weixin.qq.com",
    ]


async def test_binded_redirect_retains_existing_authorization(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    existing = _credential("EXISTING_TOKEN_SECRET")
    storage.save_credential(existing)
    before = storage.credential_path.read_bytes()
    server = QRServer([{"status": "binded_redirect"}])

    result = await _authorize(server, storage, replace=True)

    assert result.reused_existing
    assert result.credential == existing
    assert storage.credential_path.read_bytes() == before
    creation = next(request for request in server.requests if request.method == "POST")
    assert json.loads(creation.content) == {
        "local_token_list": ["EXISTING_TOKEN_SECRET"]
    }


async def test_binded_redirect_without_local_authorization_is_actionable(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")

    with pytest.raises(WeChatControlError, match="no local authorization"):
        await _authorize(QRServer([{"status": "binded_redirect"}]), storage)

    assert not storage.has_credential()


async def test_login_refuses_to_overwrite_without_replace(tmp_path: Path) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_credential(_credential())
    server = QRServer([_confirmed()])

    with pytest.raises(WeChatControlError, match="--replace"):
        await _authorize(server, storage)

    assert server.requests == []


async def test_failed_replacement_preserves_previous_credential(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    existing = _credential("EXISTING_TOKEN_SECRET")
    storage.save_credential(existing)
    before = storage.credential_path.read_bytes()
    server = QRServer([_confirmed(baseurl="http://unsafe.example")])

    with pytest.raises(WeChatControlError, match="invalid"):
        await _authorize(server, storage, replace=True)

    assert storage.load_credential() == existing
    assert storage.credential_path.read_bytes() == before


async def test_successful_replacement_atomically_commits_the_new_credential(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_credential(_credential("EXISTING_TOKEN_SECRET"))
    server = QRServer(
        [
            _confirmed(
                bot_token="NEW_TOKEN_SECRET",
                ilink_bot_id="new-bot-identity@im.bot",
            )
        ]
    )

    result = await _authorize(server, storage, replace=True)

    assert not result.reused_existing
    assert storage.load_credential().bot_token == "NEW_TOKEN_SECRET"
    assert storage.load_credential().bot_id == "new-bot-identity@im.bot"
    creation = next(request for request in server.requests if request.method == "POST")
    assert json.loads(creation.content) == {
        "local_token_list": ["EXISTING_TOKEN_SECRET"]
    }


async def test_replacement_with_new_bot_resets_runtime_state(tmp_path: Path) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_credential(_credential("EXISTING_TOKEN_SECRET"))
    storage.save_runtime_state(
        WeChatRuntimeState(
            get_updates_buf="OLD_CURSOR",
            context_tokens={("abcd1234efgh5678@im.bot", "user-one"): "OLD_CONTEXT"},
            processed_message_ids=(("abcd1234efgh5678@im.bot", "user-one", 7),),
        )
    )
    server = QRServer(
        [
            _confirmed(
                bot_token="NEW_TOKEN_SECRET",
                ilink_bot_id="new-bot-identity@im.bot",
            )
        ]
    )

    await _authorize(server, storage, replace=True)

    assert storage.load_runtime_state() == WeChatRuntimeState()


async def test_replacement_with_same_bot_preserves_runtime_state(tmp_path: Path) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_credential(_credential("EXISTING_TOKEN_SECRET"))
    previous = WeChatRuntimeState(
        get_updates_buf="OLD_CURSOR",
        context_tokens={("abcd1234efgh5678@im.bot", "user-one"): "OLD_CONTEXT"},
        processed_message_ids=(("abcd1234efgh5678@im.bot", "user-one", 7),),
    )
    storage.save_runtime_state(previous)
    server = QRServer([_confirmed(bot_token="REFRESHED_TOKEN_SECRET")])

    await _authorize(server, storage, replace=True)

    assert storage.load_runtime_state() == previous


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("authorized_at", "not-a-timestamp", "authorization time is invalid"),
        ("base_url", "http://unsafe.example", "stored WeChat base URL is invalid"),
    ],
)
def test_storage_translates_invalid_credential_fields(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    credential = replace(_credential(), **{field: value})

    with pytest.raises(WeChatStorageError, match=message):
        storage.save_credential(credential)


async def test_timeout_and_cancellation_do_not_create_credentials(
    tmp_path: Path,
) -> None:
    class AdvancingClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

        async def sleep(self, delay: float) -> None:
            self.value += delay

    timeout_storage = WeChatStorage(tmp_path / "timeout")
    clock = AdvancingClock()
    with pytest.raises(WeChatControlError, match="timed out"):
        await _authorize(
            QRServer([]),
            timeout_storage,
            clock=clock,
            sleep=clock.sleep,
            timeout_seconds=2,
        )
    assert not timeout_storage.has_credential()

    cancellation_storage = WeChatStorage(tmp_path / "cancel")
    sleeping = asyncio.Event()

    async def blocked_sleep(_delay: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(QRServer([]))
    ) as client:
        task = asyncio.create_task(
            authorize_with_qr(
                cancellation_storage,
                WeChatAuthAPI(client),
                stream=StringIO(),
                sleep=blocked_sleep,
            )
        )
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not cancellation_storage.has_credential()


async def test_qr_status_retry_backoff_is_capped_and_resets(
    tmp_path: Path,
) -> None:
    status_results: deque[dict[str, Any] | BaseException] = deque(
        [
            httpx.ConnectError("one"),
            httpx.ConnectError("two"),
            httpx.ConnectError("three"),
            {"status": "wait"},
            httpx.ConnectError("after success"),
            _confirmed(),
        ]
    )
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_bot_qrcode"):
            return httpx.Response(
                200,
                json={
                    "qrcode": "QR_SECRET",
                    "qrcode_img_content": "https://weixin.example/authorize",
                },
            )
        result = status_results.popleft()
        if isinstance(result, BaseException):
            raise result
        return httpx.Response(200, json=result)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await authorize_with_qr(
            WeChatStorage(tmp_path / "wechat"),
            WeChatAuthAPI(client),
            stream=StringIO(),
            sleep=record_sleep,
            max_poll_retry_delay_seconds=4.0,
        )

    assert delays == [1.0, 2.0, 4.0, 1.0, 1.0]


async def test_malformed_and_failed_responses_do_not_expose_bodies(
    tmp_path: Path,
) -> None:
    secret = "DO_NOT_PRINT_RAW_RESPONSE_SECRET"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_bot_qrcode"):
            return httpx.Response(500, text=secret)
        raise AssertionError("unexpected request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WeChatRetryableError) as caught:
            await authorize_with_qr(
                WeChatStorage(tmp_path / "wechat"),
                WeChatAuthAPI(client),
                stream=StringIO(),
            )
    assert secret not in str(caught.value)


async def test_malformed_status_and_incomplete_confirmation_fail_closed(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    with pytest.raises(WeChatAPIError, match="unsupported status"):
        await _authorize(QRServer([{"status": "future-status"}]), storage)
    assert not storage.has_credential()

    with pytest.raises(WeChatControlError, match="stable scanner identity"):
        await _authorize(
            QRServer([_confirmed(ilink_user_id=None)]),
            storage,
        )
    assert not storage.has_credential()


@requires_posix_modes
def test_storage_is_atomic_private_and_secret_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kimi_bridge.platforms.wechat import storage as storage_module

    storage = WeChatStorage(tmp_path / "wechat")
    original = _credential("ORIGINAL_TOKEN_SECRET")
    storage.save_credential(original)

    assert oct(storage.path.stat().st_mode & 0o777) == "0o700"
    assert oct(storage.credential_path.stat().st_mode & 0o777) == "0o600"
    assert "ORIGINAL_TOKEN_SECRET" not in repr(original)

    before = storage.credential_path.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(storage_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        storage.save_credential(_credential("REPLACEMENT_TOKEN_SECRET"))

    assert storage.credential_path.read_bytes() == before
    assert storage.load_credential() == original
    assert not tuple(storage.path.glob("*.tmp"))


@requires_posix_modes
def test_normal_storage_load_rejects_unsafe_modes(tmp_path: Path) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_credential(_credential("MODE_TOKEN_SECRET"))
    storage.save_runtime_state(WeChatRuntimeState(get_updates_buf="MODE_CURSOR"))

    storage.path.chmod(0o755)
    with pytest.raises(WeChatStorageError, match="storage directory mode must be 700"):
        storage.load_credential()
    with pytest.raises(WeChatStorageError, match="storage directory mode must be 700"):
        storage.load_runtime_state()

    storage.path.chmod(0o700)
    storage.credential_path.chmod(0o644)
    with pytest.raises(WeChatStorageError, match="credential file mode must be 600"):
        storage.load_credential()

    storage.credential_path.chmod(0o600)
    storage.runtime_state_path.chmod(0o644)
    with pytest.raises(
        WeChatStorageError, match="runtime-state file mode must be 600"
    ):
        storage.load_runtime_state()



def test_storage_rejects_unknown_versions_without_exposing_token(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.path.mkdir(mode=0o700, parents=True)
    storage.credential_path.write_text(
        '{"version": 999, "bot_token": "FUTURE_TOKEN_SECRET"}\n',
        encoding="utf-8",
    )
    storage.credential_path.chmod(0o600)

    with pytest.raises(WeChatStorageError) as caught:
        storage.load_credential()

    assert "unsupported" in str(caught.value)
    assert "FUTURE_TOKEN_SECRET" not in str(caught.value)


def test_status_is_local_redacted_and_reports_permissions(tmp_path: Path) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    credential = _credential()
    storage.save_credential(credential)
    storage.save_runtime_state(
        WeChatRuntimeState(
            get_updates_buf="STATUS_CURSOR_SECRET",
            context_tokens={
                (credential.bot_id, "user-one"): "STATUS_CONTEXT_SECRET"
            },
        )
    )
    output = StringIO()

    assert (
        run_status(
            WechatConfig(storage_path=storage.path),
            stream=output,
        )
        == 0
    )
    rendered = output.getvalue()
    assert credential.bot_token not in rendered
    assert credential.bot_id not in rendered
    assert "STATUS_CURSOR_SECRET" not in rendered
    assert "STATUS_CONTEXT_SECRET" not in rendered
    assert "Bot identity: abcd…" in rendered
    assert "network status was not checked" in rendered
    assert "ilinkai.weixin.qq.com" in rendered


def test_logout_removes_only_owned_files_and_is_idempotent(tmp_path: Path) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_credential(_credential())
    storage.runtime_state_path.write_text("future state", encoding="utf-8")
    unrelated = storage.path / "operator-note.txt"
    unrelated.write_text("preserve", encoding="utf-8")
    output = StringIO()
    config = WechatConfig(storage_path=storage.path)

    assert run_logout(config, stream=output) == 0
    assert "credentials.json" in output.getvalue()
    assert RUNTIME_STATE_FILE_NAME in output.getvalue()
    assert unrelated.read_text(encoding="utf-8") == "preserve"
    assert storage.path.is_dir()
    assert run_logout(config, stream=StringIO()) == 0


@requires_posix_modes
def test_logout_unlinks_owned_symlink_without_touching_target(tmp_path: Path) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.path.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("preserve", encoding="utf-8")
    storage.credential_path.symlink_to(outside)

    assert run_logout(WechatConfig(storage_path=storage.path), stream=StringIO()) == 0
    assert outside.read_text(encoding="utf-8") == "preserve"
    assert not os.path.lexists(storage.credential_path)


@requires_posix_modes
def test_logout_refuses_to_follow_a_symlinked_storage_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    credential = outside / "credentials.json"
    credential.write_text("preserve", encoding="utf-8")
    linked_root = tmp_path / "wechat"
    linked_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(WeChatStorageError, match="safe directory"):
        run_logout(WechatConfig(storage_path=linked_root), stream=StringIO())

    assert credential.read_text(encoding="utf-8") == "preserve"


class RuntimeAPIStub:
    def __init__(self) -> None:
        self.polls: asyncio.Queue[WeChatPollResult | BaseException] = asyncio.Queue()
        self.poll_calls: list[tuple[str, float]] = []
        self.poll_started = asyncio.Event()
        self.poll_cancelled = asyncio.Event()
        self.sends: list[dict[str, str]] = []
        self.send_error: BaseException | None = None
        self.notify_start_result = WeChatAPIResult()
        self.notify_stop_result = WeChatAPIResult()
        self.notify_start_error: Exception | None = None
        self.notify_stop_error: Exception | None = None
        self.notify_start_results: asyncio.Queue[WeChatAPIResult | BaseException] = (
            asyncio.Queue()
        )
        self.notify_stop_results: asyncio.Queue[WeChatAPIResult | BaseException] = (
            asyncio.Queue()
        )
        self.notify_calls: list[str] = []
        self.closed = False
        self.close_calls = 0
        self.typing_ticket: str | None = None
        self.config_calls: list[tuple[str, str]] = []
        self.typing_calls: list[tuple[str, str, int]] = []
        self.config_results: asyncio.Queue[WeChatTypingConfig | BaseException] = (
            asyncio.Queue()
        )
        self.typing_results: asyncio.Queue[None | BaseException] = asyncio.Queue()

    async def get_updates(
        self, get_updates_buf: str, *, timeout_seconds: float
    ) -> WeChatPollResult:
        self.poll_calls.append((get_updates_buf, timeout_seconds))
        self.poll_started.set()
        try:
            result = await self.polls.get()
        except asyncio.CancelledError:
            self.poll_cancelled.set()
            raise
        if isinstance(result, BaseException):
            raise result
        return result

    async def send_text(
        self,
        *,
        to_user_id: str,
        context_token: str,
        text: str,
        client_id: str,
    ) -> None:
        self.sends.append(
            {
                "to_user_id": to_user_id,
                "context_token": context_token,
                "text": text,
                "client_id": client_id,
            }
        )
        if self.send_error is not None:
            raise self.send_error

    async def notify_start(self) -> WeChatAPIResult:
        self.notify_calls.append("start")
        if not self.notify_start_results.empty():
            result = await self.notify_start_results.get()
            if isinstance(result, BaseException):
                raise result
            return result
        if self.notify_start_error is not None:
            raise self.notify_start_error
        return self.notify_start_result

    async def notify_stop(self) -> WeChatAPIResult:
        self.notify_calls.append("stop")
        if not self.notify_stop_results.empty():
            result = await self.notify_stop_results.get()
            if isinstance(result, BaseException):
                raise result
            return result
        if self.notify_stop_error is not None:
            raise self.notify_stop_error
        return self.notify_stop_result

    async def get_config(
        self, *, ilink_user_id: str, context_token: str
    ) -> WeChatTypingConfig:
        self.config_calls.append((ilink_user_id, context_token))
        if not self.config_results.empty():
            result = await self.config_results.get()
            if isinstance(result, BaseException):
                raise result
            return result
        return WeChatTypingConfig(typing_ticket=self.typing_ticket)

    async def send_typing(
        self,
        *,
        ilink_user_id: str,
        typing_ticket: str,
        status: int,
    ) -> None:
        self.typing_calls.append((ilink_user_id, typing_ticket, status))
        if not self.typing_results.empty():
            result = await self.typing_results.get()
            if isinstance(result, BaseException):
                raise result

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _runtime_event(
    *,
    sender: str = "user-one",
    message_id: int = 101,
    context_token: str = "CONTEXT_TOKEN_SECRET_ONE",
    text: str = "hello",
    message_type: int = MESSAGE_TYPE_USER,
    group_id: str | None = None,
    items: tuple[WeChatMessageItem, ...] | None = None,
) -> WeChatInboundEvent:
    return WeChatInboundEvent(
        message_id=message_id,
        from_user_id=sender,
        create_time_ms=1_754_640_000_123,
        message_type=message_type,
        group_id=group_id,
        items=items
        if items is not None
        else (WeChatMessageItem(type=MESSAGE_ITEM_TYPE_TEXT, text=text),),
        context_token=context_token,
    )


async def _start_runtime_adapter(
    tmp_path: Path,
    on_message: Any,
    *,
    allowed_users: frozenset[str] = frozenset({"user-one", "user-two"}),
    initial_state: WeChatRuntimeState | None = None,
    api: RuntimeAPIStub | None = None,
    adapter_kwargs: dict[str, Any] | None = None,
) -> tuple[WeChatAdapter, RuntimeAPIStub, WeChatStorage]:
    storage = WeChatStorage(tmp_path / "wechat")
    if initial_state is not None:
        storage.save_runtime_state(initial_state)
    runtime_api = api or RuntimeAPIStub()
    adapter = WeChatAdapter(
        "bot-one@im.bot",
        allowed_users,
        api=runtime_api,
        storage=storage,
        **(adapter_kwargs or {}),
    )

    async def forbidden_interaction(*_args: Any) -> None:
        raise AssertionError("WeChat must not deliver interactions")

    await adapter.start(on_message, forbidden_interaction)
    await runtime_api.poll_started.wait()
    return adapter, runtime_api, storage


async def _wait_until(predicate: Any) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


async def test_runtime_transport_matches_pinned_headers_and_envelopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kimi_bridge.platforms.wechat import api as api_module

    uins = iter(("UIN-1", "UIN-2", "UIN-3", "UIN-4"))
    monkeypatch.setattr(api_module, "_random_wechat_uin", lambda: next(uins))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/getupdates"):
            return httpx.Response(
                200,
                json={
                    "ret": 0,
                    "msgs": [
                        {
                            "message_id": 42,
                            "from_user_id": "user-one",
                            "create_time_ms": 1_754_640_000_123,
                            "message_type": 1,
                            "item_list": [
                                {"type": 1, "text_item": {"text": "hello"}}
                            ],
                            "context_token": "INBOUND_CONTEXT_SECRET",
                        }
                    ],
                    "get_updates_buf": "NEXT_CURSOR_SECRET",
                    "longpolling_timeout_ms": 42000,
                },
            )
        return httpx.Response(200, json={"ret": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = WeChatAPI(_credential(), client)
        poll = await api.get_updates("OLD_CURSOR_SECRET", timeout_seconds=35)
        await api.send_text(
            to_user_id="user-one",
            context_token="OUTBOUND_CONTEXT_SECRET",
            text="reply",
            client_id="client-one",
        )
        assert (await api.notify_start()).ret == 0
        assert (await api.notify_stop()).ret == 0

    assert poll.long_poll_timeout_seconds == 42
    assert poll.messages[0].message_id == 42
    assert poll.messages[0].items == (
        WeChatMessageItem(type=MESSAGE_ITEM_TYPE_TEXT, text="hello"),
    )
    assert "INBOUND_CONTEXT_SECRET" not in repr(poll)
    assert "NEXT_CURSOR_SECRET" not in repr(poll)
    assert [request.headers["x-wechat-uin"] for request in requests] == [
        "UIN-1",
        "UIN-2",
        "UIN-3",
        "UIN-4",
    ]
    for request in requests:
        assert request.headers["authorization"] == "Bearer TOKEN_SECRET"
        assert request.headers["authorizationtype"] == "ilink_bot_token"
        assert request.headers["ilink-app-id"] == "bot"
        assert request.headers["ilink-app-clientversion"] == "132102"
        assert request.extensions["timeout"]["read"] > 0
        body = json.loads(request.content)
        assert body["base_info"] == build_base_info()
    assert json.loads(requests[0].content)["get_updates_buf"] == "OLD_CURSOR_SECRET"
    sent = json.loads(requests[1].content)["msg"]
    assert sent == {
        "from_user_id": "",
        "to_user_id": "user-one",
        "client_id": "client-one",
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": "reply"}}],
        "context_token": "OUTBOUND_CONTEXT_SECRET",
    }


async def test_fake_ilink_service_drives_real_adapter_and_reply_context(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    reply_sent = asyncio.Event()
    poll_cancelled = asyncio.Event()
    poll_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        requests.append(request)
        if request.url.path.endswith("/notifystart"):
            return httpx.Response(200, json={"ret": 0})
        if request.url.path.endswith("/notifystop"):
            return httpx.Response(200, json={"ret": 0})
        if request.url.path.endswith("/sendmessage"):
            reply_sent.set()
            return httpx.Response(200, json={"ret": 0})
        if request.url.path.endswith("/getupdates"):
            poll_count += 1
            if poll_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "ret": 0,
                        "msgs": [
                            {
                                "message_id": 7001,
                                "from_user_id": "user-one",
                                "create_time_ms": 1_754_640_000_123,
                                "message_type": 1,
                                "item_list": [
                                    {
                                        "type": 1,
                                        "text_item": {"text": "/status"},
                                    }
                                ],
                                "context_token": "ROUND_TRIP_CONTEXT_SECRET",
                            }
                        ],
                        "get_updates_buf": "ROUND_TRIP_CURSOR_SECRET",
                    },
                )
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                poll_cancelled.set()
                raise
        raise AssertionError(f"unexpected endpoint: {request.url.path}")

    storage = WeChatStorage(tmp_path / "wechat")
    inbound: list[InboundMessage] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = WeChatAdapter(
            "abcd1234efgh5678@im.bot",
            frozenset({"user-one"}),
            api=WeChatAPI(_credential(), client),
            storage=storage,
        )

        async def on_message(
            received_adapter: WeChatAdapter, message: InboundMessage
        ) -> None:
            inbound.append(message)
            await received_adapter.send_final_text(
                message.conversation, "**command reply**"
            )

        async def forbidden_interaction(*_args: Any) -> None:
            raise AssertionError("unexpected interaction")

        await adapter.start(on_message, forbidden_interaction)
        await reply_sent.wait()
        await _wait_until(
            lambda: storage.load_runtime_state().get_updates_buf
            == "ROUND_TRIP_CURSOR_SECRET"
        )
        await adapter.stop()

    assert [message.message_id for message in inbound] == ["7001"]
    send_request = next(
        request for request in requests if request.url.path.endswith("/sendmessage")
    )
    sent_message = json.loads(send_request.content)["msg"]
    assert sent_message["context_token"] == "ROUND_TRIP_CONTEXT_SECRET"
    assert sent_message["to_user_id"] == "user-one"
    assert sent_message["item_list"][0]["text_item"]["text"] == "**command reply**"
    assert poll_cancelled.is_set()


async def test_runtime_transport_timeout_is_empty_and_send_is_not_retried() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ReadTimeout("uncertain transport failure", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = WeChatAPI(_credential(), client)
        result = await api.get_updates("CURRENT_CURSOR", timeout_seconds=1)
        assert result == WeChatPollResult(
            messages=(), get_updates_buf="CURRENT_CURSOR"
        )
        with pytest.raises(WeChatRetryableError, match="sendMessage timeout"):
            await api.send_text(
                to_user_id="user-one",
                context_token="CONTEXT_SECRET",
                text="reply",
                client_id="client-one",
            )

    assert calls == [
        "/ilink/bot/getupdates",
        "/ilink/bot/sendmessage",
    ]


async def test_poll_connect_timeout_is_retryable_not_an_empty_poll() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timeout secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = WeChatAPI(_credential(), client)
        with pytest.raises(WeChatRetryableError, match="getUpdates timeout") as caught:
            await api.get_updates("CURSOR_SECRET", timeout_seconds=1)

    assert "connect timeout secret" not in str(caught.value)
    assert "CURSOR_SECRET" not in str(caught.value)


async def test_runtime_transport_rejects_nonzero_send_without_retry() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"ret": 23, "errmsg": "SEND_RESPONSE_SECRET"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = WeChatAPI(_credential(), client)
        with pytest.raises(WeChatProtocolError, match="ret=23") as caught:
            await api.send_text(
                to_user_id="user-one",
                context_token="CONTEXT_SECRET",
                text="reply",
                client_id="client-one",
            )

    assert calls == 1
    assert "SEND_RESPONSE_SECRET" not in str(caught.value)
    assert "CONTEXT_SECRET" not in str(caught.value)


async def test_runtime_transport_matches_typing_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/getconfig"):
            return httpx.Response(200, json={"ret": 0, "typing_ticket": "TICKET"})
        if request.url.path.endswith("/sendtyping"):
            return httpx.Response(200, json={"ret": 0})
        raise AssertionError(f"unexpected endpoint: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = WeChatAPI(_credential(), client)
        config = await api.get_config(
            ilink_user_id="user-one", context_token="CONTEXT"
        )
        await api.send_typing(
            ilink_user_id="user-one",
            typing_ticket=config.typing_ticket or "",
            status=TYPING_STATUS_ACTIVE,
        )
        await api.send_typing(
            ilink_user_id="user-one",
            typing_ticket=config.typing_ticket or "",
            status=TYPING_STATUS_CANCEL,
        )

    config_body = json.loads(requests[0].content)
    assert config_body["ilink_user_id"] == "user-one"
    assert config_body["context_token"] == "CONTEXT"
    assert config_body["base_info"] == build_base_info()
    assert [json.loads(request.content)["status"] for request in requests[1:]] == [
        TYPING_STATUS_ACTIVE,
        TYPING_STATUS_CANCEL,
    ]


async def test_stale_token_code_is_actionable_and_redacted() -> None:
    secret = "STALE_RESPONSE_SECRET"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ret": -14, "errmsg": secret})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = WeChatAPI(_credential(), client)
        with pytest.raises(WeChatAuthenticationExpired, match="login --replace") as caught:
            await api.get_updates("CURSOR_SECRET", timeout_seconds=1)

    rendered = str(caught.value)
    assert secret not in rendered
    assert "CURSOR_SECRET" not in rendered
    assert "TOKEN_SECRET" not in rendered


@pytest.mark.parametrize(
    ("status", "payload", "message"),
    [
        (500, "RAW_RESPONSE_SECRET", "HTTP 500"),
        (200, "RAW_RESPONSE_SECRET", "valid JSON"),
        (200, {"ret": -7, "errmsg": "RAW_RESPONSE_SECRET"}, "ret=-7"),
        (200, {"ret": 0, "msgs": {}}, "invalid msgs"),
    ],
)
async def test_runtime_transport_failures_are_typed_and_redacted(
    status: int, payload: object, message: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = WeChatAPI(_credential(), client)
        expected_error = WeChatRetryableError if status == 500 else WeChatProtocolError
        with pytest.raises(expected_error, match=message) as caught:
            await api.get_updates("CURSOR_SECRET", timeout_seconds=1)

    assert "RAW_RESPONSE_SECRET" not in str(caught.value)
    assert "CURSOR_SECRET" not in str(caught.value)
    assert "TOKEN_SECRET" not in str(caught.value)


async def test_runtime_transport_cancellation_aborts_the_inflight_request() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = WeChatAPI(_credential(), client)
        task = asyncio.create_task(api.get_updates("", timeout_seconds=60))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert cancelled.is_set()


async def test_tls_identity_failure_is_permanent_and_redacted() -> None:
    secret = "TLS_FAILURE_SECRET"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"certificate verify failed {secret}", request=request
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = WeChatAPI(_credential(), client)
        with pytest.raises(WeChatProtocolError, match="TLS identity") as caught:
            await api.get_updates("CURSOR_SECRET", timeout_seconds=1)

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is not None
    assert secret not in str(caught.value.__cause__)


@requires_posix_modes
def test_runtime_state_is_versioned_atomic_private_and_secret_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kimi_bridge.platforms.wechat import storage as storage_module

    storage = WeChatStorage(tmp_path / "wechat")
    original = WeChatRuntimeState(
        get_updates_buf="CURSOR_SECRET_ONE",
        context_tokens={
            ("bot-one@im.bot", "user-one"): "CONTEXT_TOKEN_SECRET_ONE"
        },
        processed_message_ids=(("bot-one@im.bot", "user-one", 1001),),
    )
    storage.save_runtime_state(original)

    assert storage.load_runtime_state() == original
    assert oct(storage.runtime_state_path.stat().st_mode & 0o777) == "0o600"
    assert "CURSOR_SECRET_ONE" not in repr(original)
    assert "CONTEXT_TOKEN_SECRET_ONE" not in repr(original)
    assert "user-one" not in repr(original)
    before = storage.runtime_state_path.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(storage_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        storage.save_runtime_state(
            WeChatRuntimeState(get_updates_buf="CURSOR_SECRET_TWO")
        )

    assert storage.runtime_state_path.read_bytes() == before
    assert storage.load_runtime_state() == original
    assert not tuple(storage.path.glob("*.tmp"))


def test_runtime_state_rejects_unknown_schema_without_exposing_secrets(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.path.mkdir(mode=0o700, parents=True)
    storage.runtime_state_path.write_text(
        json.dumps(
            {
                "version": RUNTIME_STATE_VERSION + 1,
                "get_updates_buf": "FUTURE_CURSOR_SECRET",
                "context_tokens": [],
            }
        ),
        encoding="utf-8",
    )
    storage.runtime_state_path.chmod(0o600)

    with pytest.raises(WeChatStorageError) as caught:
        storage.load_runtime_state()

    assert "unsupported" in str(caught.value)
    assert "FUTURE_CURSOR_SECRET" not in str(caught.value)


def test_runtime_state_loads_known_v1_and_rewrites_current_schema(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.path.mkdir(mode=0o700)
    storage.runtime_state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "get_updates_buf": "OLD_CURSOR",
                "context_tokens": [
                    {
                        "bot_id": "bot-one@im.bot",
                        "conversation_id": "user-one",
                        "context_token": "OLD_CONTEXT",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    storage.runtime_state_path.chmod(0o600)

    state = storage.load_runtime_state()
    assert state.processed_message_ids == ()
    storage.save_runtime_state(state)

    payload = json.loads(storage.runtime_state_path.read_text(encoding="utf-8"))
    assert payload["version"] == RUNTIME_STATE_VERSION
    assert payload["processed_message_ids"] == []


def test_runtime_state_rejects_duplicate_composite_identities(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.path.mkdir(mode=0o700)
    identity = {
        "bot_id": "bot-one@im.bot",
        "sender_id": "user-one",
        "message_id": 7,
    }
    storage.runtime_state_path.write_text(
        json.dumps(
            {
                "version": RUNTIME_STATE_VERSION,
                "get_updates_buf": "",
                "context_tokens": [],
                "processed_message_ids": [identity, identity],
            }
        ),
        encoding="utf-8",
    )
    storage.runtime_state_path.chmod(0o600)

    with pytest.raises(WeChatStorageError, match="unique"):
        storage.load_runtime_state()


async def test_allowlisted_text_maps_semantics_and_rotates_isolated_contexts(
    tmp_path: Path,
) -> None:
    inbound: list[InboundMessage] = []

    async def handler(adapter: WeChatAdapter, message: InboundMessage) -> None:
        inbound.append(message)
        await adapter.send_final_text(message.conversation, f"reply {message.message_id}")

    adapter, api, storage = await _start_runtime_adapter(tmp_path, handler)
    try:
        await adapter.handle_poll_result(
            WeChatPollResult(
                messages=(
                    _runtime_event(
                        sender="user-one",
                        message_id=101,
                        context_token="CONTEXT_ONE",
                    ),
                    _runtime_event(
                        sender="user-two",
                        message_id=202,
                        context_token="CONTEXT_TWO",
                    ),
                    _runtime_event(
                        sender="user-one",
                        message_id=303,
                        context_token="CONTEXT_THREE",
                    ),
                ),
                get_updates_buf="CURSOR_AFTER_BATCH",
            )
        )
    finally:
        await adapter.stop()

    assert [message.message_id for message in inbound] == ["101", "202", "303"]
    assert inbound[0].conversation == ConversationRef(
        platform="wechat",
        bot_id="bot-one@im.bot",
        conversation_id="user-one",
    )
    assert inbound[0].actor.id == "user-one"
    assert inbound[0].timestamp == 1_754_640_000.123
    assert [send["context_token"] for send in api.sends] == [
        "CONTEXT_ONE",
        "CONTEXT_TWO",
        "CONTEXT_THREE",
    ]
    state = storage.load_runtime_state()
    assert state.get_updates_buf == "CURSOR_AFTER_BATCH"
    assert state.context_tokens == {
        ("bot-one@im.bot", "user-one"): "CONTEXT_THREE",
        ("bot-one@im.bot", "user-two"): "CONTEXT_TWO",
    }


async def test_filtered_events_do_not_reach_the_router(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    inbound: list[InboundMessage] = []

    async def handler(_adapter: WeChatAdapter, message: InboundMessage) -> None:
        inbound.append(message)

    caplog.set_level(logging.WARNING)
    adapter, api, storage = await _start_runtime_adapter(tmp_path, handler)
    try:
        await adapter.handle_poll_result(
            WeChatPollResult(
                messages=(
                    _runtime_event(sender="intruder", message_id=501),
                    replace(
                        _runtime_event(
                            message_id=502, message_type=MESSAGE_TYPE_BOT
                        ),
                        from_user_id=None,
                    ),
                    _runtime_event(message_id=503, message_type=99),
                    _runtime_event(message_id=504, group_id="group-one"),
                ),
                get_updates_buf="FILTERED_CURSOR",
            )
        )
    finally:
        await adapter.stop()

    assert inbound == []
    assert api.sends == []
    assert storage.load_runtime_state().get_updates_buf == "FILTERED_CURSOR"
    assert "[wechat].allowed_users" in caplog.text
    assert "intruder" in caplog.text


async def test_malformed_authorized_messages_are_isolated_and_committed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    inbound: list[InboundMessage] = []

    async def handler(_adapter: WeChatAdapter, message: InboundMessage) -> None:
        inbound.append(message)

    caplog.set_level(logging.WARNING)
    initial = WeChatRuntimeState(get_updates_buf="COMMITTED_CURSOR")
    adapter, _api, storage = await _start_runtime_adapter(
        tmp_path, handler, initial_state=initial
    )
    messages = (
        replace(_runtime_event(message_id=601), from_user_id=None),
        replace(_runtime_event(message_id=602), message_type=None),
        replace(_runtime_event(message_id=603), message_id=None),
        replace(_runtime_event(message_id=604), create_time_ms=None),
        replace(_runtime_event(message_id=605), context_token=None),
        replace(_runtime_event(message_id=606), items=()),
        _runtime_event(message_id=607, context_token="VALID_CONTEXT"),
    )
    try:
        await adapter.handle_poll_result(
            WeChatPollResult(
                messages=messages,
                get_updates_buf="CURSOR_AFTER_MALFORMED",
            )
        )
        await adapter.handle_poll_result(
            WeChatPollResult(
                messages=messages,
                get_updates_buf="CURSOR_AFTER_REPLAY",
            )
        )
    finally:
        await adapter.stop()

    assert [message.message_id for message in inbound] == ["607"]
    state = storage.load_runtime_state()
    assert state.get_updates_buf == "CURSOR_AFTER_REPLAY"
    assert state.processed_message_ids == (
        ("bot-one@im.bot", "user-one", 602),
        ("bot-one@im.bot", "user-one", 604),
        ("bot-one@im.bot", "user-one", 605),
        ("bot-one@im.bot", "user-one", 606),
        ("bot-one@im.bot", "user-one", 607),
    )
    assert "malformed" in caplog.text
    assert "CONTEXT_TOKEN_SECRET_ONE" not in caplog.text


async def test_media_failure_does_not_block_later_event_or_cursor_dedupe(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class FailingMedia:
        async def download_item(self, _item: WeChatMessageItem) -> object:
            raise WeChatMediaError("invalid inbound media")

        async def close(self) -> None:
            return None

    inbound: list[str] = []

    async def handler(_adapter: WeChatAdapter, message: InboundMessage) -> None:
        inbound.append(message.message_id)

    caplog.set_level(logging.WARNING)
    adapter, _api, storage = await _start_runtime_adapter(
        tmp_path,
        handler,
        adapter_kwargs={"media": FailingMedia()},
    )
    messages = (
        _runtime_event(
            message_id=701,
            context_token="MEDIA_CONTEXT_SECRET",
            items=(WeChatMessageItem(type=2),),
        ),
        _runtime_event(message_id=702, context_token="VALID_CONTEXT"),
    )
    try:
        await adapter.handle_poll_result(
            WeChatPollResult(messages=messages, get_updates_buf="MEDIA_CURSOR")
        )
        await adapter.handle_poll_result(
            WeChatPollResult(messages=messages, get_updates_buf="REPLAY_CURSOR")
        )
    finally:
        await adapter.stop()

    assert inbound == ["702"]
    assert storage.load_runtime_state().get_updates_buf == "REPLAY_CURSOR"
    assert storage.load_runtime_state().processed_message_ids == (
        ("bot-one@im.bot", "user-one", 701),
        ("bot-one@im.bot", "user-one", 702),
    )
    assert "invalid inbound media" not in caplog.text
    assert "MEDIA_CONTEXT_SECRET" not in caplog.text


async def test_batch_failure_replay_skips_durable_prefix_and_commits_suffix(
    tmp_path: Path,
) -> None:
    handled: list[str] = []
    fail_second = True

    async def handler(_adapter: WeChatAdapter, message: InboundMessage) -> None:
        nonlocal fail_second
        handled.append(message.message_id)
        if message.message_id == "2" and fail_second:
            fail_second = False
            raise RuntimeError("handler failed")

    initial = WeChatRuntimeState(get_updates_buf="COMMITTED_CURSOR")
    adapter, _api, storage = await _start_runtime_adapter(
        tmp_path, handler, initial_state=initial
    )
    try:
        with pytest.raises(RuntimeError, match="handler failed"):
            await adapter.handle_poll_result(
                WeChatPollResult(
                    messages=(
                        _runtime_event(message_id=1, context_token="CONTEXT_ONE"),
                        _runtime_event(message_id=2, context_token="CONTEXT_TWO"),
                    ),
                    get_updates_buf="UNSAFE_CURSOR",
                )
            )
        failed_state = storage.load_runtime_state()
        assert failed_state.get_updates_buf == "COMMITTED_CURSOR"
        assert failed_state.processed_message_ids == (
            ("bot-one@im.bot", "user-one", 1),
        )

        await adapter.handle_poll_result(
            WeChatPollResult(
                messages=(
                    _runtime_event(message_id=1, context_token="CONTEXT_ONE"),
                    _runtime_event(message_id=2, context_token="CONTEXT_TWO"),
                ),
                get_updates_buf="SAFE_CURSOR",
            )
        )
    finally:
        await adapter.stop()

    assert handled == ["1", "2", "2"]
    state = storage.load_runtime_state()
    assert state.get_updates_buf == "SAFE_CURSOR"
    assert state.context_tokens[("bot-one@im.bot", "user-one")] == "CONTEXT_TWO"
    assert state.processed_message_ids == (
        ("bot-one@im.bot", "user-one", 1),
        ("bot-one@im.bot", "user-one", 2),
    )


async def test_restart_restores_cursor_context_and_composite_dedupe(
    tmp_path: Path,
) -> None:
    handled: list[tuple[str, str]] = []

    async def handler(_adapter: WeChatAdapter, message: InboundMessage) -> None:
        handled.append((message.conversation.bot_id, message.actor.id))

    first, _first_api, storage = await _start_runtime_adapter(tmp_path, handler)
    try:
        await first.handle_poll_result(
            WeChatPollResult(
                messages=(_runtime_event(message_id=77),),
                get_updates_buf="RESTART_CURSOR",
            )
        )
    finally:
        await first.stop()

    second_api = RuntimeAPIStub()
    second = WeChatAdapter(
        "bot-one@im.bot",
        frozenset({"user-one", "user-two"}),
        api=second_api,
        storage=storage,
    )
    await second.start(handler, lambda *_args: _no_sleep(0))
    await second_api.poll_started.wait()
    try:
        assert second_api.poll_calls[0][0] == "RESTART_CURSOR"
        await second.handle_poll_result(
            WeChatPollResult(
                messages=(
                    _runtime_event(message_id=77),
                    _runtime_event(sender="user-two", message_id=77),
                ),
                get_updates_buf="SECOND_CURSOR",
            )
        )
    finally:
        await second.stop()

    third_api = RuntimeAPIStub()
    third = WeChatAdapter(
        "bot-two@im.bot",
        frozenset({"user-one"}),
        api=third_api,
        storage=storage,
    )
    await third.start(handler, lambda *_args: _no_sleep(0))
    await third_api.poll_started.wait()
    try:
        await third.handle_poll_result(
            WeChatPollResult(
                messages=(_runtime_event(message_id=77),),
                get_updates_buf="THIRD_CURSOR",
            )
        )
    finally:
        await third.stop()

    assert handled == [
        ("bot-one@im.bot", "user-one"),
        ("bot-one@im.bot", "user-two"),
        ("bot-two@im.bot", "user-one"),
    ]
    state = storage.load_runtime_state()
    assert state.context_tokens[("bot-one@im.bot", "user-one")]
    assert state.context_tokens[("bot-one@im.bot", "user-two")]
    assert state.context_tokens[("bot-two@im.bot", "user-one")]


async def test_dedupe_eviction_preserves_the_active_uncommitted_batch(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_runtime_state(
        WeChatRuntimeState(
            processed_message_ids=(("bot-one@im.bot", "user-one", 99),)
        )
    )
    observed_before_second_return: tuple[tuple[str, str, int], ...] = ()
    handled: list[str] = []

    async def handler(_adapter: WeChatAdapter, message: InboundMessage) -> None:
        nonlocal observed_before_second_return
        handled.append(message.message_id)
        if message.message_id == "2":
            observed_before_second_return = (
                storage.load_runtime_state().processed_message_ids
            )

    api = RuntimeAPIStub()
    adapter = WeChatAdapter(
        "bot-one@im.bot",
        frozenset({"user-one"}),
        api=api,
        storage=storage,
        processed_message_limit=1,
    )
    await adapter.start(handler, lambda *_args: _no_sleep(0))
    await api.poll_started.wait()
    try:
        await adapter.handle_poll_result(
            WeChatPollResult(
                messages=(
                    _runtime_event(message_id=1),
                    _runtime_event(message_id=2),
                    _runtime_event(message_id=99),
                ),
                get_updates_buf="CURSOR",
            )
        )
    finally:
        await adapter.stop()

    assert observed_before_second_return == (
        ("bot-one@im.bot", "user-one", 99),
        ("bot-one@im.bot", "user-one", 1),
    )
    assert handled == ["1", "2"]
    assert storage.load_runtime_state().processed_message_ids == (
        ("bot-one@im.bot", "user-one", 2),
    )


async def test_processed_state_write_failure_does_not_advance_memory(
    tmp_path: Path,
) -> None:
    class FailProcessedStorage(WeChatStorage):
        fail_next_processed_write = True

        def save_runtime_state(self, state: WeChatRuntimeState) -> None:
            if state.processed_message_ids and self.fail_next_processed_write:
                self.fail_next_processed_write = False
                raise OSError("durable processed write failed")
            super().save_runtime_state(state)

    storage = FailProcessedStorage(tmp_path / "wechat")
    handled: list[str] = []

    async def handler(_adapter: WeChatAdapter, message: InboundMessage) -> None:
        handled.append(message.message_id)

    api = RuntimeAPIStub()
    adapter = WeChatAdapter(
        "bot-one@im.bot",
        frozenset({"user-one"}),
        api=api,
        storage=storage,
    )
    await adapter.start(handler, lambda *_args: _no_sleep(0))
    await api.poll_started.wait()
    result = WeChatPollResult(
        messages=(_runtime_event(message_id=9),),
        get_updates_buf="CURSOR",
    )
    try:
        with pytest.raises(OSError, match="durable processed write failed"):
            await adapter.handle_poll_result(result)
        assert storage.load_runtime_state().processed_message_ids == ()
        await adapter.handle_poll_result(result)
    finally:
        await adapter.stop()

    assert handled == ["9", "9"]
    assert storage.load_runtime_state().processed_message_ids == (
        ("bot-one@im.bot", "user-one", 9),
    )


async def test_empty_poll_commits_only_a_nonempty_replacement_cursor(
    tmp_path: Path,
) -> None:
    async def handler(_adapter: WeChatAdapter, _message: InboundMessage) -> None:
        raise AssertionError("empty poll reached the router")

    initial = WeChatRuntimeState(get_updates_buf="CURRENT_CURSOR")
    adapter, _api, storage = await _start_runtime_adapter(
        tmp_path, handler, initial_state=initial
    )
    try:
        await adapter.handle_poll_result(
            WeChatPollResult(messages=(), get_updates_buf="")
        )
        assert storage.load_runtime_state().get_updates_buf == "CURRENT_CURSOR"
        await adapter.handle_poll_result(
            WeChatPollResult(messages=(), get_updates_buf="NEXT_CURSOR")
        )
    finally:
        await adapter.stop()

    assert storage.load_runtime_state().get_updates_buf == "NEXT_CURSOR"


async def test_poll_backoff_is_capped_and_resets_after_success(
    tmp_path: Path,
) -> None:
    api = RuntimeAPIStub()
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    for _ in range(3):
        await api.polls.put(WeChatRetryableError("getUpdates", "transport"))
    await api.polls.put(WeChatPollResult(messages=(), get_updates_buf="ONE"))
    await api.polls.put(WeChatRetryableError("getUpdates", "HTTP", status_code=503))

    async def handler(_adapter: WeChatAdapter, _message: InboundMessage) -> None:
        raise AssertionError("empty poll reached router")

    adapter, _runtime_api, _storage = await _start_runtime_adapter(
        tmp_path,
        handler,
        api=api,
        adapter_kwargs={
            "sleep": record_sleep,
            "poll_retry_initial_seconds": 1.0,
            "poll_retry_max_seconds": 2.0,
        },
    )
    try:
        await _wait_until(lambda: len(delays) == 4)
    finally:
        await adapter.stop()

    assert delays == [1.0, 2.0, 2.0, 1.0]


async def test_poll_backoff_cancellation_is_prompt(tmp_path: Path) -> None:
    api = RuntimeAPIStub()
    sleeping = asyncio.Event()
    sleep_cancelled = asyncio.Event()

    async def blocking_sleep(_delay: float) -> None:
        sleeping.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sleep_cancelled.set()
            raise

    await api.polls.put(WeChatRetryableError("getUpdates", "transport"))

    async def handler(_adapter: WeChatAdapter, _message: InboundMessage) -> None:
        raise AssertionError("empty poll reached router")

    adapter, _runtime_api, _storage = await _start_runtime_adapter(
        tmp_path,
        handler,
        api=api,
        adapter_kwargs={"sleep": blocking_sleep},
    )
    await sleeping.wait()
    await adapter.stop()

    assert sleep_cancelled.is_set()
    assert api.closed


async def test_permanent_poll_failure_is_not_retried(tmp_path: Path) -> None:
    api = RuntimeAPIStub()
    await api.polls.put(WeChatProtocolError("malformed getUpdates response"))

    async def handler(_adapter: WeChatAdapter, _message: InboundMessage) -> None:
        raise AssertionError("failed poll reached router")

    adapter, runtime_api, _storage = await _start_runtime_adapter(
        tmp_path, handler, api=api
    )
    try:
        with pytest.raises(WeChatProtocolError, match="malformed"):
            await adapter.wait()
    finally:
        await adapter.stop()

    assert len(runtime_api.poll_calls) == 1


async def test_stale_poll_terminates_with_relogin_guidance(tmp_path: Path) -> None:
    api = RuntimeAPIStub()
    await api.polls.put(WeChatAuthenticationExpired("getUpdates"))

    async def handler(_adapter: WeChatAdapter, _message: InboundMessage) -> None:
        raise AssertionError("stale poll reached router")

    adapter, runtime_api, _storage = await _start_runtime_adapter(
        tmp_path, handler, api=api
    )
    try:
        with pytest.raises(WeChatAuthenticationExpired, match="login --replace"):
            await adapter.wait()
    finally:
        await adapter.stop()

    assert len(runtime_api.poll_calls) == 1


async def test_send_requires_current_context_and_deferred_features_fail_closed(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    api = RuntimeAPIStub()
    conversation = ConversationRef("wechat", "bot-one@im.bot", "user-one")
    adapter = WeChatAdapter(
        "bot-one@im.bot",
        frozenset({"user-one"}),
        api=api,
        storage=storage,
    )

    with pytest.raises(WeChatProtocolError, match="context"):
        await adapter.send_text(conversation, "hello")
    with pytest.raises(WeChatUnsupportedOperation, match="edited"):
        await adapter.edit_text(
            MessageRef(conversation, "message-one"), "edit"
        )
    with pytest.raises(WeChatProtocolError, match="context"):
        await adapter.send_file(
            conversation,
            OutboundFile("one.txt", b"one", "text/plain"),
        )
    assert api.sends == []

    state = WeChatRuntimeState(
        context_tokens={("bot-one@im.bot", "user-one"): "CURRENT_CONTEXT"}
    )
    storage.save_runtime_state(state)
    adapter = WeChatAdapter(
        "bot-one@im.bot",
        frozenset({"user-one"}),
        api=api,
        storage=storage,
    )
    message = await adapter.present_interaction(
        conversation,
        ApprovalPrompt(
            interaction_id="interaction-one",
            request=ApprovalRequest(
                id="request-one",
                session_id="session-one",
                tool_name="shell",
                action="run",
            ),
            session_title="Session",
            workspace="/workspace",
        ),
    )
    await adapter.finish_interaction(
        message, InteractionOutcome(state="cancelled", detail="cancelled")
    )
    assert api.sends[-1]["context_token"] == "CURRENT_CONTEXT"
    assert "normal message" in api.sends[-1]["text"]


async def test_formatter_state_spans_chunks_and_resets_after_finalization(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_runtime_state(
        WeChatRuntimeState(
            context_tokens={("bot-one@im.bot", "user-one"): "CURRENT_CONTEXT"}
        )
    )
    api = RuntimeAPIStub()
    adapter = WeChatAdapter(
        "bot-one@im.bot",
        frozenset({"user-one"}),
        api=api,
        storage=storage,
    )
    conversation = ConversationRef("wechat", "bot-one@im.bot", "user-one")
    first_chunk = "````\n" + "x" * (adapter.message_limit - len("````\n"))
    second_chunk = (
        "~~inside~~ [link](https://example.test)\n"
        "```\n"
        "~~~~\n"
        "````\n"
    )

    await adapter.send_text(conversation, first_chunk)
    await adapter.send_final_text(conversation, second_chunk)
    await adapter.send_final_text(conversation, "~~next response~~")
    await adapter.stop()

    assert len(first_chunk) == 4000
    assert [send["text"] for send in api.sends] == [
        first_chunk,
        second_chunk,
        "next response",
    ]


async def test_notice_text_preserves_active_formatter_and_typing(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_runtime_state(
        WeChatRuntimeState(
            context_tokens={("bot-one@im.bot", "user-one"): "CURRENT_CONTEXT"}
        )
    )
    api = RuntimeAPIStub()
    api.typing_ticket = "TYPING_TICKET_SECRET"
    adapter = WeChatAdapter(
        "bot-one@im.bot",
        frozenset({"user-one"}),
        api=api,
        storage=storage,
    )
    conversation = ConversationRef("wechat", "bot-one@im.bot", "user-one")
    adapter._start_typing(conversation, "CURRENT_CONTEXT")
    await _wait_until(
        lambda: any(
            status == TYPING_STATUS_ACTIVE
            for _user, _ticket, status in api.typing_calls
        )
    )

    await adapter.send_text(conversation, "````\n")
    await adapter.send_notice_text(conversation, "Kimi warning")
    assert conversation in adapter._typing_tasks
    await adapter.send_final_text(conversation, "# still code\n````\n")
    await _wait_until(
        lambda: any(
            status == TYPING_STATUS_CANCEL
            for _user, _ticket, status in api.typing_calls
        )
    )
    await adapter.stop()

    assert [send["text"] for send in api.sends] == [
        "````\n",
        "Kimi warning",
        "# still code\n````\n",
    ]


async def test_empty_final_text_is_noop_and_finishes_typing(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_runtime_state(
        WeChatRuntimeState(
            context_tokens={("bot-one@im.bot", "user-one"): "CURRENT_CONTEXT"}
        )
    )
    api = RuntimeAPIStub()
    api.typing_ticket = "TYPING_TICKET_SECRET"
    adapter = WeChatAdapter(
        "bot-one@im.bot",
        frozenset({"user-one"}),
        api=api,
        storage=storage,
    )
    conversation = ConversationRef("wechat", "bot-one@im.bot", "user-one")
    adapter._start_typing(conversation, "CURRENT_CONTEXT")
    await _wait_until(
        lambda: any(
            status == TYPING_STATUS_ACTIVE
            for _user, _ticket, status in api.typing_calls
        )
    )

    await adapter.send_text(conversation, "```\n")
    message = await adapter.send_final_text(conversation, "")
    await adapter.send_text(conversation, "~~next response~~")
    await _wait_until(
        lambda: any(
            status == TYPING_STATUS_CANCEL
            for _user, _ticket, status in api.typing_calls
        )
    )
    await adapter.stop()

    assert message.conversation == conversation
    assert message.message_id
    assert [send["text"] for send in api.sends] == ["```\n", "next response"]


@pytest.mark.parametrize("failed", [False, True])
async def test_send_file_finishes_typing_after_success_or_failure(
    tmp_path: Path, failed: bool
) -> None:
    class FileAPI(RuntimeAPIStub):
        def __init__(self) -> None:
            super().__init__()
            self.typing_ticket = "TYPING_TICKET_SECRET"
            self.media_sends: list[dict[str, object]] = []

        async def send_media(self, **kwargs: object) -> None:
            self.media_sends.append(kwargs)

    class FileMedia:
        async def upload_file(
            self, _file: OutboundFile, *, to_user_id: str
        ) -> tuple[object, object]:
            assert to_user_id == "user-one"
            if failed:
                raise WeChatMediaError("upload failed")

            class Classification:
                message_item_type = 4
                name = "one.txt"

            return Classification(), object()

        async def close(self) -> None:
            return None

    async def yield_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    storage = WeChatStorage(tmp_path / "wechat")
    storage.save_runtime_state(
        WeChatRuntimeState(
            context_tokens={("bot-one@im.bot", "user-one"): "CURRENT_CONTEXT"}
        )
    )
    api = FileAPI()
    adapter = WeChatAdapter(
        "bot-one@im.bot",
        frozenset({"user-one"}),
        api=api,
        media=FileMedia(),
        storage=storage,
        sleep=yield_sleep,
    )
    conversation = ConversationRef("wechat", "bot-one@im.bot", "user-one")
    adapter._start_typing(conversation, "CURRENT_CONTEXT")
    await _wait_until(
        lambda: any(
            status == TYPING_STATUS_ACTIVE
            for _user, _ticket, status in api.typing_calls
        )
    )

    if failed:
        with pytest.raises(WeChatMediaError, match="upload failed"):
            await adapter.send_file(
                conversation, OutboundFile("one.txt", b"one", "text/plain")
            )
    else:
        await adapter.send_file(
            conversation, OutboundFile("one.txt", b"one", "text/plain")
        )
        assert api.media_sends
    await _wait_until(
        lambda: any(
            status == TYPING_STATUS_CANCEL
            for _user, _ticket, status in api.typing_calls
        )
    )
    await adapter.stop()


async def test_typing_fetch_refresh_cancel_and_safe_retries(
    tmp_path: Path,
) -> None:
    api = RuntimeAPIStub()
    api.typing_ticket = "TYPING_TICKET_SECRET"
    await api.config_results.put(WeChatRetryableError("getConfig", "transport"))
    await api.typing_results.put(WeChatRetryableError("sendTyping", "transport"))
    release = asyncio.Event()
    entered = asyncio.Event()
    delays: list[float] = []

    async def fast_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    async def handler(adapter: WeChatAdapter, message: InboundMessage) -> None:
        entered.set()
        await release.wait()
        await adapter.send_final_text(message.conversation, "done")

    adapter, runtime_api, storage = await _start_runtime_adapter(
        tmp_path,
        handler,
        api=api,
        adapter_kwargs={
            "sleep": fast_sleep,
            "safe_retry_initial_seconds": 0.25,
            "safe_retry_max_seconds": 0.5,
            "typing_refresh_seconds": 5.0,
        },
    )
    work = asyncio.create_task(
        adapter.handle_poll_result(
            WeChatPollResult(
                messages=(
                    _runtime_event(
                        message_id=901,
                        context_token="LATEST_CONTEXT_SECRET",
                    ),
                ),
                get_updates_buf="TYPING_CURSOR",
            )
        )
    )
    try:
        await entered.wait()
        await _wait_until(
            lambda: sum(
                status == TYPING_STATUS_ACTIVE
                for _user, _ticket, status in runtime_api.typing_calls
            )
            >= 2
        )
        release.set()
        await work
        await _wait_until(
            lambda: any(
                status == TYPING_STATUS_CANCEL
                for _user, _ticket, status in runtime_api.typing_calls
            )
        )
    finally:
        release.set()
        if not work.done():
            await work
        await adapter.stop()

    assert runtime_api.config_calls == [
        ("user-one", "LATEST_CONTEXT_SECRET"),
        ("user-one", "LATEST_CONTEXT_SECRET"),
    ]
    assert delays[:2] == [0.25, 0.25]
    assert runtime_api.typing_calls[0][2] == TYPING_STATUS_ACTIVE
    assert runtime_api.typing_calls[-1][2] == TYPING_STATUS_CANCEL
    assert storage.load_runtime_state().get_updates_buf == "TYPING_CURSOR"


async def test_typing_cancel_follows_an_inflight_active_request(
    tmp_path: Path,
) -> None:
    class TypingRaceAPI(RuntimeAPIStub):
        def __init__(self) -> None:
            super().__init__()
            self.typing_ticket = "TYPING_TICKET_SECRET"
            self.active_started = asyncio.Event()
            self.active_cancelled = asyncio.Event()
            self.release_active = asyncio.Event()
            self.cancel_started = asyncio.Event()
            self.completed_statuses: list[int] = []

        async def send_typing(
            self,
            *,
            ilink_user_id: str,
            typing_ticket: str,
            status: int,
        ) -> None:
            self.typing_calls.append((ilink_user_id, typing_ticket, status))
            if status == TYPING_STATUS_CANCEL:
                self.cancel_started.set()
                self.completed_statuses.append(status)
                return
            self.active_started.set()
            try:
                await self.release_active.wait()
            except asyncio.CancelledError:
                self.active_cancelled.set()
                await self.release_active.wait()
                self.completed_statuses.append(status)
                raise
            self.completed_statuses.append(status)

    api = TypingRaceAPI()

    async def handler(adapter: WeChatAdapter, message: InboundMessage) -> None:
        await api.active_started.wait()
        await adapter.send_final_text(message.conversation, "done")

    adapter, _runtime_api, _storage = await _start_runtime_adapter(
        tmp_path,
        handler,
        api=api,
    )
    work = asyncio.create_task(
        adapter.handle_poll_result(
            WeChatPollResult(
                messages=(_runtime_event(message_id=904),),
                get_updates_buf="CURSOR",
            )
        )
    )
    try:
        await api.active_cancelled.wait()
        await work
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not api.cancel_started.is_set()
        api.release_active.set()
        await _wait_until(lambda: len(api.completed_statuses) == 2)
    finally:
        api.release_active.set()
        if not work.done():
            await work
        await adapter.stop()

    assert api.completed_statuses == [
        TYPING_STATUS_ACTIVE,
        TYPING_STATUS_CANCEL,
    ]


async def test_stop_awaits_cleanup_created_by_poll_cancellation(
    tmp_path: Path,
) -> None:
    class OrderedAPI(RuntimeAPIStub):
        def __init__(self) -> None:
            super().__init__()
            self.typing_ticket = "TYPING_TICKET_SECRET"
            self.events: list[str] = []

        async def send_typing(
            self,
            *,
            ilink_user_id: str,
            typing_ticket: str,
            status: int,
        ) -> None:
            self.events.append(
                "cancel" if status == TYPING_STATUS_CANCEL else "active"
            )
            await super().send_typing(
                ilink_user_id=ilink_user_id,
                typing_ticket=typing_ticket,
                status=status,
            )

        async def close(self) -> None:
            self.events.append("close")
            await super().close()

    api = OrderedAPI()
    handler_started = asyncio.Event()

    async def yield_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    async def handler(_adapter: WeChatAdapter, _message: InboundMessage) -> None:
        handler_started.set()
        await asyncio.Event().wait()

    adapter, _runtime_api, _storage = await _start_runtime_adapter(
        tmp_path,
        handler,
        api=api,
        adapter_kwargs={"sleep": yield_sleep},
    )
    await api.polls.put(
        WeChatPollResult(
            messages=(_runtime_event(message_id=905),),
            get_updates_buf="STOP_CURSOR",
        )
    )
    await handler_started.wait()
    await _wait_until(
        lambda: any(status == TYPING_STATUS_ACTIVE for _user, _ticket, status in api.typing_calls)
    )

    await adapter.stop()

    assert api.events[-1] == "close"
    close_index = api.events.index("close")
    assert all(event != "cancel" or index < close_index for index, event in enumerate(api.events))


async def test_typing_failure_never_blocks_handler_or_cursor_commit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    api = RuntimeAPIStub()
    for _ in range(3):
        await api.config_results.put(
            WeChatRetryableError("getConfig", "transport")
        )
    handled = asyncio.Event()

    async def handler(_adapter: WeChatAdapter, _message: InboundMessage) -> None:
        handled.set()

    adapter, runtime_api, storage = await _start_runtime_adapter(
        tmp_path,
        handler,
        api=api,
        adapter_kwargs={"sleep": _no_sleep},
    )
    try:
        await adapter.handle_poll_result(
            WeChatPollResult(
                messages=(
                    _runtime_event(
                        message_id=902,
                        context_token="TYPING_CONTEXT_SECRET",
                    ),
                ),
                get_updates_buf="FAILURE_CURSOR_SECRET",
            )
        )
        await handled.wait()
        await _wait_until(lambda: len(runtime_api.config_calls) == 3)
    finally:
        await adapter.stop()

    assert storage.load_runtime_state().get_updates_buf == "FAILURE_CURSOR_SECRET"
    assert "best-effort failure" in caplog.text
    assert "TYPING_CONTEXT_SECRET" not in caplog.text
    assert "FAILURE_CURSOR_SECRET" not in caplog.text


async def test_stale_typing_terminates_the_adapter(tmp_path: Path) -> None:
    api = RuntimeAPIStub()
    await api.config_results.put(WeChatAuthenticationExpired("getConfig"))

    async def handler(_adapter: WeChatAdapter, _message: InboundMessage) -> None:
        return None

    adapter, runtime_api, _storage = await _start_runtime_adapter(
        tmp_path, handler, api=api
    )
    try:
        await adapter.handle_poll_result(
            WeChatPollResult(
                messages=(_runtime_event(message_id=903),),
                get_updates_buf="CURSOR",
            )
        )
        with pytest.raises(WeChatAuthenticationExpired, match="login --replace"):
            await adapter.wait()
    finally:
        await adapter.stop()

    assert len(runtime_api.poll_calls) == 1


def test_whole_message_formatter_preserves_code_and_readable_links() -> None:
    image_target = "https://signed.example/IMAGE_TARGET_SECRET"
    rendered = sanitize_markdown(
        "> quoted\n"
        "##### Heading\n"
        "~~removed markers~~ and [docs](https://docs.example/path)\n"
        f"![diagram]({image_target})\n"
        "`~~inline code~~`\n"
        "```python\n"
        "> # ~~fenced code~~\n"
        "```\n"
    )

    assert rendered.startswith("quoted\nHeading\n")
    assert "removed markers" in rendered
    assert "~~removed markers~~" not in rendered
    assert "docs (https://docs.example/path)" in rendered
    assert "diagram" in rendered
    assert image_target not in rendered
    assert "`~~inline code~~`" in rendered
    assert "```python\n> # ~~fenced code~~\n```" in rendered


def test_formatter_supports_matching_backtick_and_tilde_fences() -> None:
    text = (
        "````python\n"
        "triple ``` stays code\n"
        "~~~ stays code\n"
        "```\n"
        "````\n"
        "~~visible~~\n"
        "~~~text\n"
        "triple ``` stays code\n"
        "~~~~\n"
        "~~visible again~~\n"
    )

    assert sanitize_markdown(text) == (
        "````python\n"
        "triple ``` stays code\n"
        "~~~ stays code\n"
        "```\n"
        "````\n"
        "visible\n"
        "~~~text\n"
        "triple ``` stays code\n"
        "~~~~\n"
        "visible again\n"
    )


def test_formatter_preserves_escaped_and_incomplete_images_and_links() -> None:
    image_url = "https://images.example/diagram.png"
    link_url = "https://docs.example/path"
    text = "\n".join(
        (
            rf"\![escaped]({image_url})",
            rf"\\![active]({image_url})",
            rf"![incomplete]({image_url}",
            rf"\[escaped]({link_url})",
            rf"\\[active]({link_url})",
            rf"[incomplete]({link_url}",
        )
    )

    assert sanitize_markdown(text) == "\n".join(
        (
            rf"\![escaped]({image_url})",
            r"\\active",
            rf"![incomplete]({image_url}",
            rf"\[escaped]({link_url})",
            rf"\\active ({link_url})",
            rf"[incomplete]({link_url}",
        )
    )


def test_formatter_only_removes_matched_unescaped_strikethrough() -> None:
    text = "\n".join(
        (
            "~~matched~~",
            "keep ~~unmatched",
            r"\~~escaped~~",
            r"~~open \~~ inner~~",
            "`~~inline code~~`",
        )
    )

    assert sanitize_markdown(text) == "\n".join(
        (
            "matched",
            "keep ~~unmatched",
            r"\~~escaped~~",
            r"open \~~ inner",
            "`~~inline code~~`",
        )
    )


async def test_lifecycle_honors_bounded_timeout_and_cancels_promptly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    api = RuntimeAPIStub()
    api.notify_start_result = WeChatAPIResult(ret=7)
    api.notify_stop_error = RuntimeError("stop failed")
    await api.polls.put(
        WeChatPollResult(
            messages=(),
            get_updates_buf="",
            long_poll_timeout_seconds=0.01,
        )
    )

    async def handler(_adapter: WeChatAdapter, _message: InboundMessage) -> None:
        raise AssertionError("empty poll reached the router")

    adapter, runtime_api, _storage = await _start_runtime_adapter(
        tmp_path, handler, api=api
    )
    await _wait_until(lambda: len(runtime_api.poll_calls) >= 2)
    wait_task = asyncio.create_task(adapter.wait())
    await adapter.stop()

    with pytest.raises(asyncio.CancelledError):
        await wait_task
    assert runtime_api.poll_calls[0] == (
        "",
        DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    )
    assert runtime_api.poll_calls[1] == ("", 1.0)
    assert runtime_api.poll_cancelled.is_set()
    assert runtime_api.notify_calls == ["start", "stop"]
    assert runtime_api.closed
    assert "ret=7" in caplog.text
    assert "notifyStop failed" in caplog.text


async def test_lifecycle_retries_safe_notifications_and_closes_once(
    tmp_path: Path,
) -> None:
    api = RuntimeAPIStub()
    for operation in ("notifyStart", "notifyStart"):
        await api.notify_start_results.put(
            WeChatRetryableError(operation, "transport")
        )
    await api.notify_start_results.put(WeChatAPIResult())
    for operation in ("notifyStop", "notifyStop"):
        await api.notify_stop_results.put(
            WeChatRetryableError(operation, "transport")
        )
    await api.notify_stop_results.put(WeChatAPIResult())

    async def handler(_adapter: WeChatAdapter, _message: InboundMessage) -> None:
        raise AssertionError("unexpected message")

    adapter, runtime_api, _storage = await _start_runtime_adapter(
        tmp_path,
        handler,
        api=api,
        adapter_kwargs={"sleep": _no_sleep},
    )
    await adapter.stop()
    await adapter.stop()

    assert runtime_api.notify_calls == [
        "start",
        "start",
        "start",
        "stop",
        "stop",
        "stop",
    ]
    assert runtime_api.close_calls == 1
