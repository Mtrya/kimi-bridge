from __future__ import annotations

import pytest

from kimi_bridge.compatibility import (
    KIMI_CODE_INSTALL_URL,
    SUPPORTED_KIMI_CODE_VERSIONS,
    KimiProduct,
    KimiProductFingerprintError,
    VersionSupport,
    classify_kimi_code_version,
    identify_kimi_executable,
    legacy_product_message,
    unknown_version_warning,
)


KIMI_CODE_HELP = """Usage: kimi [options] [command]

The Starting Point for Next-Gen Agents

Commands:
  web [options]  Run the local Kimi server and open the web UI.
  doctor         Validate Kimi Code configuration files.
  migrate        Migrate data from a legacy kimi-cli installation into kimi-code.
"""

LEGACY_KIMI_CLI_HELP = """Usage: kimi [OPTIONS] COMMAND [ARGS]...

Kimi, your next CLI agent.

  --mcp-config-file PATH
Documentation: https://moonshotai.github.io/kimi-cli/
"""


def test_manifest_contains_the_verified_baseline_and_is_immutable() -> None:
    assert SUPPORTED_KIMI_CODE_VERSIONS == frozenset({"0.28.1"})


def test_identifies_supported_official_kimi_code() -> None:
    identity = identify_kimi_executable("\x1b[1m0.28.1\x1b[0m\n", KIMI_CODE_HELP)

    assert identity.product is KimiProduct.KIMI_CODE
    assert identity.version == "0.28.1"
    assert identity.support is VersionSupport.SUPPORTED


def test_identifies_unknown_official_kimi_code_without_accepting_version_alone() -> None:
    identity = identify_kimi_executable("0.29.0\n", KIMI_CODE_HELP)

    assert identity.product is KimiProduct.KIMI_CODE
    assert identity.support is VersionSupport.UNKNOWN

    with pytest.raises(KimiProductFingerprintError):
        identify_kimi_executable("0.29.0\n", "Usage: kimi [options]")


def test_identifies_legacy_python_kimi_cli_from_structural_fixture() -> None:
    identity = identify_kimi_executable(
        "kimi, version 1.49.0\n", LEGACY_KIMI_CLI_HELP
    )

    assert identity.product is KimiProduct.LEGACY_KIMI_CLI
    assert identity.version == "1.49.0"
    assert KIMI_CODE_INSTALL_URL in legacy_product_message(identity.version)


@pytest.mark.parametrize("version", ["", "v0.28.1", "0.28", "secret-value"])
def test_rejects_malformed_version_evidence(version: str) -> None:
    with pytest.raises(KimiProductFingerprintError):
        identify_kimi_executable(version, KIMI_CODE_HELP)

    with pytest.raises(ValueError, match="malformed"):
        classify_kimi_code_version(version)


def test_unknown_warning_is_prominent_and_actionable() -> None:
    warning = unknown_version_warning("0.29.0")

    assert "UNTESTED KIMI CODE VERSION" in warning
    assert "0.29.0" in warning
    assert KIMI_CODE_INSTALL_URL in warning
