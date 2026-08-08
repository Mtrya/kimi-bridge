from __future__ import annotations

import asyncio
import base64
import json
import os
from collections import deque
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest

from kimi_bridge.config import WechatConfig
from kimi_bridge.platforms.wechat import (
    WeChatAPIError,
    WeChatControlError,
    WeChatCredential,
    WeChatStorage,
    WeChatStorageError,
    authorize_with_qr,
    run_logout,
    run_status,
)
from kimi_bridge.platforms.wechat.api import (
    WeChatAuthAPI,
    build_base_info,
    normalize_https_base_url,
)
from kimi_bridge.platforms.wechat.storage import RUNTIME_STATE_FILE_NAME
from kimi_bridge.platforms.wechat.types import (
    ILINK_APP_CLIENT_VERSION,
    PINNED_SOURCE_COMMIT,
    PINNED_SOURCE_TAG,
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


async def test_malformed_and_failed_responses_do_not_expose_bodies(
    tmp_path: Path,
) -> None:
    secret = "DO_NOT_PRINT_RAW_RESPONSE_SECRET"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_bot_qrcode"):
            return httpx.Response(500, text=secret)
        raise AssertionError("unexpected request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WeChatAPIError) as caught:
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


def test_storage_rejects_unknown_versions_without_exposing_token(
    tmp_path: Path,
) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    storage.path.mkdir(parents=True)
    storage.credential_path.write_text(
        '{"version": 999, "bot_token": "FUTURE_TOKEN_SECRET"}\n',
        encoding="utf-8",
    )

    with pytest.raises(WeChatStorageError) as caught:
        storage.load_credential()

    assert "unsupported" in str(caught.value)
    assert "FUTURE_TOKEN_SECRET" not in str(caught.value)


def test_status_is_local_redacted_and_reports_permissions(tmp_path: Path) -> None:
    storage = WeChatStorage(tmp_path / "wechat")
    credential = _credential()
    storage.save_credential(credential)
    output = StringIO()

    assert (
        run_status(
            WechatConfig(storage_path=storage.path),
            stream=output,
            platform_name="linux",
        )
        == 0
    )
    rendered = output.getvalue()
    assert credential.bot_token not in rendered
    assert credential.bot_id not in rendered
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
