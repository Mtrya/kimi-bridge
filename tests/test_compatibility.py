from __future__ import annotations

import pytest

from kimi_bridge import __version__
from kimi_bridge.compatibility import (
    COMPATIBILITY_MAP,
    KIMI_CODE_INSTALL_URL,
    SUPPORTED_KIMI_CODE_VERSIONS,
    BridgeSupport,
    CompatibilityMapEntry,
    KimiProduct,
    KimiProductFingerprintError,
    VersionSupport,
    _parse_compatibility_map,
    classify_bridge_compatibility,
    classify_kimi_code_version,
    identify_kimi_executable,
    kimi_code_version_sort_key,
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


def test_latest_map_entry_defines_current_supported_versions() -> None:
    assert isinstance(SUPPORTED_KIMI_CODE_VERSIONS, frozenset)
    assert COMPATIBILITY_MAP[-1].bridge == __version__
    assert SUPPORTED_KIMI_CODE_VERSIONS == frozenset(
        COMPATIBILITY_MAP[-1].kimi_code
    )


def test_compatibility_version_order_is_semantic() -> None:
    assert sorted(
        ["0.100.0", "0.29.0", "0.29.0-beta"],
        key=kimi_code_version_sort_key,
    ) == ["0.29.0-beta", "0.29.0", "0.100.0"]


def test_identifies_supported_official_kimi_code() -> None:
    version = next(iter(SUPPORTED_KIMI_CODE_VERSIONS))
    identity = identify_kimi_executable(
        f"\x1b[1m{version}\x1b[0m\n", KIMI_CODE_HELP
    )

    assert identity.product is KimiProduct.KIMI_CODE
    assert identity.version == version
    assert identity.support is VersionSupport.SUPPORTED


def test_identifies_unknown_official_kimi_code_without_accepting_version_alone(
    unlisted_kimi_code_version: str,
) -> None:
    version_output = f"{unlisted_kimi_code_version}\n"
    identity = identify_kimi_executable(version_output, KIMI_CODE_HELP)

    assert identity.product is KimiProduct.KIMI_CODE
    assert identity.version == unlisted_kimi_code_version
    assert identity.support is VersionSupport.UNKNOWN

    with pytest.raises(KimiProductFingerprintError):
        identify_kimi_executable(version_output, "Usage: kimi [options]")


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


def test_unknown_warning_is_prominent_and_actionable(
    unlisted_kimi_code_version: str,
) -> None:
    warning = unknown_version_warning(unlisted_kimi_code_version)

    assert "UNTESTED KIMI CODE VERSION" in warning
    assert unlisted_kimi_code_version in warning
    assert KIMI_CODE_INSTALL_URL in warning


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schema_version": 2, "releases": []}, "unsupported"),
        ({"schema_version": 1, "releases": []}, "no releases"),
        (
            {
                "schema_version": 1,
                "releases": [
                    {"bridge": "0.2.0", "kimi_code": ["0.29.0"]},
                    {"bridge": "0.1.0", "kimi_code": ["0.29.0"]},
                ],
            },
            "unique and sorted",
        ),
        (
            {
                "schema_version": 1,
                "releases": [
                    {"bridge": "0.1.0", "kimi_code": ["0.29.0"]},
                    {"bridge": "0.1.0", "kimi_code": ["0.29.0"]},
                ],
            },
            "unique and sorted",
        ),
        (
            {
                "schema_version": 1,
                "releases": [
                    {"bridge": "0.1.0", "kimi_code": ["0.29.1", "0.29.0"]}
                ],
            },
            "unique and sorted",
        ),
        (
            {
                "schema_version": 1,
                "releases": [{"bridge": "0.1.0", "kimi_code": ["not-semver"]}],
            },
            "malformed",
        ),
        (
            {"schema_version": 1, "releases": [{"bridge": "0.1.0"}]},
            "non-empty",
        ),
    ],
)
def test_compatibility_map_loader_rejects_invalid_payloads(
    payload: dict, message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _parse_compatibility_map(payload)


_FAKE_RELEASES = (
    CompatibilityMapEntry("0.1.0", ("0.28.0", "0.29.0")),
    CompatibilityMapEntry("0.2.0", ("0.29.0", "0.29.1")),
)


def test_classifier_supports_the_current_bridge_list() -> None:
    verdict = classify_bridge_compatibility("0.29.0", releases=_FAKE_RELEASES)

    assert verdict.support is BridgeSupport.SUPPORTED_BY_CURRENT_BRIDGE
    assert verdict.releases == ()


def test_classifier_names_other_supporting_releases() -> None:
    verdict = classify_bridge_compatibility("0.28.0", releases=_FAKE_RELEASES)

    assert verdict.support is BridgeSupport.SUPPORTED_BY_OTHER_RELEASES
    assert verdict.releases == ("0.1.0",)


def test_classifier_distinguishes_untested_direction() -> None:
    newer = classify_bridge_compatibility("0.30.0", releases=_FAKE_RELEASES)
    older = classify_bridge_compatibility("0.27.0", releases=_FAKE_RELEASES)

    assert newer.support is BridgeSupport.UNTESTED_NEWER_THAN_ALL
    assert older.support is BridgeSupport.UNTESTED_OLDER_THAN_ALL


def test_classifier_marks_untested_versions_inside_the_tested_range() -> None:
    verdict = classify_bridge_compatibility("0.28.1", releases=_FAKE_RELEASES)

    assert verdict.support is BridgeSupport.UNTESTED_WITHIN_TESTED_RANGE
    assert verdict.tested_range == ("0.28.0", "0.29.1")


def test_classifier_requires_a_non_empty_map() -> None:
    with pytest.raises(RuntimeError, match="no releases"):
        classify_bridge_compatibility("0.29.0", releases=())


def test_classifier_honors_the_current_bridge_override() -> None:
    verdict = classify_bridge_compatibility(
        "0.29.1", releases=_FAKE_RELEASES, current_bridge="0.1.0"
    )

    assert verdict.support is BridgeSupport.SUPPORTED_BY_OTHER_RELEASES
    assert verdict.releases == ("0.2.0",)


def test_classifier_rejects_malformed_versions() -> None:
    with pytest.raises(ValueError, match="malformed"):
        classify_bridge_compatibility("not-a-version", releases=_FAKE_RELEASES)
