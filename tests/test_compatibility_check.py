from __future__ import annotations

import ast
import base64
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from kimi_bridge import compatibility_check as checker
from kimi_bridge.compatibility_check import (
    AUTOMATION_BRANCH,
    ArtifactMetadata,
    CompatibilityCheckError,
    GitHubApiAutomation,
    build_report,
    check_fixture,
    check_live,
    install_official_kimi,
    read_report,
    redact,
    synchronize_report,
    write_report,
)
from kimi_bridge.kimi_server import KimiContractCheck, KimiServerClient
from kimi_bridge.kimi_server import contract as kimi_contract
from kimi_bridge.kimi_server.probe import PROBED_LIFECYCLE_INVARIANTS


def _passing_check(identifier: str = "ok") -> KimiContractCheck:
    return KimiContractCheck(identifier, "test", "pass", "compatible", "test")


def _failing_check(
    identifier: str = "broken", detail: str = "required surface is missing"
) -> KimiContractCheck:
    return KimiContractCheck(identifier, "rest", "fail", detail, "test")


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


def test_client_operations_are_sourced_from_the_tracked_contract() -> None:
    tree = ast.parse(inspect.getsource(KimiServerClient))
    executed = {
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "_request_operation"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }

    assert executed == set(kimi_contract.KIMI_REST_OPERATIONS)
    assert all(
        operation.source.startswith("KimiServerClient.")
        for operation in kimi_contract.KIMI_REST_OPERATIONS.values()
    )
    json.dumps(kimi_contract.kimi_semantic_contract(), sort_keys=True)
    assert PROBED_LIFECYCLE_INVARIANTS == {
        identifier for identifier, _source in kimi_contract.KIMI_LIFECYCLE_INVARIANTS
    }


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
        )
    assert "do-not-print" not in str(raised.value)
    assert raised.value.category == "installer-download"


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

    report = await check_live(runner=incomplete_runner)

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
        self.branch_content = {
            "schema_version": 1,
            "versions": ["0.28.1"],
        }
        self.pulls: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.comments: list[str] = []
        self.content_updates = 0
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
        if "/contents/src/kimi_bridge/supported-kimi-code-versions.json" in path:
            if method == "GET":
                content = base64.b64encode(
                    (json.dumps(self.branch_content) + "\n").encode()
                ).decode()
                return self._json({"sha": "manifest-sha", "content": content})
            self.branch_content = json.loads(base64.b64decode(payload["content"]))
            self.content_updates += 1
            return self._json({"content": {"sha": "next-sha"}})
        if path.endswith("/pulls"):
            if method == "GET":
                return self._json(self.pulls)
            pull = {
                "number": 1,
                "node_id": "pull-node",
                "title": payload["title"],
                "body": payload["body"],
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
                return self._json(self.issues)
            issue = {
                "number": 2,
                "state": "open",
                "title": payload["title"],
                "body": payload["body"],
            }
            self.issues.append(issue)
            return self._json(issue, status=201)
        if path.endswith("/issues/2/comments"):
            self.comments.append(payload["body"])
            return self._json({"id": len(self.comments)}, status=201)
        if path.endswith("/issues/2"):
            self.issues[0].update(payload)
            return self._json(self.issues[0])
        raise AssertionError(f"unexpected GitHub request: {method} {path}")

    @staticmethod
    def _json(value: Any, *, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=value)


def test_github_promotion_drift_dedup_and_recovery() -> None:
    fake = FakeGitHub()
    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(fake.handle),
    )
    automation = GitHubApiAutomation(
        "Mtrya/kimi-bridge", "token", client=client
    )
    supported = build_report(
        mode="live",
        product="kimi-code",
        version="0.28.1",
        checks=(_passing_check(),),
    )
    assert synchronize_report(supported, automation) == ()
    assert not fake.pulls and not fake.issues

    compatible_unknown = build_report(
        mode="live",
        product="kimi-code",
        version="0.29.0",
        checks=(_passing_check(),),
    )

    assert synchronize_report(compatible_unknown, automation) == (
        "created-promotion-pr",
    )
    assert AUTOMATION_BRANCH == "automation/kimi-code-compatibility"
    assert len(fake.pulls) == 1
    assert fake.branch_content["versions"] == ["0.28.1", "0.29.0"]
    assert fake.ci_dispatches == 1
    assert synchronize_report(compatible_unknown, automation) == (
        "unchanged-promotion-pr",
    )
    assert len(fake.pulls) == 1
    assert fake.content_updates == 1
    assert fake.ci_dispatches == 1
    fake.pulls.clear()
    assert synchronize_report(compatible_unknown, automation) == ()
    assert fake.content_updates == 1

    broken = build_report(
        mode="live",
        product="kimi-code",
        version="0.30.0",
        checks=(_failing_check(),),
    )
    assert synchronize_report(broken, automation) == ("created-drift-issue",)
    assert synchronize_report(broken, automation) == ("unchanged-drift-issue",)
    assert len(fake.issues) == 1

    changed = build_report(
        mode="live",
        product="kimi-code",
        version="0.30.0",
        checks=(_failing_check(detail="a different required failure"),),
    )
    assert synchronize_report(changed, automation) == ("updated-drift-issue",)
    assert len(fake.issues) == 1

    recovered = build_report(
        mode="live",
        product="kimi-code",
        version="0.28.1",
        checks=(_passing_check(),),
    )
    assert synchronize_report(recovered, automation) == (
        "closed-recovered-drift-issue",
    )
    assert fake.issues[0]["state"] == "closed"
    assert len(fake.comments) == 1
    assert synchronize_report(recovered, automation) == ()
    assert len(fake.comments) == 1
