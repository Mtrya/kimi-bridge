from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


WORKFLOW_DIRECTORY = Path(".github/workflows")
ACTION_PIN_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _load(name: str) -> dict[str, Any]:
    value = yaml.load(
        (WORKFLOW_DIRECTORY / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(value, dict)
    return value


def test_all_workflows_parse_and_pin_third_party_actions() -> None:
    paths = sorted(WORKFLOW_DIRECTORY.glob("*.yml"))
    assert {path.name for path in paths} == {"ci.yml", "kimi-drift.yml"}
    for path in paths:
        document = _load(path.name)
        assert isinstance(document["jobs"], dict)
        for job in document["jobs"].values():
            for step in job.get("steps", []):
                action = step.get("uses")
                if action is not None:
                    assert ACTION_PIN_RE.fullmatch(action), action


def test_ci_has_locked_fake_test_matrix_quality_and_distribution_jobs() -> None:
    workflow = _load("ci.yml")

    assert set(workflow["on"]) == {
        "pull_request",
        "push",
        "workflow_dispatch",
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "true"
    jobs = workflow["jobs"]
    assert set(jobs) == {"tests", "quality", "distribution"}
    assert jobs["tests"]["strategy"]["matrix"]["python-version"] == [
        "3.11",
        "3.13",
    ]
    commands = "\n".join(
        step.get("run", "")
        for job in jobs.values()
        for step in job["steps"]
    )
    for required in (
        "uv sync --locked",
        "pytest -q",
        "ruff check .",
        "git diff --check",
        "uv build --no-sources",
        "scripts/check_distribution.py",
    ):
        assert required in commands
    assert "secrets." not in (WORKFLOW_DIRECTORY / "ci.yml").read_text()


def test_drift_workflow_is_daily_manual_credential_free_and_write_scoped() -> None:
    workflow = _load("kimi-drift.yml")

    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    assert workflow["on"]["schedule"] == [{"cron": "17 19 * * *"}]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["canary"]["if"] == (
        "github.ref == 'refs/heads/main'"
    )
    assert "permissions" not in workflow["jobs"]["canary"]
    assert workflow["jobs"]["synchronize"]["permissions"] == {
        "actions": "write",
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    rendered = (WORKFLOW_DIRECTORY / "kimi-drift.yml").read_text()
    assert "check_kimi_compatibility.py check" in rendered
    assert "check_kimi_compatibility.py sync" in rendered
    assert "submit_prompt" not in rendered
    assert "APP_SECRET" not in rendered
    assert "FEISHU" not in rendered
    assert "TELEGRAM" not in rendered
