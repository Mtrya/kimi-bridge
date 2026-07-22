#!/usr/bin/env python3
"""Validate release identity against the project metadata and Git tag."""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path


PROJECT_NAME = "kimi-bridge"
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--commit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if bool(args.tag) != bool(args.commit):
        raise RuntimeError("--tag and --commit must be provided together")

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    if project["name"] != PROJECT_NAME:
        raise RuntimeError(f"unexpected project name: {project['name']}")
    project_version = project["version"]
    if (
        not isinstance(project_version, str)
        or VERSION_RE.fullmatch(project_version) is None
    ):
        raise RuntimeError("project version must be a plain X.Y.Z release")

    installed_version = importlib.metadata.version(PROJECT_NAME)
    if installed_version != project_version:
        raise RuntimeError(
            f"installed version {installed_version} does not match project version {project_version}"
        )

    if args.tag is not None:
        expected_tag = f"v{project_version}"
        if args.tag != expected_tag:
            raise RuntimeError(
                f"release tag {args.tag!r} does not match package version {project_version}"
            )
        head = _git("rev-parse", "HEAD")
        event_commit = _git("rev-parse", f"{args.commit}^{{commit}}")
        tag_commit = _git("rev-parse", f"refs/tags/{args.tag}^{{commit}}")
        if len({head, event_commit, tag_commit}) != 1:
            raise RuntimeError(
                "release tag, event commit, and checked-out commit differ"
            )

    print(f"release identity valid: {PROJECT_NAME} {project_version}")
    return 0


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
