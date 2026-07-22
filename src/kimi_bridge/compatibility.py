"""Product identity and tested-version policy for Kimi Code."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


KIMI_CODE_INSTALL_URL = (
    "https://moonshotai.github.io/kimi-code/en/guides/getting-started"
)
SUPPORTED_KIMI_CODE_VERSIONS = frozenset({"0.28.1"})

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_VERSION_PATTERN = (
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?"
)
_VERSION_RE = re.compile(rf"(?P<version>{_VERSION_PATTERN})")
_LEGACY_VERSION_RE = re.compile(
    rf"kimi, version (?P<version>{_VERSION_PATTERN})"
)
_KIMI_CODE_HELP_MARKERS = (
    "Usage: kimi [options] [command]",
    "web [options]",
    "doctor",
    "migrate",
)
_LEGACY_KIMI_CLI_HELP_MARKERS = (
    "Usage: kimi [OPTIONS] COMMAND [ARGS]...",
    "--mcp-config-file",
    "moonshotai.github.io/kimi-cli/",
)


class KimiProduct(str, Enum):
    """Products that have shipped a command named ``kimi``."""

    KIMI_CODE = "kimi-code"
    LEGACY_KIMI_CLI = "legacy-kimi-cli"


class VersionSupport(str, Enum):
    """Whether a Kimi Code version has passed the tracked contract."""

    SUPPORTED = "supported"
    UNKNOWN = "unknown"


class KimiProductFingerprintError(ValueError):
    """The CLI output did not establish a recognized Kimi product identity."""


@dataclass(frozen=True, slots=True)
class KimiExecutableIdentity:
    """Normalized identity collected from non-starting Kimi CLI surfaces."""

    product: KimiProduct
    version: str

    @property
    def support(self) -> VersionSupport:
        if self.product is not KimiProduct.KIMI_CODE:
            return VersionSupport.UNKNOWN
        return classify_kimi_code_version(self.version)


def identify_kimi_executable(
    version_output: str, help_output: str
) -> KimiExecutableIdentity:
    """Identify current Kimi Code or the incompatible legacy Python CLI.

    The version command alone is deliberately insufficient for current Kimi
    Code. Its plain semantic version must be accompanied by the structural
    ``web``/``doctor``/``migrate`` product fingerprint from top-level help.
    """

    version_text = _plain(version_output).strip()
    help_text = _plain(help_output)

    current_match = _VERSION_RE.fullmatch(version_text)
    if current_match is not None and _has_markers(
        help_text, _KIMI_CODE_HELP_MARKERS
    ):
        return KimiExecutableIdentity(
            product=KimiProduct.KIMI_CODE,
            version=current_match.group("version"),
        )

    legacy_match = _LEGACY_VERSION_RE.fullmatch(version_text)
    if legacy_match is not None and _has_markers(
        help_text, _LEGACY_KIMI_CLI_HELP_MARKERS
    ):
        return KimiExecutableIdentity(
            product=KimiProduct.LEGACY_KIMI_CLI,
            version=legacy_match.group("version"),
        )

    raise KimiProductFingerprintError(
        "the 'kimi' executable did not provide a recognized Kimi Code product "
        "fingerprint"
    )


def normalize_kimi_code_version(version: str) -> str:
    """Validate and normalize a version advertised by official Kimi Code."""

    normalized = _plain(version).strip()
    match = _VERSION_RE.fullmatch(normalized)
    if match is None:
        raise ValueError("Kimi Code reported a malformed version")
    return match.group("version")


def classify_kimi_code_version(version: str) -> VersionSupport:
    """Classify one normalized official Kimi Code version."""

    normalized = normalize_kimi_code_version(version)
    if normalized in SUPPORTED_KIMI_CODE_VERSIONS:
        return VersionSupport.SUPPORTED
    return VersionSupport.UNKNOWN


def unknown_version_warning(version: str) -> str:
    """Return the common actionable warning for untested official versions."""

    normalized = normalize_kimi_code_version(version)
    return (
        f"UNTESTED KIMI CODE VERSION {normalized}: this version is not in the "
        "bridge's tested compatibility manifest. Continuing with live protocol "
        f"checks. Installation and support guidance: {KIMI_CODE_INSTALL_URL}"
    )


def legacy_product_message(version: str) -> str:
    """Return the actionable failure for the incompatible Python product."""

    normalized = normalize_kimi_code_version(version)
    return (
        f"legacy Python kimi-cli {normalized} is incompatible with kimi-bridge; "
        f"install current Kimi Code instead: {KIMI_CODE_INSTALL_URL}"
    )


def _plain(value: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", value)


def _has_markers(text: str, markers: tuple[str, ...]) -> bool:
    return all(marker in text for marker in markers)
