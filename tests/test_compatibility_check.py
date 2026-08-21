from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from kimi_bridge.compatibility import (
    COMPATIBILITY_MAP,
    SUPPORTED_KIMI_CODE_VERSIONS,
    kimi_code_version_sort_key,
)
from kimi_bridge.kimi_server import KimiContractCheck
from kimi_bridge.kimi_server import contract as kimi_contract
from kimi_bridge.kimi_server import probe as probe_module
from kimi_bridge.kimi_server.types import SessionStatus


def _load_checker() -> Any:
    script = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "check_kimi_compatibility.py"
    )
    spec = importlib.util.spec_from_file_location(
        "check_kimi_compatibility", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()

AUTOMATION_BRANCH = checker.AUTOMATION_BRANCH
CANARY_PLATFORMS = checker.CANARY_PLATFORMS
ArtifactMetadata = checker.ArtifactMetadata
CompatibilityCheckError = checker.CompatibilityCheckError
GitHubApiAutomation = checker.GitHubApiAutomation
build_report = checker.build_report
check_fixture = checker.check_fixture
check_live = checker.check_live
install_official_kimi = checker.install_official_kimi
prepare_compatibility_release = checker.prepare_compatibility_release
read_report = checker.read_report
redact = checker.redact
select_kimi_code_versions = checker.select_kimi_code_versions
summarize_report_batches = checker.summarize_report_batches
summarize_reports = checker.summarize_reports
synchronize_reports = checker.synchronize_reports
write_report = checker.write_report


def _passing_check(identifier: str = "ok") -> KimiContractCheck:
    return KimiContractCheck(identifier, "test", "pass", "compatible", "test")


def _failing_check(
    identifier: str = "broken", detail: str = "required surface is missing"
) -> KimiContractCheck:
    return KimiContractCheck(identifier, "rest", "fail", detail, "test")


def _reports_for_all_platforms(
    version: str,
    *,
    failing: dict[str, KimiContractCheck] | None = None,
    versions: dict[str, str] | None = None,
) -> list[Any]:
    reports = []
    for platform in CANARY_PLATFORMS:
        check = (failing or {}).get(platform) or _passing_check()
        reports.append(
            build_report(
                mode="live",
                product="kimi-code",
                version=(versions or {}).get(platform, version),
                checks=(check,),
                platform=platform,
            )
        )
    return reports


def _minimal_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    openapi = {
        "openapi": "3.0.3",
        "info": {
            "title": kimi_contract.KIMI_OPENAPI_TITLE,
            "version": "0.28.1",
        },
        "paths": {
            "/api/v1/example": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "code": {"type": "number", "enum": [0]},
                                            "data": {
                                                "type": "object",
                                                "properties": {
                                                    "value": {"type": "string"}
                                                },
                                                "required": ["value"],
                                            },
                                        },
                                        "required": ["code", "data"],
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
    }
    asyncapi = {
        "asyncapi": "3.1.0",
        "info": {
            "title": kimi_contract.KIMI_ASYNCAPI_TITLE,
            "version": "0.28.1",
        },
        "channels": {
            "kimiCodeWebSocket": {"address": kimi_contract.KIMI_WEBSOCKET_PATH}
        },
        "components": {"messages": {}},
    }
    return openapi, asyncapi


@pytest.mark.parametrize(
    ("persisted_model", "expected_status"),
    [(None, "pass"), ("kimi-code/k3", "fail")],
)
async def test_live_probe_monitors_create_time_model_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    persisted_model: str | None,
    expected_status: str,
) -> None:
    class FakeProbeClient:
        def __init__(self) -> None:
            self.created_profiles: list[dict[str, Any]] = []
            self.subscription_calls = 0

        async def __aenter__(self) -> FakeProbeClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def check_server_version(self) -> str:
            return "0.29.1"

        async def get_openapi_document(self) -> dict[str, Any]:
            return {}

        async def get_asyncapi_document(self) -> dict[str, Any]:
            return {}

        async def get_default_model(self) -> str:
            return "kimi-code/k3"

        async def create_session(
            self, _workspace: str, **profile: Any
        ) -> str:
            self.created_profiles.append(profile)
            return "session-1"

        async def get_session_status(self, _session_id: str) -> SessionStatus:
            return SessionStatus(
                busy=False,
                model=persisted_model,
                thinking_effort="off",
                permission_mode="manual",
                plan_mode=False,
                swarm_mode=False,
                context_tokens=0,
                context_limit=262_144,
            )

        async def probe_subscription(self, _session_id: str) -> None:
            self.subscription_calls += 1

    client = FakeProbeClient()
    supervisor = SimpleNamespace(
        connection=SimpleNamespace(token="secret"),
        executable_identity=SimpleNamespace(
            product=SimpleNamespace(value="kimi-code")
        ),
    )
    monkeypatch.setattr(
        probe_module, "KimiServerClient", lambda **_kwargs: client
    )
    monkeypatch.setattr(
        probe_module, "evaluate_kimi_semantic_contract", lambda *_args, **_kwargs: ()
    )

    result = await probe_module.probe_kimi_compatibility(
        supervisor, tmp_path / "workspace"
    )

    assert client.created_profiles == [{"model": "kimi-code/k3"}]
    assert client.subscription_calls == 2
    model_check = next(
        item
        for item in result.checks
        if item.id == "runtime.behavior.session.create_model_persistence"
    )
    assert model_check.status == expected_status


def test_semantic_projection_tolerates_additions_and_rejects_required_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = kimi_contract.RestOperationContract(
        "example",
        "KimiServerClient.example",
        "GET",
        "/example",
        "/api/v1/example",
        response_fields=(
            kimi_contract.SchemaFieldContract(
                ("value",), ("string",), values=("one", "two")
            ),
        ),
    )
    monkeypatch.setattr(kimi_contract, "KIMI_REST_OPERATIONS", {"example": operation})
    monkeypatch.setattr(kimi_contract, "KIMI_WEBSOCKET_MESSAGES", ())
    monkeypatch.setattr(kimi_contract, "KIMI_SESSION_EVENTS", ())
    openapi, asyncapi = _minimal_documents()
    openapi["paths"]["/api/v1/added"] = {"get": {}}
    data_schema = openapi["paths"]["/api/v1/example"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["properties"]["data"]
    data_schema["properties"]["optional_addition"] = {"type": "boolean"}
    data_schema["properties"]["value"]["enum"] = ["one", "two", "added"]

    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)
    assert not [item for item in checks if item.status == "fail"]

    checks = kimi_contract.evaluate_kimi_semantic_contract(
        openapi, asyncapi, expected_version="0.29.0"
    )
    assert {item.id for item in checks if item.status == "fail"} == {
        "asyncapi.version",
        "openapi.version",
    }

    data_schema["properties"]["value"]["enum"] = ["one", "added"]
    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)
    assert {item.id for item in checks if item.status == "fail"} == {
        "rest.example.response.value"
    }

    data_schema["properties"]["value"] = {"type": "integer"}
    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)
    assert {item.id for item in checks if item.status == "fail"} == {
        "rest.example.response.value"
    }

    del openapi["paths"]["/api/v1/example"]
    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)
    assert "rest.example.operation" in {
        item.id for item in checks if item.status == "fail"
    }


def test_semantic_projection_checks_requests_messages_and_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openapi, asyncapi = _minimal_documents()
    operation = kimi_contract.RestOperationContract(
        "example",
        "KimiServerClient.example",
        "GET",
        "/example",
        "/api/v1/example",
    )
    message = kimi_contract.WebSocketMessageContract(
        "client_hello",
        "KimiServerClient._send_client_hello",
        (
            kimi_contract.SchemaFieldContract(("type",), ("string",)),
            kimi_contract.SchemaFieldContract(("id",), ("string",)),
        ),
        ({"type": "client_hello", "id": "request-1"},),
    )
    event = kimi_contract.SessionEventContract(
        "assistant.delta",
        "ChatRouter._dispatch_event",
        (kimi_contract.SchemaFieldContract(("delta",), ("string",)),),
    )
    monkeypatch.setattr(kimi_contract, "KIMI_REST_OPERATIONS", {"example": operation})
    monkeypatch.setattr(kimi_contract, "KIMI_WEBSOCKET_MESSAGES", (message,))
    monkeypatch.setattr(kimi_contract, "KIMI_SESSION_EVENTS", (event,))
    asyncapi["components"]["messages"] = {
        "client_hello": {
            "payload": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["client_hello"]},
                    "id": {"type": "string"},
                },
                "required": ["type", "id"],
            }
        },
        "session_event": {
            "payload": {
                "type": "object",
                "properties": {
                    "payload": {
                        "oneOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "enum": ["assistant.delta"],
                                    },
                                    "delta": {"type": "string"},
                                },
                                "required": ["type", "delta"],
                            }
                        ]
                    }
                },
            }
        },
    }

    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)
    assert not [item for item in checks if item.status == "fail"]

    openapi["paths"]["/api/v1/example"]["get"]["requestBody"] = {
        "required": True,
        "content": {"application/json": {"schema": {"type": "object"}}},
    }
    del asyncapi["components"]["messages"]["session_event"]["payload"][
        "properties"
    ]["payload"]["oneOf"][0]["properties"]["delta"]
    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)
    failures = {item.id for item in checks if item.status == "fail"}
    assert "rest.example.request" in failures
    assert "websocket.event.assistant.delta.delta" in failures


def test_semantic_projection_checks_fields_required_in_optional_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openapi, asyncapi = _minimal_documents()
    event = kimi_contract.SessionEventContract(
        "turn.ended",
        "session_notice_from_event",
        (
            kimi_contract.SchemaFieldContract(
                ("error", "retryable"),
                ("boolean",),
                required=False,
                required_if_parent_present=True,
            ),
        ),
    )
    monkeypatch.setattr(kimi_contract, "KIMI_REST_OPERATIONS", {})
    monkeypatch.setattr(kimi_contract, "KIMI_WEBSOCKET_MESSAGES", ())
    monkeypatch.setattr(kimi_contract, "KIMI_SESSION_EVENTS", (event,))
    retryable_schema = {"type": "boolean"}
    error_schema = {
        "type": "object",
        "properties": {"retryable": retryable_schema},
        "required": ["retryable"],
    }
    asyncapi["components"]["messages"] = {
        "session_event": {
            "payload": {
                "type": "object",
                "properties": {
                    "payload": {
                        "oneOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "enum": ["turn.ended"],
                                    },
                                    "error": error_schema,
                                },
                                "required": ["type"],
                            }
                        ]
                    }
                },
            }
        }
    }

    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)
    assert not [item for item in checks if item.status == "fail"]

    error_schema["required"] = []
    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)
    assert {item.id for item in checks if item.status == "fail"} == {
        "websocket.event.turn.ended.error.retryable"
    }


def test_semantic_projection_checks_multipart_request_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openapi, asyncapi = _minimal_documents()
    operation = kimi_contract.RestOperationContract(
        "upload_file",
        "KimiServerClient._upload_prompt_media",
        "POST",
        "/files",
        "/api/v1/files",
        request_media_type="multipart/form-data",
        request_examples=({"file": "binary", "name": "photo.png"},),
        request_fields=(
            kimi_contract.SchemaFieldContract(
                ("file",),
                ("string",),
                format="binary",
            ),
        ),
    )
    example = openapi["paths"].pop("/api/v1/example")["get"]
    example["requestBody"] = {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "name": {"type": "string"},
                    },
                    "required": ["file"],
                }
            }
        },
    }
    openapi["paths"]["/api/v1/files"] = {"post": example}
    monkeypatch.setattr(
        kimi_contract,
        "KIMI_REST_OPERATIONS",
        {"upload_file": operation},
    )
    monkeypatch.setattr(kimi_contract, "KIMI_WEBSOCKET_MESSAGES", ())
    monkeypatch.setattr(kimi_contract, "KIMI_SESSION_EVENTS", ())

    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)
    assert not [item for item in checks if item.status == "fail"]

    file_schema = example["requestBody"]["content"]["multipart/form-data"][
        "schema"
    ]["properties"]["file"]
    del file_schema["format"]
    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)
    assert {item.id for item in checks if item.status == "fail"} == {
        "rest.upload_file.request"
    }
    file_schema["format"] = "binary"

    request_content = example["requestBody"]["content"]
    request_content["application/json"] = request_content.pop(
        "multipart/form-data"
    )
    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)
    assert {item.id for item in checks if item.status == "fail"} == {
        "rest.upload_file.request"
    }


def test_semantic_projection_accepts_an_optional_outbound_message_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openapi, asyncapi = _minimal_documents()
    client_hello = next(
        message
        for message in kimi_contract.KIMI_WEBSOCKET_MESSAGES
        if message.name == "client_hello"
    )
    assert next(
        field
        for field in client_hello.fields
        if field.path == ("payload", "subscriptions")
    ).required is False

    message = kimi_contract.WebSocketMessageContract(
        "client_hello",
        "KimiServerClient._send_client_hello",
        (
            kimi_contract.SchemaFieldContract(("payload", "client_id"), ("string",)),
            kimi_contract.SchemaFieldContract(
                ("payload", "subscriptions"), ("array",), required=False
            ),
        ),
        (
            {
                "payload": {
                    "client_id": "kimi-bridge",
                    "subscriptions": [],
                }
            },
        ),
    )
    monkeypatch.setattr(kimi_contract, "KIMI_REST_OPERATIONS", {})
    monkeypatch.setattr(kimi_contract, "KIMI_WEBSOCKET_MESSAGES", (message,))
    monkeypatch.setattr(kimi_contract, "KIMI_SESSION_EVENTS", ())
    asyncapi["components"]["messages"] = {
        "client_hello": {
            "payload": {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "properties": {
                            "client_id": {"type": "string"},
                            "subscriptions": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["client_id"],
                    }
                },
                "required": ["payload"],
            }
        }
    }

    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)

    assert not [item for item in checks if item.status == "fail"]


def test_semantic_projection_accepts_an_optional_session_context_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openapi, asyncapi = _minimal_documents()
    session_status = kimi_contract.KIMI_REST_OPERATIONS["session_status"]
    context_limit = next(
        field
        for field in session_status.response_fields
        if field.path == ("max_context_tokens",)
    )
    assert context_limit.required is False

    operation = kimi_contract.RestOperationContract(
        "session_status",
        "KimiServerClient.get_session_status/_materialize_session",
        "GET",
        "/sessions/{session_id}/status",
        "/api/v1/sessions/{session_id}/status",
        response_fields=(context_limit,),
    )
    example = openapi["paths"].pop("/api/v1/example")
    data_schema = example["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["data"]
    data_schema["properties"] = {
        "max_context_tokens": {"type": "integer"}
    }
    data_schema.pop("required")
    openapi["paths"]["/api/v1/sessions/{session_id}/status"] = example
    monkeypatch.setattr(
        kimi_contract, "KIMI_REST_OPERATIONS", {"session_status": operation}
    )
    monkeypatch.setattr(kimi_contract, "KIMI_WEBSOCKET_MESSAGES", ())
    monkeypatch.setattr(kimi_contract, "KIMI_SESSION_EVENTS", ())

    checks = kimi_contract.evaluate_kimi_semantic_contract(openapi, asyncapi)

    assert not [item for item in checks if item.status == "fail"]


def _write_fixture(directory: Path, *, legacy: bool = False) -> None:
    version = "kimi, version 1.49.0\n" if legacy else "0.28.1\n"
    help_text = (
        "Usage: kimi [OPTIONS] COMMAND [ARGS]...\n"
        "--mcp-config-file PATH\n"
        "https://moonshotai.github.io/kimi-cli/\n"
        if legacy
        else "Usage: kimi [options] [command]\nweb [options]\ndoctor\nmigrate\n"
    )
    (directory / "version.txt").write_text(version, encoding="utf-8")
    (directory / "help.txt").write_text(help_text, encoding="utf-8")
    (directory / "web-help.txt").write_text(
        "--no-open --host --port", encoding="utf-8"
    )
    (directory / "openapi.json").write_text("{}", encoding="utf-8")
    (directory / "asyncapi.json").write_text("{}", encoding="utf-8")


def test_fixture_mode_fingerprints_product_and_cli_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture(tmp_path)
    monkeypatch.setattr(
        checker,
        "evaluate_kimi_semantic_contract",
        lambda *_args, **_kwargs: (),
    )

    report = check_fixture(tmp_path)

    assert report.compatible
    assert report.product == "kimi-code"
    assert report.version == "0.28.1"

    _write_fixture(tmp_path, legacy=True)
    report = check_fixture(tmp_path)
    assert not report.compatible
    assert report.product == "legacy-kimi-cli"
    assert report.failures[0]["id"] == "cli.product"


def test_reports_are_redacted_bounded_and_order_stable() -> None:
    secret = (
        "Authorization: Bearer abc123 "
        "http://127.0.0.1/#token=fragment "
        "https://example.test/?api_key=query-secret "
        '{"app_secret":"configured-secret","password":"password-secret"}'
    )
    assert "abc123" not in redact(secret)
    assert "fragment" not in redact(secret)
    assert "configured-secret" not in redact(secret)
    assert "query-secret" not in redact(secret)
    assert "password-secret" not in redact(secret)
    assert redact("x" * 3000).endswith("...<truncated>")

    first = build_report(
        mode="fixture",
        product="kimi-code",
        version="0.29.0",
        checks=(_failing_check("b", secret), _failing_check("a")),
    )
    second = build_report(
        mode="fixture",
        product="kimi-code",
        version="0.29.0",
        checks=(_failing_check("a"), _failing_check("b", secret)),
        artifacts=(ArtifactMetadata("openapi.json", 99, "a" * 64),),
    )
    assert first.failure_digest == second.failure_digest
    assert first.report_digest == second.report_digest
    assert secret not in json.dumps(first.to_dict())


def test_report_round_trip_preserves_machine_contract(tmp_path: Path) -> None:
    report = build_report(
        mode="fixture",
        product="kimi-code",
        version="0.28.1",
        checks=(_passing_check(),),
    )
    path = tmp_path / "report.json"

    write_report(report, path)

    assert read_report(path) == report


def test_report_reader_rejects_an_outdated_semantic_contract(
    tmp_path: Path,
) -> None:
    report = build_report(
        mode="fixture",
        product="kimi-code",
        version="0.28.1",
        checks=(_passing_check(),),
    )
    path = tmp_path / "report.json"
    write_report(report, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["contract_schema_version"] = (
        kimi_contract.KIMI_SEMANTIC_CONTRACT_VERSION - 1
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="unsupported Kimi semantic contract schema",
    ):
        read_report(path)


def test_installer_failure_is_redacted(tmp_path: Path) -> None:
    def failed_runner(
        command: Any, *, env: Any = None, timeout: Any = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 22, "", "failed at #token=do-not-print"
        )

    with pytest.raises(CompatibilityCheckError) as raised:
        install_official_kimi(
            tmp_path,
            version="0.28.1",
            installer_url="https://example.invalid/install.sh",
            runner=failed_runner,
            platform_name="linux",
        )
    assert "do-not-print" not in str(raised.value)
    assert raised.value.category == "installer-download"


def test_probe_config_defines_a_credential_free_default_model(
    tmp_path: Path,
) -> None:
    checker._write_probe_config(tmp_path)

    with (tmp_path / "config.toml").open("rb") as config_file:
        config = tomllib.load(config_file)
    alias = config["default_model"]
    model = config["models"][alias]
    provider = config["providers"][model["provider"]]

    assert alias == checker.PROBE_MODEL_ALIAS
    assert model["model"]
    assert model["max_context_size"] > 0
    assert provider["type"] == "openai"
    assert provider["base_url"].startswith("http://127.0.0.1:")
    assert provider["api_key"] == "unused"


def test_installer_timeout_reports_partial_output(tmp_path: Path) -> None:
    def hanging_runner(
        command: Any, *, env: Any = None, timeout: Any = None
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "curl":
            destination = Path(command[command.index("--output") + 1])
            destination.write_text("# installer\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=b"Downloading kimi.exe #token=do-not-print",
            stderr=None,
        )

    with pytest.raises(CompatibilityCheckError) as raised:
        install_official_kimi(
            tmp_path,
            version=None,
            installer_url="https://example.invalid/install.ps1",
            runner=hanging_runner,
            platform_name="win32",
        )
    assert raised.value.category == "installer-execution"
    assert "timed out after 600 seconds" in str(raised.value)
    assert "Downloading kimi.exe" in str(raised.value)
    assert "do-not-print" not in str(raised.value)


def test_windows_installer_uses_powershell_and_finds_exe(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def windows_runner(
        command: Any, *, env: Any = None, timeout: Any = None
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if command[0] == "curl":
            destination = Path(command[command.index("--output") + 1])
            destination.write_text("# installer\n", encoding="utf-8")
        else:
            executable = Path(env["KIMI_INSTALL_DIR"]) / "bin" / "kimi.exe"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"")
        return subprocess.CompletedProcess(command, 0, "", "")

    installed = install_official_kimi(
        tmp_path,
        version=None,
        installer_url="https://example.invalid/install.ps1",
        runner=windows_runner,
        platform_name="win32",
    )

    assert installed.executable.name == "kimi.exe"
    assert commands[0][0] == "curl"
    assert commands[0][-1] == "https://example.invalid/install.ps1"
    assert Path(commands[0][commands[0].index("--output") + 1]).name == "install.ps1"
    assert commands[1][:6] == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "$ProgressPreference = 'SilentlyContinue'; "
        f"& '{tmp_path / 'install.ps1'}'; exit $LASTEXITCODE",
    ]
    assert installed.environment["USERPROFILE"] == str(tmp_path / "home")
    install_bin = str(tmp_path / "install" / "bin")
    assert installed.environment["PATH"].startswith(f"{install_bin};")


async def test_live_checker_cleans_temporary_home_after_installer_failure() -> None:
    roots: list[Path] = []
    commands: list[list[str]] = []

    def incomplete_runner(
        command: Any, *, env: Any = None, timeout: Any = None
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if command[0] == "curl":
            destination = Path(command[command.index("--output") + 1])
            roots.append(destination.parent)
            destination.write_text("#!/bin/sh\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    report = await check_live(runner=incomplete_runner, platform_name="linux")

    assert not report.compatible
    assert report.failures[0]["category"] == "installer-execution"
    assert roots and not roots[0].exists()
    assert "--fail" in commands[0]
    assert "--retry-all-errors" in commands[0]
    assert commands[1][0] == "bash"


async def test_live_checker_rejects_malformed_explicit_version_before_download() -> None:
    def unexpected_runner(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("installer must not run")

    report = await check_live(version="latest; unsafe", runner=unexpected_runner)

    assert not report.compatible
    assert report.version == "invalid"
    assert report.failures[0]["id"] == "input.version"


async def test_live_checker_attributes_probe_failure_to_requested_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = checker.InstalledKimi(
        tmp_path / "kimi",
        {"KIMI_CODE_HOME": str(tmp_path / "kimi-home")},
    )

    class FakeSupervisor:
        executable_identity = SimpleNamespace(
            product=SimpleNamespace(value="kimi-code"),
            version="0.37.1",
        )

        async def __aenter__(self) -> FakeSupervisor:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    async def failed_probe(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(
        checker, "install_official_kimi", lambda *_args, **_kwargs: installed
    )
    monkeypatch.setattr(
        checker, "KimiServerSupervisor", lambda **_kwargs: FakeSupervisor()
    )
    monkeypatch.setattr(checker, "probe_kimi_compatibility", failed_probe)

    report = await check_live(version="0.37.2")

    assert not report.compatible
    assert report.product == "kimi-code"
    assert report.version == "0.37.2"


@pytest.mark.skipif(
    os.name != "posix", reason="spawns a shebang script as the fake kimi"
)
async def test_live_checker_reports_startup_timeout_and_cleans_up() -> None:
    roots: list[Path] = []

    def hanging_runner(
        command: Any, *, env: Any = None, timeout: Any = None
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "curl":
            destination = Path(command[command.index("--output") + 1])
            roots.append(destination.parent)
            destination.write_text("installer", encoding="utf-8")
        else:
            executable = Path(env["KIMI_INSTALL_DIR"]) / "bin" / "kimi"
            executable.parent.mkdir(parents=True)
            executable.write_text(
                f"#!{sys.executable}\nimport time\ntime.sleep(10)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
        return subprocess.CompletedProcess(command, 0, "", "")

    report = await check_live(runner=hanging_runner, startup_timeout=0.02)

    assert not report.compatible
    assert report.failures[0]["category"] == "startup", report.failures
    assert roots and not roots[0].exists()


class FakeGitHub:
    def __init__(self) -> None:
        self.branch_exists = False
        self.project_version = COMPATIBILITY_MAP[-1].bridge
        self.base_pyproject = (
            "[build-system]\nrequires = []\n\n"
            "[project]\n"
            'name = "kimi-bridge"\n'
            f'version = "{self.project_version}"\n'
        )
        self.base_uv_lock = (
            "version = 1\n\n"
            "[[package]]\n"
            'name = "kimi-bridge"\n'
            f'version = "{self.project_version}"\n'
            'source = { editable = "." }\n'
        )
        self.base_content = {
            "schema_version": 1,
            "releases": [
                {
                    "bridge": COMPATIBILITY_MAP[-1].bridge,
                    "kimi_code": sorted(
                        SUPPORTED_KIMI_CODE_VERSIONS,
                        key=kimi_code_version_sort_key,
                    ),
                }
            ],
        }
        self.pyproject = self.base_pyproject
        self.uv_lock = self.base_uv_lock
        self.branch_content = json.loads(json.dumps(self.base_content))
        self.pulls: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.comments: list[str] = []
        self.content_updates = 0
        self.content_refs: list[str] = []
        self.updated_paths: list[str] = []
        self.ci_dispatches = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        payload = json.loads(request.content) if request.content else {}
        if path == "/repos/Mtrya/kimi-bridge/git/ref/heads/main":
            return self._json({"object": {"sha": "base-sha"}})
        if path.endswith("/git/refs/heads/automation/kimi-code-compatibility"):
            if method == "GET":
                return self._json(
                    {"object": {"sha": "automation-sha"}}
                    if self.branch_exists
                    else {"message": "not found"},
                    status=200 if self.branch_exists else 404,
                )
            self.branch_exists = True
            return self._json({"object": {"sha": payload["sha"]}})
        if path.endswith("/git/refs"):
            self.branch_exists = True
            return self._json({"ref": payload["ref"]}, status=201)
        if "/contents/" in path and any(
            path.endswith(f"/contents/{item}")
            for item in (
                "pyproject.toml",
                "uv.lock",
                "src/kimi_bridge/compatibility-map.json",
            )
        ):
            content_path = path.split("/contents/", 1)[1]
            if method == "GET":
                self.content_refs.append(str(request.url.params["ref"]))
                if content_path == "pyproject.toml":
                    value = self.base_pyproject
                elif content_path == "uv.lock":
                    value = self.base_uv_lock
                else:
                    value = json.dumps(self.base_content) + "\n"
                content = base64.b64encode(
                    value.encode()
                ).decode()
                return self._json(
                    {"sha": f"{content_path}-sha", "content": content}
                )
            value = base64.b64decode(payload["content"]).decode()
            if content_path == "pyproject.toml":
                self.pyproject = value
            elif content_path == "uv.lock":
                self.uv_lock = value
            else:
                self.branch_content = json.loads(value)
            self.content_updates += 1
            self.updated_paths.append(content_path)
            return self._json({"content": {"sha": "next-sha"}})
        if path.endswith("/pulls"):
            if method == "GET":
                return self._json(
                    [
                        pull
                        for pull in self.pulls
                        if pull.get("state", "open") == "open"
                    ]
                )
            pull = {
                "number": 1,
                "node_id": "pull-node",
                "title": payload["title"],
                "body": payload["body"],
                "state": "open",
            }
            self.pulls.append(pull)
            return self._json(pull, status=201)
        if path.endswith("/pulls/1"):
            self.pulls[0].update(payload)
            return self._json(self.pulls[0])
        if path == "/graphql":
            return self._json({"data": {"enablePullRequestAutoMerge": {}}})
        if path.endswith("/actions/workflows/ci.yml/dispatches"):
            assert payload == {"ref": AUTOMATION_BRANCH}
            self.ci_dispatches += 1
            return httpx.Response(204)
        if path.endswith("/labels/upstream-drift"):
            return self._json({"name": "upstream-drift"})
        if path.endswith("/issues"):
            if method == "GET":
                state = request.url.params.get("state", "open")
                return self._json(
                    [
                        issue
                        for issue in self.issues
                        if state == "all" or issue.get("state") == state
                    ]
                )
            issue = {
                "number": len(self.issues) + 2,
                "state": "open",
                "title": payload["title"],
                "body": payload["body"],
            }
            self.issues.append(issue)
            return self._json(issue, status=201)
        issue_match = re.search(r"/issues/(\d+)(/comments)?$", path)
        if issue_match is not None and issue_match.group(2):
            self.comments.append(payload["body"])
            return self._json({"id": len(self.comments)}, status=201)
        if issue_match is not None:
            issue = next(
                item
                for item in self.issues
                if item["number"] == int(issue_match.group(1))
            )
            issue.update(payload)
            return self._json(issue)
        raise AssertionError(f"unexpected GitHub request: {method} {path}")

    @staticmethod
    def _json(value: Any, *, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=value)

    def merge_promotion(self) -> None:
        self.base_pyproject = self.pyproject
        self.base_uv_lock = self.uv_lock
        self.base_content = json.loads(json.dumps(self.branch_content))
        self.pulls.clear()


def test_prepare_compatibility_release_accepts_reordered_commented_toml() -> None:
    pyproject = (
        "[project] # package metadata\n"
        'description = "test"\n'
        "version = '1.2.3' # release identity\n"
        'name = "kimi-bridge"\n'
    )
    uv_lock = (
        "version = 1\n\n"
        "[[package]] # unrelated\n"
        'version = "9.0.0"\n'
        'name = "dependency"\n\n'
        "[[package]] # editable project\n"
        'source = { editable = "." }\n'
        'version = "1.2.3" # release identity\n'
        'name = "kimi-bridge"\n'
    )
    compatibility_map = json.dumps(
        {
            "schema_version": 1,
            "releases": [
                {"bridge": "1.2.3", "kimi_code": ["0.29.2"]}
            ],
        }
    )

    version, files = prepare_compatibility_release(
        kimi_code_versions=("0.29.3", "0.29.4"),
        pyproject=pyproject,
        uv_lock=uv_lock,
        compatibility_map=compatibility_map,
    )

    assert version == "1.2.4"
    assert "version = '1.2.4' # release identity" in files["pyproject.toml"]
    assert 'version = "9.0.0"' in files["uv.lock"]
    assert 'version = "1.2.4" # release identity' in files["uv.lock"]
    promoted = json.loads(
        files["src/kimi_bridge/compatibility-map.json"]
    )["releases"][-1]["kimi_code"]
    assert promoted == ["0.29.2", "0.29.3", "0.29.4"]


def test_release_discovery_preserves_intermediate_versions() -> None:
    releases = [
        {
            "tag_name": f"{checker.OFFICIAL_KIMI_RELEASE_TAG_PREFIX}{version}",
            "draft": False,
            "prerelease": False,
        }
        for version in ("0.37.2", "0.37.1", "0.37.0", "0.36.1", "0.36.0")
    ]

    assert select_kimi_code_versions(
        releases,
        (),
        supported_versions={"0.36.0", "0.36.1", "0.37.2"},
    ) == ("0.37.0", "0.37.1")


def test_release_discovery_rechecks_open_drift_but_not_closed_drift() -> None:
    releases = [
        {
            "tag_name": f"{checker.OFFICIAL_KIMI_RELEASE_TAG_PREFIX}{version}",
            "draft": False,
            "prerelease": False,
        }
        for version in ("0.37.2", "0.37.1", "0.37.0", "0.36.0")
    ]
    issues = [
        {
            "state": "closed",
            "body": (
                f"{checker.DRIFT_MARKER}\n"
                "<!-- version:0.37.0 failure-digest:old -->"
            ),
        },
        {
            "state": "open",
            "body": (
                f"{checker.DRIFT_MARKER}\n"
                "<!-- version:0.37.1 failure-digest:current -->"
            ),
        },
    ]

    assert select_kimi_code_versions(
        releases, issues, supported_versions={"0.36.0", "0.37.2"}
    ) == ("0.37.1",)


def test_release_discovery_keeps_canarying_latest_when_nothing_is_missing() -> None:
    releases = [
        {
            "tag_name": (
                f"{checker.OFFICIAL_KIMI_RELEASE_TAG_PREFIX}0.37.2"
            ),
            "draft": False,
            "prerelease": False,
        }
    ]

    assert select_kimi_code_versions(
        releases, (), supported_versions={"0.37.2"}
    ) == ("0.37.2",)


def test_github_promotion_drift_dedup_and_recovery(
    unlisted_kimi_code_version: str,
) -> None:
    fake = FakeGitHub()
    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(fake.handle),
    )
    automation = GitHubApiAutomation(
        "Mtrya/kimi-bridge", "token", client=client
    )
    current_supported = next(iter(SUPPORTED_KIMI_CODE_VERSIONS))
    supported = _reports_for_all_platforms(current_supported)
    assert synchronize_reports(supported, automation) == ()
    assert not fake.pulls and not fake.issues

    compatible_unknown = _reports_for_all_platforms(unlisted_kimi_code_version)
    major, minor, patch = (
        int(item) for item in COMPATIBILITY_MAP[-1].bridge.split(".")
    )
    next_bridge_version = f"{major}.{minor}.{patch + 1}"

    assert synchronize_reports(compatible_unknown, automation) == (
        "created-promotion-pr",
    )
    assert AUTOMATION_BRANCH == "automation/kimi-code-compatibility"
    assert len(fake.pulls) == 1
    assert f'version = "{next_bridge_version}"' in fake.pyproject
    assert f'version = "{next_bridge_version}"' in fake.uv_lock
    assert fake.branch_content["releases"][-1]["bridge"] == next_bridge_version
    assert fake.branch_content["releases"][-1]["kimi_code"] == sorted(
        {*SUPPORTED_KIMI_CODE_VERSIONS, unlisted_kimi_code_version},
        key=kimi_code_version_sort_key,
    )
    assert fake.updated_paths == [
        "pyproject.toml",
        "uv.lock",
        "src/kimi_bridge/compatibility-map.json",
    ]
    assert fake.content_refs == ["base-sha", "base-sha", "base-sha"]
    assert fake.ci_dispatches == 1
    assert synchronize_reports(compatible_unknown, automation) == (
        "unchanged-promotion-pr",
    )
    assert len(fake.pulls) == 1
    assert fake.content_updates == 3
    assert fake.ci_dispatches == 1
    fake.merge_promotion()
    assert synchronize_reports(compatible_unknown, automation) == ()
    assert fake.content_updates == 3

    broken = _reports_for_all_platforms(
        unlisted_kimi_code_version, failing={"windows": _failing_check()}
    )
    assert synchronize_reports(broken, automation) == ("created-drift-issue",)
    assert synchronize_reports(broken, automation) == ("unchanged-drift-issue",)
    assert len(fake.issues) == 1
    assert "**windows**" in fake.issues[0]["body"]

    changed = _reports_for_all_platforms(
        unlisted_kimi_code_version,
        failing={
            "macos": _failing_check(detail="a different required failure")
        },
    )
    assert synchronize_reports(changed, automation) == ("updated-drift-issue",)
    assert len(fake.issues) == 1
    assert "**macos**" in fake.issues[0]["body"]

    recovered = _reports_for_all_platforms(unlisted_kimi_code_version)
    assert synchronize_reports(recovered, automation) == (
        "closed-recovered-drift-issue",
    )
    assert fake.issues[0]["state"] == "closed"
    assert len(fake.comments) == 1
    assert synchronize_reports(recovered, automation) == ()
    assert len(fake.comments) == 1
    assert fake.pulls == []

    assert synchronize_reports(broken, automation) == ("created-drift-issue",)
    assert len(fake.issues) == 2
    assert fake.issues[0]["state"] == "closed"
    assert fake.issues[1]["state"] == "open"


def test_batch_promotion_preserves_every_passing_version(
    unlisted_kimi_code_version: str,
) -> None:
    major = unlisted_kimi_code_version.split(".", 1)[0]
    versions = (f"{major}.0.0", f"{major}.0.1", f"{major}.0.2")
    fake = FakeGitHub()
    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(fake.handle),
    )
    automation = GitHubApiAutomation(
        "Mtrya/kimi-bridge", "token", client=client
    )
    reports = [
        report
        for version in versions
        for report in _reports_for_all_platforms(version)
    ]

    assert synchronize_reports(reports, automation) == (
        "created-promotion-pr",
    )
    assert len(fake.pulls) == 1
    assert fake.branch_content["releases"][-1]["kimi_code"] == sorted(
        {*SUPPORTED_KIMI_CODE_VERSIONS, *versions},
        key=kimi_code_version_sort_key,
    )
    assert all(version in fake.pulls[0]["body"] for version in versions)
    assert synchronize_reports(reports, automation) == (
        "unchanged-promotion-pr",
    )

    drifted = [
        report
        for version in versions
        for report in _reports_for_all_platforms(
            version,
            failing={"windows": _failing_check()} if version == versions[1] else None,
        )
    ]
    assert "created-drift-issue" in synchronize_reports(drifted, automation)
    assert fake.pulls[0]["state"] == "closed"


def test_batch_promotion_describes_only_versions_missing_from_base(
    unlisted_kimi_code_version: str,
) -> None:
    major = unlisted_kimi_code_version.split(".", 1)[0]
    existing, missing = (f"{major}.0.0", f"{major}.0.1")
    fake = FakeGitHub()
    fake.base_content["releases"][-1]["kimi_code"] = sorted(
        {*SUPPORTED_KIMI_CODE_VERSIONS, existing},
        key=kimi_code_version_sort_key,
    )
    fake.branch_content = json.loads(json.dumps(fake.base_content))
    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(fake.handle),
    )
    automation = GitHubApiAutomation(
        "Mtrya/kimi-bridge", "token", client=client
    )
    reports = [
        report
        for version in (existing, missing)
        for report in _reports_for_all_platforms(version)
    ]

    assert synchronize_reports(reports, automation) == (
        "created-promotion-pr",
    )
    assert missing in fake.pulls[0]["title"]
    assert existing not in fake.pulls[0]["title"]
    assert f"<!-- versions:{missing} " in fake.pulls[0]["body"]
    assert f"<!-- versions:{existing}," not in fake.pulls[0]["body"]


def test_recovered_unknown_version_does_not_prepare_automatic_release(
    unlisted_kimi_code_version: str,
) -> None:
    fake = FakeGitHub()
    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(fake.handle),
    )
    automation = GitHubApiAutomation(
        "Mtrya/kimi-bridge", "token", client=client
    )
    broken = _reports_for_all_platforms(
        unlisted_kimi_code_version,
        failing={"linux": _failing_check()},
    )
    recovered = _reports_for_all_platforms(unlisted_kimi_code_version)
    fake.pulls.append(
        {
            "number": 1,
            "node_id": "pull-node",
            "title": "Stale compatibility promotion",
            "body": (
                f"{checker.PROMOTION_MARKER}\n"
                f"<!-- version:{unlisted_kimi_code_version} "
                "report-digest:stale -->"
            ),
            "state": "open",
        }
    )

    assert synchronize_reports(broken, automation) == ("created-drift-issue",)
    assert fake.pulls[0]["state"] == "closed"
    fake.pulls[0]["state"] = "open"
    assert synchronize_reports(recovered, automation) == (
        "closed-recovered-drift-issue",
    )
    assert fake.issues[0]["state"] == "closed"
    assert fake.pulls[0]["state"] == "closed"
    assert synchronize_reports(recovered, automation) == ()
    assert len(fake.pulls) == 1


def test_sync_dry_run_predicts_the_decision_without_github(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    unlisted_kimi_code_version: str,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    def render(reports: list[Any]) -> list[str]:
        argv = ["sync", "--dry-run"]
        for index, report in enumerate(reports):
            path = tmp_path / f"report-{index}.json"
            write_report(report, path)
            argv += ["--report", str(path)]
        return argv

    exit_code = checker.main(render(_reports_for_all_platforms("0.28.1")))
    assert exit_code == 0
    assert "compatible" in capsys.readouterr().out

    exit_code = checker.main(
        render(_reports_for_all_platforms(unlisted_kimi_code_version))
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert f"would-promote-{unlisted_kimi_code_version}" in output

    exit_code = checker.main(
        render(
            _reports_for_all_platforms(
                "0.30.0", failing={"windows": _failing_check()}
            )
        )
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "incompatible" in output
    assert "would-record-drift" in output


def test_sync_report_directory_batches_versions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    unlisted_kimi_code_version: str,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    major = unlisted_kimi_code_version.split(".", 1)[0]
    versions = (f"{major}.0.0", f"{major}.0.1")
    for version in versions:
        for report in _reports_for_all_platforms(version):
            write_report(
                report,
                tmp_path / version / report.platform / "report.json",
            )

    exit_code = checker.main(
        ["sync", "--dry-run", "--report-directory", str(tmp_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"would-promote-{versions[0]},{versions[1]}" in output
    assert all(f"kimi-code {version}" in output for version in versions)


def test_strict_gating_blocks_promotion_without_every_platform(
    unlisted_kimi_code_version: str,
) -> None:
    fake = FakeGitHub()
    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(fake.handle),
    )
    automation = GitHubApiAutomation(
        "Mtrya/kimi-bridge", "token", client=client
    )
    linux_only = [
        build_report(
            mode="live",
            product="kimi-code",
            version=unlisted_kimi_code_version,
            checks=(_passing_check(),),
            platform="linux",
        )
    ]

    assert synchronize_reports(linux_only, automation) == (
        "created-drift-issue",
    )
    assert not fake.pulls
    body = fake.issues[0]["body"]
    assert "no compatibility report for macos" in body
    assert "no compatibility report for windows" in body

    summary = summarize_reports(linux_only)
    assert not summary.compatible
    assert summary.platforms == ("linux",)


def test_strict_gating_treats_version_skew_as_drift() -> None:
    skewed = summarize_reports(
        _reports_for_all_platforms(
            "0.30.0", versions={"windows": "0.30.1"}
        )
    )

    assert not skewed.compatible
    assert skewed.version == "mixed"
    assert any(
        item["id"] == "aggregation.version" and item["platform"] == "all"
        for item in skewed.failures
    )


def test_partial_multi_version_reports_never_create_a_mixed_summary() -> None:
    reports = [
        build_report(
            mode="live",
            product="kimi-code",
            version="0.37.1",
            checks=(_passing_check(),),
            platform="linux",
        ),
        build_report(
            mode="live",
            product="kimi-code",
            version="0.37.2",
            checks=(_passing_check(),),
            platform="macos",
        ),
    ]

    summaries = summarize_report_batches(reports)

    assert tuple(summary.version for summary in summaries) == (
        "0.37.1",
        "0.37.2",
    )
    assert all(not summary.compatible for summary in summaries)


def test_summarize_reports_rejects_duplicate_platforms() -> None:
    duplicated = [
        build_report(
            mode="live",
            product="kimi-code",
            version="0.28.1",
            checks=(_passing_check(),),
            platform="linux",
        )
    ] * 2

    with pytest.raises(ValueError, match="distinct platform"):
        summarize_reports(duplicated)
