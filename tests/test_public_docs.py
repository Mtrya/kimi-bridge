from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from kimi_bridge import __version__


def test_public_documentation_contracts() -> None:
    subprocess.run(
        [sys.executable, "scripts/check_docs.py"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_release_identity_rejects_a_tag_that_does_not_match_metadata() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/check_release.py",
            "--tag",
            "v999.999.999",
            "--commit",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


def test_package_identity_has_one_derived_version_and_public_metadata() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert __version__ == project["version"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["authors"] == [{"name": "Mtrya"}]
    assert project["urls"]["Repository"] == "https://github.com/Mtrya/kimi-bridge"
    assert "Permission is hereby granted" in Path("LICENSE").read_text(encoding="utf-8")
