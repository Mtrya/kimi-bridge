#!/usr/bin/env python3
"""Check Kimi compatibility or synchronize one generated report."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from kimi_bridge.compatibility_check import (
    GitHubApiAutomation,
    read_report,
    run_cli_check,
    synchronize_report,
)
from kimi_bridge.kimi_server import kimi_semantic_contract


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check", help="run a fixture or isolated live compatibility check"
    )
    check.add_argument(
        "--fixture",
        type=Path,
        help="directory containing CLI text and OpenAPI/AsyncAPI fixtures",
    )
    check.add_argument(
        "--version",
        help="install an explicit kimi-code version instead of latest",
    )
    check.add_argument(
        "--report",
        type=Path,
        default=Path("compatibility-report.json"),
    )
    check.add_argument("--artifacts", type=Path)

    contract = subparsers.add_parser(
        "contract", help="print the tracked semantic contract as JSON"
    )
    contract.add_argument("--output", type=Path)

    sync = subparsers.add_parser(
        "sync", help="quietly synchronize a report with GitHub"
    )
    sync.add_argument("--report", type=Path, required=True)
    sync.add_argument(
        "--repository", default=os.environ.get("GITHUB_REPOSITORY")
    )
    sync.add_argument(
        "--default-branch",
        default=os.environ.get("GITHUB_DEFAULT_BRANCH", "main"),
    )
    sync.add_argument("--run-url", default=os.environ.get("GITHUB_RUN_URL"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "contract":
        rendered = json.dumps(
            kimi_semantic_contract(), indent=2, sort_keys=True
        ) + "\n"
        if args.output is None:
            print(rendered, end="")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        return 0

    if args.command == "check":
        if args.fixture is not None and args.version is not None:
            raise SystemExit("--version cannot be combined with --fixture")
        report = run_cli_check(
            fixture_directory=args.fixture,
            version=args.version,
            report_path=args.report,
            artifact_directory=args.artifacts,
        )
        outcome = "compatible" if report.compatible else "incompatible"
        print(
            f"kimi-code {report.version}: {outcome}; "
            f"report {report.report_digest}"
        )
        return 0 if report.compatible else 1

    token = os.environ.get("GITHUB_TOKEN")
    if not args.repository or not token:
        raise SystemExit("sync requires GITHUB_REPOSITORY and GITHUB_TOKEN")
    report = read_report(args.report)
    with GitHubApiAutomation(
        args.repository,
        token,
        default_branch=args.default_branch,
        run_url=args.run_url,
    ) as automation:
        actions = synchronize_report(report, automation)
    if actions:
        print("\n".join(actions))
    return 0 if report.compatible else 1


if __name__ == "__main__":
    raise SystemExit(main())
