#!/usr/bin/env python3
"""Validate built distributions and isolated uv tool install surfaces."""

from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Sequence
from email.parser import Parser
from email.policy import default
from pathlib import Path


REQUIRED_PACKAGE_FILES = {
    "kimi_bridge/assets/video-cover.png",
    "kimi_bridge/supported-kimi-code-versions.json",
}
REQUIRED_SOURCE_FILES = {
    "AGENTS.md",
    "INSTALL.md",
    "LICENSE",
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/COMMANDS.md",
    "docs/CONFIGURATION.md",
    "docs/kimi-bridge.service",
}
PROJECT_VERSION = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    wheel = _only(args.dist_dir, "*.whl")
    source = _only(args.dist_dir, "*.tar.gz")
    _check_wheel(wheel)
    _check_source(source)
    for artifact in (source, wheel):
        for feishu in (False, True):
            _check_tool_install(artifact.resolve(), feishu=feishu)
    print("distribution checks passed")
    return 0


def _only(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one {pattern} in {directory}, found {len(matches)}"
        )
    return matches[0]


def _check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = REQUIRED_PACKAGE_FILES - names
        if missing:
            raise RuntimeError(f"wheel is missing: {sorted(missing)}")
        metadata_names = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise RuntimeError("wheel does not contain exactly one METADATA file")
        license_names = [
            name for name in names if name.endswith(".dist-info/licenses/LICENSE")
        ]
        if len(license_names) != 1:
            raise RuntimeError("wheel does not contain exactly one MIT license file")
        metadata = archive.read(metadata_names[0]).decode()
    _check_metadata(metadata)


def _check_source(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
        root = names[0].split("/", 1)[0]
        expected = {f"{root}/src/{name}" for name in REQUIRED_PACKAGE_FILES}
        expected.update(f"{root}/{name}" for name in REQUIRED_SOURCE_FILES)
        missing = expected - set(names)
        if missing:
            raise RuntimeError(f"source distribution is missing: {sorted(missing)}")
        metadata = archive.extractfile(f"{root}/PKG-INFO")
        if metadata is None:
            raise RuntimeError("source distribution has no PKG-INFO")
        rendered = metadata.read().decode()
    _check_metadata(rendered)


def _check_metadata(metadata: str) -> None:
    parsed = Parser(policy=default).parsestr(metadata)
    expected = {
        "Name": "kimi-bridge",
        "Version": PROJECT_VERSION,
        "Summary": "Control a local Kimi Code agent from Feishu or Telegram",
        "Requires-Python": ">=3.11",
        "License-Expression": "MIT",
        "Description-Content-Type": "text/markdown",
    }
    mismatches = {
        name: (parsed.get(name), value)
        for name, value in expected.items()
        if parsed.get(name) != value
    }
    if mismatches:
        raise RuntimeError(f"distribution metadata mismatches: {mismatches}")
    if parsed.get_all("License-File") != ["LICENSE"]:
        raise RuntimeError("distribution metadata does not identify LICENSE")
    if parsed.get_all("Provides-Extra") != ["feishu"]:
        raise RuntimeError("distribution metadata does not identify the Feishu extra")

    project_urls = set(parsed.get_all("Project-URL", []))
    required_urls = {
        "Homepage, https://github.com/Mtrya/kimi-bridge",
        "Repository, https://github.com/Mtrya/kimi-bridge",
        "Issues, https://github.com/Mtrya/kimi-bridge/issues",
        "Documentation, https://github.com/Mtrya/kimi-bridge#readme",
        "Changelog, https://github.com/Mtrya/kimi-bridge/releases",
    }
    if not required_urls.issubset(project_urls):
        raise RuntimeError("distribution metadata is missing project URLs")


def _check_tool_install(artifact: Path, *, feishu: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="kimi-bridge-tool-check-") as raw_root:
        root = Path(raw_root)
        tool_directory = root / "tools"
        bin_directory = root / "bin"
        home = root / "home"
        home.mkdir()
        retained = (
            "PATH",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        )
        environment = {
            name: os.environ[name] for name in retained if name in os.environ
        }
        environment.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "UV_TOOL_DIR": str(tool_directory),
                "UV_TOOL_BIN_DIR": str(bin_directory),
                "UV_CACHE_DIR": str(root / "cache"),
            }
        )
        source = f"{artifact}[feishu]" if feishu else str(artifact)
        _run(["uv", "tool", "install", "--from", source, "kimi-bridge"], environment)
        executable = bin_directory / "kimi-bridge"
        _run([str(executable), "--help"], environment)
        version = subprocess.run(
            [str(executable), "--version"],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        if version.stdout.strip() != f"kimi-bridge {PROJECT_VERSION}":
            raise RuntimeError(
                f"unexpected installed version: {version.stdout.strip()}"
            )
        doctor_environment = dict(environment)
        doctor_environment["PATH"] = "/usr/bin:/bin"
        doctor = subprocess.run(
            [str(executable), "doctor"],
            env=doctor_environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if doctor.returncode == 0:
            raise RuntimeError("doctor unexpectedly passed in an empty home")
        if "managed kimi server ready" in (doctor.stdout + doctor.stderr).lower():
            raise RuntimeError("doctor started a managed service")
        _run(["uv", "tool", "uninstall", "kimi-bridge"], environment)
        if executable.exists():
            raise RuntimeError("uv tool uninstall left the console command behind")


def _run(command: list[str], environment: dict[str, str]) -> None:
    result = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"command failed ({' '.join(command)}): {output}")


if __name__ == "__main__":
    raise SystemExit(main())
