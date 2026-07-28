"""Product identity and tested-version policy for Kimi Code."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from importlib.resources import files


KIMI_CODE_INSTALL_URL = (
    "https://moonshotai.github.io/kimi-code/en/guides/getting-started"
)
SUPPORTED_VERSION_MANIFEST_RESOURCE = "supported-kimi-code-versions.json"
COMPATIBILITY_MAP_RESOURCE = "compatibility-map.json"

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


def _load_supported_versions() -> frozenset[str]:
    raw = files("kimi_bridge").joinpath(
        SUPPORTED_VERSION_MANIFEST_RESOURCE
    ).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if payload.get("schema_version") != 1:
        raise RuntimeError("unsupported Kimi compatibility manifest version")
    versions = payload.get("versions")
    if not isinstance(versions, list) or not versions:
        raise RuntimeError("Kimi compatibility manifest has no versions")
    normalized = tuple(normalize_kimi_code_version(item) for item in versions)
    if list(normalized) != sorted(set(normalized), key=kimi_code_version_sort_key):
        raise RuntimeError(
            "Kimi compatibility manifest versions must be unique and sorted"
        )
    return frozenset(normalized)


@dataclass(frozen=True, slots=True)
class CompatibilityMapEntry:
    """One bridge release and the Kimi Code versions it was tested with."""

    bridge: str
    kimi_code: tuple[str, ...]


class BridgeSupport(str, Enum):
    """How one Kimi Code version relates to the bridge release history."""

    SUPPORTED_BY_CURRENT_BRIDGE = "supported-by-current-bridge"
    SUPPORTED_BY_OTHER_RELEASES = "supported-by-other-releases"
    UNTESTED_NEWER_THAN_ALL = "untested-newer-than-all"
    UNTESTED_OLDER_THAN_ALL = "untested-older-than-all"


@dataclass(frozen=True, slots=True)
class BridgeCompatibility:
    """Verdict for one Kimi Code version against the compatibility map."""

    support: BridgeSupport
    releases: tuple[str, ...] = ()


def _parse_compatibility_map(payload: object) -> tuple[CompatibilityMapEntry, ...]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("unsupported Kimi compatibility map version")
    releases = payload.get("releases")
    if not isinstance(releases, list) or not releases:
        raise RuntimeError("Kimi compatibility map has no releases")
    entries: list[CompatibilityMapEntry] = []
    for release in releases:
        if not isinstance(release, dict):
            raise RuntimeError("Kimi compatibility map releases must be objects")
        bridge_raw = release.get("bridge")
        kimi_raw = release.get("kimi_code")
        if (
            not isinstance(bridge_raw, str)
            or not isinstance(kimi_raw, list)
            or not kimi_raw
        ):
            raise RuntimeError(
                "Kimi compatibility map releases must name a bridge version "
                "and a non-empty Kimi Code version list"
            )
        try:
            bridge = normalize_kimi_code_version(bridge_raw)
            kimi_code = tuple(normalize_kimi_code_version(item) for item in kimi_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Kimi compatibility map contains a malformed version"
            ) from exc
        if list(kimi_code) != sorted(set(kimi_code), key=kimi_code_version_sort_key):
            raise RuntimeError(
                "Kimi compatibility map version lists must be unique and sorted"
            )
        entries.append(CompatibilityMapEntry(bridge=bridge, kimi_code=kimi_code))
    bridges = [entry.bridge for entry in entries]
    if bridges != sorted(set(bridges), key=kimi_code_version_sort_key):
        raise RuntimeError(
            "Kimi compatibility map releases must be unique and sorted"
        )
    return tuple(entries)


def _load_compatibility_map() -> tuple[CompatibilityMapEntry, ...]:
    raw = files("kimi_bridge").joinpath(COMPATIBILITY_MAP_RESOURCE).read_text(
        encoding="utf-8"
    )
    return _parse_compatibility_map(json.loads(raw))


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


def kimi_code_version_sort_key(version: str) -> tuple[int, int, int, int, str]:
    """Return a deterministic semantic ordering key for manifest updates."""

    normalized = normalize_kimi_code_version(version)
    without_build = normalized.split("+", 1)[0]
    core, separator, prerelease = without_build.partition("-")
    major, minor, patch = (int(item) for item in core.split("."))
    return major, minor, patch, 0 if separator else 1, prerelease


def classify_kimi_code_version(version: str) -> VersionSupport:
    """Classify one normalized official Kimi Code version."""

    normalized = normalize_kimi_code_version(version)
    if normalized in SUPPORTED_KIMI_CODE_VERSIONS:
        return VersionSupport.SUPPORTED
    return VersionSupport.UNKNOWN


def classify_bridge_compatibility(
    kimi_version: str,
    *,
    releases: tuple[CompatibilityMapEntry, ...] | None = None,
    current_bridge: str | None = None,
) -> BridgeCompatibility:
    """Classify one Kimi Code version against the bridge release history."""

    entries = COMPATIBILITY_MAP if releases is None else releases
    if not entries:
        raise RuntimeError("Kimi compatibility map has no releases")
    normalized = normalize_kimi_code_version(kimi_version)
    current = entries[-1]
    if current_bridge is not None:
        current = next(
            (entry for entry in entries if entry.bridge == current_bridge),
            entries[-1],
        )
    if normalized in current.kimi_code:
        return BridgeCompatibility(BridgeSupport.SUPPORTED_BY_CURRENT_BRIDGE)
    others = tuple(
        entry.bridge
        for entry in entries
        if entry is not current and normalized in entry.kimi_code
    )
    if others:
        return BridgeCompatibility(
            BridgeSupport.SUPPORTED_BY_OTHER_RELEASES, others
        )
    tested_keys = [
        kimi_code_version_sort_key(version)
        for entry in entries
        for version in entry.kimi_code
    ]
    if kimi_code_version_sort_key(normalized) < min(tested_keys):
        return BridgeCompatibility(BridgeSupport.UNTESTED_OLDER_THAN_ALL)
    return BridgeCompatibility(BridgeSupport.UNTESTED_NEWER_THAN_ALL)


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


SUPPORTED_KIMI_CODE_VERSIONS = _load_supported_versions()
COMPATIBILITY_MAP = _load_compatibility_map()
