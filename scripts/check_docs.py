#!/usr/bin/env python3
"""Check public documentation structure, coverage, and links."""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable, Sequence
from dataclasses import fields
from pathlib import Path
from urllib.parse import urldefrag

import httpx

from kimi_bridge.config import Config, FeishuConfig, KimiServerConfig, TelegramConfig


PUBLIC_MARKDOWN = (
    Path("README.md"),
    Path("INSTALL.md"),
    Path("AGENTS.md"),
    Path("docs/CONFIGURATION.md"),
    Path("docs/COMMANDS.md"),
    Path("docs/ARCHITECTURE.md"),
)
SERVICE_TEMPLATE = Path("docs/kimi-bridge.service")
LINK_RE = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$", re.MULTILINE)
COMMAND_RE = re.compile(r'if command == "(?P<command>/[a-z-]+)"')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--external",
        action="store_true",
        help="also request each external documentation URL",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    texts = _read_public_files()
    _check_local_links(texts)
    _check_schema_coverage(texts[Path("docs/CONFIGURATION.md")])
    _check_command_coverage(texts[Path("docs/COMMANDS.md")])
    _check_service_template()
    if len(texts[Path("README.md")].splitlines()) > 140:
        raise RuntimeError("README is no longer a compact landing page")
    if args.external:
        _check_external_links(texts.values())
    print("public documentation checks passed")
    return 0


def _read_public_files() -> dict[Path, str]:
    missing = [str(path) for path in PUBLIC_MARKDOWN if not path.is_file()]
    if missing:
        raise RuntimeError(f"missing public documentation: {missing}")
    if not SERVICE_TEMPLATE.is_file():
        raise RuntimeError(f"missing service template: {SERVICE_TEMPLATE}")
    return {path: path.read_text(encoding="utf-8") for path in PUBLIC_MARKDOWN}


def _check_local_links(texts: dict[Path, str]) -> None:
    for source, text in texts.items():
        for match in LINK_RE.finditer(text):
            target = match.group("target").strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_part, fragment = urldefrag(target)
            destination = source if not path_part else source.parent / path_part
            destination = destination.resolve()
            if not destination.exists():
                raise RuntimeError(f"broken local link in {source}: {target}")
            if fragment and destination.suffix.lower() == ".md":
                anchors = _anchors(destination.read_text(encoding="utf-8"))
                if fragment not in anchors:
                    raise RuntimeError(f"broken local anchor in {source}: {target}")


def _anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for match in HEADING_RE.finditer(text):
        title = re.sub(r"<[^>]+>", "", match.group("title"))
        title = re.sub(r"[`*_~]", "", title).strip().lower()
        slug = re.sub(r"[^\w\- ]", "", title, flags=re.UNICODE)
        slug = re.sub(r"\s+", "-", slug)
        count = counts.get(slug, 0)
        anchors.add(slug if count == 0 else f"{slug}-{count}")
        counts[slug] = count + 1
    return anchors


def _check_schema_coverage(configuration: str) -> None:
    names = {
        field.name
        for field in fields(Config)
        if field.name not in {"kimi_server", "feishu", "telegram"}
    }
    names.update(f"kimi_server.{field.name}" for field in fields(KimiServerConfig))
    names.update(f"feishu.{field.name}" for field in fields(FeishuConfig))
    names.update(f"telegram.{field.name}" for field in fields(TelegramConfig))
    missing = sorted(name for name in names if f"`{name}`" not in configuration)
    if missing:
        raise RuntimeError(f"configuration reference is missing fields: {missing}")


def _check_command_coverage(commands: str) -> None:
    source = Path("src/kimi_bridge/router/commands.py").read_text(encoding="utf-8")
    implemented = set(COMMAND_RE.findall(source))
    documented = set(re.findall(r"`(/[a-z-]+)", commands))
    missing = sorted(implemented - documented)
    if missing:
        raise RuntimeError(f"command reference is missing commands: {missing}")


def _check_service_template() -> None:
    service = SERVICE_TEMPLATE.read_text(encoding="utf-8")
    required = (
        "[Service]",
        "ExecStart=",
        "Environment=PATH=",
        ".kimi-code/bin",
        "Restart=on-failure",
        "RestartSec=",
        "WantedBy=default.target",
    )
    missing = [value for value in required if value not in service]
    if missing:
        raise RuntimeError(f"service template is missing: {missing}")
    forbidden = ("User=root", "APP_SECRET", "bot_token", "app_secret")
    present = [value for value in forbidden if value in service]
    if present:
        raise RuntimeError(f"service template contains unsafe settings: {present}")


def _check_external_links(texts: Iterable[str]) -> None:
    urls = {
        urldefrag(match.group("target").strip().strip("<>")).url
        for text in texts
        for match in LINK_RE.finditer(text)
        if match.group("target").startswith(("http://", "https://"))
    }
    headers = {"User-Agent": "kimi-bridge-documentation-check"}
    with httpx.Client(follow_redirects=True, timeout=20, headers=headers) as client:
        for url in sorted(urls):
            response = client.get(url)
            if response.status_code == 404 or response.status_code >= 500:
                raise RuntimeError(
                    f"external documentation link returned {response.status_code}: {url}"
                )


if __name__ == "__main__":
    raise SystemExit(main())
