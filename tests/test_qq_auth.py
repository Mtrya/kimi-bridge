from __future__ import annotations

import asyncio
import base64
import json
import os
from collections import deque
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest

from kimi_bridge.platforms.qq.auth import (
    QQ_BIND_TASK_URL,
    QQ_POLL_BIND_URL,
    QQControlError,
    QQRegistrationError,
    QQRegistrationResult,
    authorize_with_qr,
    build_connect_url,
    decrypt_app_secret,
    encrypt_app_secret,
    generate_qr_key,
    run_login,
    run_logout,
    run_status,
)
from kimi_bridge.platforms.qq.storage import (
    CREDENTIAL_FILE_NAME,
    QQManagedCredential,
    QQStorage,
    QQStorageError,
    redact_app_id,
    redact_openid,
)

requires_posix_modes = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file modes are not enforceable here"
)

KEY = "cU2zfw25tJTH6Xj37zbaLWCB5+cJsUn11T8IYFqalAo="
WRONG_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
ENCRYPTED_SECRET = "9/x+lG2Kq/Ok60mk5QElzSlidmhKAt0VMTjMiE1lHq0aZJ8PgLjpPl5pSaegeYrekEpq3kv1l7o/V3Pn"
APP_SECRET = "MY_KNOWN_VECTOR_APP_SECRET_12345"
APP_ID = "1234567890"
SCANNER_OPENID = "OPENID-SCANNER"
AUTHORIZED_AT = "2026-08-08T12:00:00+00:00"


@dataclass(frozen=True, slots=True)
class QQAuthConfigStub:
    storage_path: Path
    app_id: str = ""
    app_secret: str = ""


def _completed(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "retcode": 0,
        "data": {
            "status": 2,
            "bot_appid": APP_ID,
            "bot_encrypt_secret": ENCRYPTED_SECRET,
            "user_openid": SCANNER_OPENID,
        },
    }
    payload.update(overrides)
    return payload


def _pending(status: int = 1) -> dict[str, Any]:
    return {"retcode": 0, "data": {"status": status}}


def _credential(
    app_secret: str = APP_SECRET, app_id: str = APP_ID
) -> QQManagedCredential:
    return QQManagedCredential(
        app_id=app_id,
        app_secret=app_secret,
        authorized_at=AUTHORIZED_AT,
    )


class BindServer:
    """MockTransport handler scripting the q.qq.com bind contract."""

    def __init__(
        self,
        statuses: list[dict[str, Any] | BaseException],
        *,
        task_id: str = "TASK-1",
    ) -> None:
        self.statuses = deque(statuses)
        self.task_id = task_id
        self.requests: list[httpx.Request] = []
        self.create_count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == QQ_BIND_TASK_URL:
            self.create_count += 1
            return httpx.Response(
                200, json={"retcode": 0, "data": {"task_id": self.task_id}}
            )
        if str(request.url) == QQ_POLL_BIND_URL:
            item = (
                self.statuses.popleft()
                if self.statuses
                else {"retcode": 0, "data": {"status": 1}}
            )
            if isinstance(item, BaseException):
                raise item
            return httpx.Response(200, json=item)
        raise AssertionError(f"unexpected path: {request.url}")


async def _no_sleep(_delay: float) -> None:
    return None


async def _authorize(
    server: BindServer,
    storage: QQStorage,
    **kwargs: Any,
) -> QQRegistrationResult:
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        return await authorize_with_qr(
            storage,
            stream=kwargs.pop("stream", StringIO()),
            http_client=client,
            sleep=kwargs.pop("sleep", _no_sleep),
            authorized_at=kwargs.pop("authorized_at", lambda: AUTHORIZED_AT),
            key_factory=kwargs.pop("key_factory", lambda: KEY),
            **kwargs,
        )


def _write_credential_json(storage: QQStorage, payload: dict[str, Any]) -> None:
    storage.path.mkdir(mode=0o700, parents=True)
    storage.credential_path.write_text(
        json.dumps(payload), encoding="utf-8"
    )
    storage.credential_path.chmod(0o600)


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------


def test_decrypt_app_secret_known_vector() -> None:
    assert decrypt_app_secret(KEY, ENCRYPTED_SECRET) == APP_SECRET


def test_encrypt_decrypt_app_secret_roundtrip() -> None:
    encrypted = encrypt_app_secret(KEY, APP_SECRET)
    assert decrypt_app_secret(KEY, encrypted) == APP_SECRET
    assert len(encrypted) >= 28


def test_decrypt_app_secret_failures_are_stable_and_secret_safe() -> None:
    for key, encrypted in [
        (KEY, "AAAA"),  # payload too short
        (KEY, "%%%not-base64%%%"),
        ("AAAA", ENCRYPTED_SECRET),  # key not 32 bytes
        ("AAAA%", ENCRYPTED_SECRET),  # key not valid base64
        (KEY, ENCRYPTED_SECRET[:-4] + "AAAA"),  # bad auth tag
    ]:
        with pytest.raises(QQRegistrationError, match="could not be decrypted"):
            decrypt_app_secret(key, encrypted)


def test_decrypt_app_secret_wrong_key_fails() -> None:
    with pytest.raises(QQRegistrationError, match="could not be decrypted"):
        decrypt_app_secret(WRONG_KEY, ENCRYPTED_SECRET)


def test_generate_qr_key_is_32_random_bytes() -> None:
    first = generate_qr_key()
    second = generate_qr_key()
    assert len(base64.b64decode(first)) == 32
    assert first != second


# ---------------------------------------------------------------------------
# QR URL and HTTP contract
# ---------------------------------------------------------------------------


def test_build_connect_url_encodes_params() -> None:
    assert (
        build_connect_url("task id/1", "kimi-bridge")
        == "https://q.qq.com/qqbot/openclaw/connect.html"
        "?task_id=task%20id%2F1&source=kimi-bridge&_wv=2"
    )


async def test_create_bind_task_request_and_qr_url(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")
    output = StringIO()
    server = BindServer([_completed()])

    result = await _authorize(server, storage, stream=output, source="kimi-bridge")

    create_request = server.requests[0]
    assert create_request.method == "POST"
    assert str(create_request.url) == QQ_BIND_TASK_URL
    assert json.loads(create_request.content) == {"key": KEY}
    assert server.create_count == 1
    assert result.credential == storage.load_credential()

    rendered = output.getvalue()
    assert "QQ authorization URL:\n" in rendered
    assert (
        "https://q.qq.com/qqbot/openclaw/connect.html?task_id=TASK-1"
        "&source=kimi-bridge&_wv=2" in rendered
    )
    assert KEY not in rendered
    assert APP_SECRET not in rendered


async def test_poll_pending_then_completed_persists_credential(
    tmp_path: Path,
) -> None:
    storage = QQStorage(tmp_path / "qq")
    server = BindServer([_pending(0), _pending(1), _completed()])
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = await _authorize(
        server, storage, sleep=record_sleep, poll_interval_seconds=2.0
    )

    assert delays == [2.0, 2.0]
    assert result.user_openid == SCANNER_OPENID
    payload = json.loads(storage.credential_path.read_text(encoding="utf-8"))
    assert set(payload) == {"version", "app_id", "app_secret", "authorized_at"}
    assert payload["app_id"] == APP_ID
    assert payload["app_secret"] == APP_SECRET
    assert payload["authorized_at"] == AUTHORIZED_AT
    assert SCANNER_OPENID not in storage.credential_path.read_text(encoding="utf-8")
    assert storage.credential_path.read_text(encoding="utf-8").count(KEY) == 0


async def test_poll_network_errors_retry_with_capped_backoff(
    tmp_path: Path,
) -> None:
    storage = QQStorage(tmp_path / "qq")
    server = BindServer(
        [
            httpx.ConnectError("one", request=httpx.Request("POST", QQ_POLL_BIND_URL)),
            httpx.ConnectError("two", request=httpx.Request("POST", QQ_POLL_BIND_URL)),
            _pending(1),
            _completed(),
        ]
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = await _authorize(
        server,
        storage,
        sleep=record_sleep,
        poll_interval_seconds=2.0,
        max_poll_retry_delay_seconds=8.0,
    )

    assert result.credential.app_secret == APP_SECRET
    assert delays == [2.0, 4.0, 2.0]


async def test_poll_retcode_error_is_retried(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")
    server = BindServer(
        [
            {"retcode": 1101, "msg": "temporary"},
            _completed(),
        ]
    )

    result = await _authorize(server, storage)

    assert result.credential.app_id == APP_ID
    assert storage.load_credential().app_secret == APP_SECRET


async def test_completed_missing_fields_fail_closed(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")

    with pytest.raises(QQRegistrationError, match="missing the bot app_id"):
        await _authorize(
            BindServer([_completed(data={"status": 2, "bot_encrypt_secret": ENCRYPTED_SECRET})]),
            storage,
        )
    assert not storage.has_credential()

    with pytest.raises(QQRegistrationError, match="missing the bot secret"):
        await _authorize(
            BindServer([_completed(data={"status": 2, "bot_appid": APP_ID})]),
            storage,
        )
    assert not storage.has_credential()


async def test_unknown_status_and_malformed_responses_fail_closed(
    tmp_path: Path,
) -> None:
    storage = QQStorage(tmp_path / "qq")

    with pytest.raises(QQRegistrationError, match="malformed"):
        await _authorize(BindServer([_pending(7)]), storage)
    assert not storage.has_credential()

    server = BindServer([{"retcode": 0, "data": {"status": "2"}}])
    with pytest.raises(QQRegistrationError, match="malformed"):
        await _authorize(server, storage)
    assert not storage.has_credential()

    class InvalidSecretServer(BindServer):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if str(request.url) == QQ_POLL_BIND_URL:
                return httpx.Response(200, text="not json")
            return super().__call__(request)

    with pytest.raises(QQRegistrationError, match="not valid JSON"):
        await _authorize(InvalidSecretServer([_completed()]), storage)


async def test_user_openid_is_optional(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")
    server = BindServer([_completed(data={"status": 2, "bot_appid": APP_ID, "bot_encrypt_secret": ENCRYPTED_SECRET})])

    result = await _authorize(server, storage)

    assert result.user_openid is None


async def test_create_failure_is_fatal(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")

    class FailingCreateServer(BindServer):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if str(request.url) == QQ_BIND_TASK_URL:
                return httpx.Response(
                    200, json={"retcode": 1100, "msg": "rejected"}
                )
            return super().__call__(request)

    with pytest.raises(QQRegistrationError, match="retcode 1100"):
        await _authorize(FailingCreateServer([_completed()]), storage)
    assert not storage.has_credential()


# ---------------------------------------------------------------------------
# Replace semantics, timeout, cancellation
# ---------------------------------------------------------------------------


async def test_existing_credential_blocks_login_without_replace(
    tmp_path: Path,
) -> None:
    storage = QQStorage(tmp_path / "qq")
    storage.save_credential(_credential())

    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent when blocked")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(QQControlError, match="already exists"):
            await authorize_with_qr(
                storage,
                stream=StringIO(),
                http_client=client,
                sleep=_no_sleep,
            )


async def test_replace_allows_overwrite(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")
    storage.save_credential(_credential(app_secret="OLD_SECRET"))
    output = StringIO()

    result = await _authorize(
        BindServer([_completed()]), storage, replace=True, stream=output
    )

    assert result.credential.app_secret == APP_SECRET
    assert storage.load_credential().app_secret == APP_SECRET
    assert "OLD_SECRET" not in storage.credential_path.read_text(encoding="utf-8")


async def test_decryption_failure_does_not_overwrite_existing(
    tmp_path: Path,
) -> None:
    storage = QQStorage(tmp_path / "qq")
    storage.save_credential(_credential(app_secret="ORIGINAL_SECRET"))
    server = BindServer(
        [
            _completed(
                data={
                    "status": 2,
                    "bot_appid": APP_ID,
                    "bot_encrypt_secret": "AAAA",
                }
            )
        ]
    )

    with pytest.raises(QQRegistrationError, match="could not be decrypted"):
        await _authorize(server, storage, replace=True)

    assert storage.load_credential().app_secret == "ORIGINAL_SECRET"


async def test_expired_refreshes_qr_then_completes(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")
    output = StringIO()

    result = await _authorize(
        BindServer([_pending(3), _completed()]),
        storage,
        stream=output,
        max_qr_attempts=2,
    )

    assert result.credential.app_id == APP_ID
    assert "QR code expired; refreshing." in output.getvalue()
    assert "Refreshed QQ authorization URL:\n" in output.getvalue()
    assert output.getvalue().count("QQ authorization URL") == 2


async def test_expired_stops_after_repeated_refreshes(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")

    with pytest.raises(QQControlError, match="stopped after repeated refreshes"):
        await _authorize(
            BindServer([_pending(3), _pending(3)]),
            storage,
            max_qr_attempts=2,
        )
    assert not storage.has_credential()


async def test_timeout_does_not_write_credential(tmp_path: Path) -> None:
    class AdvancingClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

        async def sleep(self, delay: float) -> None:
            self.value += delay

    storage = QQStorage(tmp_path / "qq")
    clock = AdvancingClock()
    with pytest.raises(QQControlError, match="timed out"):
        await _authorize(
            BindServer([]),
            storage,
            clock=clock,
            sleep=clock.sleep,
            timeout_seconds=2,
        )
    assert not storage.has_credential()


async def test_cancellation_does_not_write_credential(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")
    sleeping = asyncio.Event()

    async def blocked_sleep(_delay: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(BindServer([]))
    ) as client:
        task = asyncio.create_task(
            authorize_with_qr(
                storage,
                stream=StringIO(),
                http_client=client,
                sleep=blocked_sleep,
                key_factory=lambda: KEY,
            )
        )
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not storage.has_credential()


async def test_authorize_validates_parameters(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")

    with pytest.raises(ValueError, match="timeout_seconds"):
        await _authorize(BindServer([]), storage, timeout_seconds=0)
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        await _authorize(BindServer([]), storage, poll_interval_seconds=0)
    with pytest.raises(ValueError, match="max_poll_retry_delay_seconds"):
        await _authorize(
            BindServer([]),
            storage,
            poll_interval_seconds=2.0,
            max_poll_retry_delay_seconds=1.0,
        )
    with pytest.raises(ValueError, match="max_qr_attempts"):
        await _authorize(BindServer([]), storage, max_qr_attempts=0)
    with pytest.raises(ValueError, match="source"):
        await _authorize(BindServer([]), storage, source="")


# ---------------------------------------------------------------------------
# Storage: schema, permissions, hygiene
# ---------------------------------------------------------------------------


def test_save_credential_writes_versioned_schema(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")
    storage.save_credential(_credential())

    payload = json.loads(storage.credential_path.read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "app_id": APP_ID,
        "app_secret": APP_SECRET,
        "authorized_at": AUTHORIZED_AT,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("app_secret", "", "non-empty"),
        ("authorized_at", "not-a-timestamp", "authorization time is invalid"),
        ("authorized_at", "2026-08-08T12:00:00", "must include a timezone"),
    ],
)
def test_save_credential_rejects_invalid_fields(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    storage = QQStorage(tmp_path / "qq")
    credential = replace(_credential(), **{field: value})

    with pytest.raises(QQStorageError, match=message):
        storage.save_credential(credential)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "app_id": "a", "app_secret": "s"},
        {"version": 1, "app_id": "a", "app_secret": "s", "authorized_at": AUTHORIZED_AT, "extra": 1},
        {"version": True, "app_id": "a", "app_secret": "s", "authorized_at": AUTHORIZED_AT},
        {"version": 1, "app_id": "a", "app_secret": 123, "authorized_at": AUTHORIZED_AT},
        {"version": 1, "app_id": "", "app_secret": "s", "authorized_at": AUTHORIZED_AT},
    ],
)
def test_load_credential_rejects_unsupported_schemas(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    storage = QQStorage(tmp_path / "qq")
    _write_credential_json(storage, payload)

    with pytest.raises(QQStorageError, match="unsupported|invalid"):
        storage.load_credential()


def test_load_credential_rejects_unknown_versions_without_exposing_secret(
    tmp_path: Path,
) -> None:
    storage = QQStorage(tmp_path / "qq")
    _write_credential_json(
        storage,
        {
            "version": 999,
            "app_id": "a",
            "app_secret": "FUTURE_SECRET",
            "authorized_at": AUTHORIZED_AT,
        },
    )

    with pytest.raises(QQStorageError) as caught:
        storage.load_credential()

    assert "unsupported" in str(caught.value)
    assert "FUTURE_SECRET" not in str(caught.value)


def test_load_credential_roundtrip_and_secret_safe_repr(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")
    storage.save_credential(_credential())

    assert storage.load_credential() == _credential()
    assert APP_SECRET not in repr(storage.load_credential())
    assert "app_secret" not in repr(storage.load_credential())


@requires_posix_modes
def test_storage_is_atomic_private_and_secret_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kimi_bridge.platforms.qq import storage as storage_module

    storage = QQStorage(tmp_path / "qq")
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
    storage = QQStorage(tmp_path / "qq")
    storage.save_credential(_credential())

    storage.path.chmod(0o755)
    with pytest.raises(QQStorageError, match="storage directory mode must be 700"):
        storage.load_credential()

    storage.path.chmod(0o700)
    storage.credential_path.chmod(0o644)
    with pytest.raises(QQStorageError, match="credential file mode must be 600"):
        storage.load_credential()


@requires_posix_modes
def test_storage_rejects_symlinked_credential_and_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_credential = outside / CREDENTIAL_FILE_NAME
    outside_credential.write_text("{}", encoding="utf-8")

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(QQStorageError, match="unsafe"):
        QQStorage(linked_root).load_credential()
    with pytest.raises(QQStorageError, match="safe directory"):
        QQStorage(linked_root).clear_owned_files()

    storage = QQStorage(tmp_path / "qq")
    storage.path.mkdir(mode=0o700, parents=True)
    storage.credential_path.symlink_to(outside_credential)
    with pytest.raises(QQStorageError, match="unsafe"):
        storage.load_credential()


@requires_posix_modes
def test_storage_rejects_non_regular_credential_file(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")
    storage.path.mkdir(mode=0o700, parents=True)
    storage.credential_path.mkdir()

    with pytest.raises(QQStorageError, match="unsafe"):
        storage.load_credential()


def test_clear_owned_files_removes_only_credentials_json(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")
    storage.save_credential(_credential())
    unrelated = storage.path / "operator-note.txt"
    unrelated.write_text("preserve", encoding="utf-8")

    assert storage.clear_owned_files() == (CREDENTIAL_FILE_NAME,)
    assert unrelated.read_text(encoding="utf-8") == "preserve"
    assert storage.path.is_dir()
    assert storage.clear_owned_files() == ()


@requires_posix_modes
def test_clear_owned_files_unlinks_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("preserve", encoding="utf-8")
    storage = QQStorage(tmp_path / "qq")
    storage.path.mkdir(parents=True)
    storage.credential_path.symlink_to(outside)

    assert storage.clear_owned_files() == (CREDENTIAL_FILE_NAME,)
    assert outside.read_text(encoding="utf-8") == "preserve"
    assert not os.path.lexists(storage.credential_path)


def test_inspect_is_secret_safe(tmp_path: Path) -> None:
    storage = QQStorage(tmp_path / "qq")
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


def test_redact_app_id_and_openid() -> None:
    assert redact_app_id("1234567890") == "1234…7890"
    assert redact_openid("OPENID-SCANNER") == "OPEN…NNER"
    assert redact_app_id("short") == "***"
    assert redact_openid("") == "***"


# ---------------------------------------------------------------------------
# CLI runners
# ---------------------------------------------------------------------------


def test_run_login_prints_next_steps_without_leaking_secrets(
    tmp_path: Path,
) -> None:
    config = QQAuthConfigStub(tmp_path / "qq")
    output = StringIO()
    client = httpx.AsyncClient(transport=httpx.MockTransport(BindServer([_completed()])))
    try:
        run_login(config, stream=output, http_client=client, key_factory=lambda: KEY)
    finally:
        asyncio.run(client.aclose())

    rendered = output.getvalue()
    assert "QQ authorization saved locally." in rendered
    assert "Bot app_id: 1234…7890" in rendered
    assert f"Scanner user openid: {SCANNER_OPENID}" in rendered
    assert "Add the scanner openid to qq.allowed_users." in rendered
    assert APP_SECRET not in rendered
    assert KEY not in rendered
    assert ENCRYPTED_SECRET not in rendered
    assert APP_ID not in rendered
    assert QQStorage(config.storage_path).load_credential().app_secret == APP_SECRET


def test_run_status_is_local_redacted_and_reports_permissions(
    tmp_path: Path,
) -> None:
    empty = QQAuthConfigStub(tmp_path / "empty")
    assert run_status(empty, stream=StringIO()) == 1

    config = QQAuthConfigStub(tmp_path / "qq")
    QQStorage(config.storage_path).save_credential(_credential())
    output = StringIO()
    assert run_status(config, stream=output) == 0
    rendered = output.getvalue()
    assert "Bot app_id: 1234…7890" in rendered
    assert "network status was not checked" in rendered
    assert APP_SECRET not in rendered


def test_run_status_reports_complete_toml_fallback(tmp_path: Path) -> None:
    config = QQAuthConfigStub(
        tmp_path / "qq", app_id=APP_ID, app_secret=APP_SECRET
    )
    output = StringIO()

    assert run_status(config, stream=output) == 0

    rendered = output.getvalue()
    assert "Managed authorization: not present locally." in rendered
    assert "Legacy TOML authorization: present locally for 1234…7890" in rendered
    assert APP_SECRET not in rendered
    assert APP_ID not in rendered


def test_run_status_incomplete_toml_pair_still_fails(tmp_path: Path) -> None:
    config = QQAuthConfigStub(tmp_path / "qq", app_id=APP_ID)
    output = StringIO()

    assert run_status(config, stream=output) == 1

    rendered = output.getvalue()
    assert "Authorization: not configured locally." in rendered
    assert APP_SECRET not in rendered


def test_run_status_managed_credential_takes_precedence_over_toml(
    tmp_path: Path,
) -> None:
    config = QQAuthConfigStub(
        tmp_path / "qq", app_id="TOML-APP-ID", app_secret="TOML-SECRET"
    )
    QQStorage(config.storage_path).save_credential(_credential())
    output = StringIO()

    assert run_status(config, stream=output) == 0

    rendered = output.getvalue()
    assert "Authorization: present locally" in rendered
    assert "TOML-APP-ID" not in rendered
    assert "TOML-SECRET" not in rendered


def test_run_status_reports_corrupt_managed_credential_without_secrets(
    tmp_path: Path,
) -> None:
    config = QQAuthConfigStub(
        tmp_path / "qq", app_id="TOML-APP-ID", app_secret="TOML-SECRET"
    )
    storage = QQStorage(config.storage_path)
    storage.path.mkdir(mode=0o700, parents=True)
    storage.credential_path.write_text("{not-json", encoding="utf-8")
    storage.credential_path.chmod(0o600)
    output = StringIO()

    assert run_status(config, stream=output) == 1

    rendered = output.getvalue()
    assert "Authorization error" in rendered
    assert "TOML-SECRET" not in rendered


def test_run_logout_removes_only_owned_files_and_is_idempotent(
    tmp_path: Path,
) -> None:
    config = QQAuthConfigStub(tmp_path / "qq")
    storage = QQStorage(config.storage_path)
    storage.save_credential(_credential())
    unrelated = storage.path / "operator-note.txt"
    unrelated.write_text("preserve", encoding="utf-8")

    output = StringIO()
    assert run_logout(config, stream=output) == 0
    assert CREDENTIAL_FILE_NAME in output.getvalue()
    assert not storage.has_credential()
    assert unrelated.read_text(encoding="utf-8") == "preserve"
    assert storage.path.is_dir()

    assert run_logout(config, stream=StringIO()) == 0
