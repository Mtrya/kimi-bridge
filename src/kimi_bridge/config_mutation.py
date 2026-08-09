"""Small, comment-preserving TOML mutations used by QR onboarding."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Iterable
from pathlib import Path


_SUPPORTED_PLATFORMS = frozenset({"feishu", "telegram", "qq", "wechat"})
_TABLE_HEADER_RE = re.compile(
    r"^[ \t]*\[\[(?P<array>[^\]\r\n]+)\]\]|"
    r"^[ \t]*\[(?P<table>[^\]\r\n]+)\]"
)
_PLATFORM_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>[ \t]*platform[ \t]*=[ \t]*)"
    r"(?P<quote>[\"'])(?P<value>[^\"'\r\n]*)"
    r"(?P=quote)(?P<suffix>[ \t]*(?:#.*)?)(?P<newline>\r?\n?)$"
)
_ALLOWED_USERS_ASSIGNMENT_RE = re.compile(r"^[ \t]*allowed_users[ \t]*=", re.MULTILINE)


class ConfigMutationError(ValueError):
    """The existing TOML cannot be safely changed by onboarding."""


def set_platform(path: str | Path, platform: str) -> None:
    """Set the top-level selected platform without rewriting other TOML text."""

    _require_platform(platform)
    config_path = Path(path).expanduser()
    text = _read_text(config_path)
    newline = _line_ending(text)

    if not text.strip():
        updated = f'platform = "{platform}"{newline}'
    else:
        _parse_toml(text, config_path)
        lines = text.splitlines(keepends=True)
        replacement_index: int | None = None
        for index, line in enumerate(lines):
            if _TABLE_HEADER_RE.match(line):
                break
            if _PLATFORM_ASSIGNMENT_RE.match(line):
                replacement_index = index
                break
        if replacement_index is None:
            updated = f'platform = "{platform}"{newline}{text}'
        else:
            line = lines[replacement_index]
            match = _PLATFORM_ASSIGNMENT_RE.match(line)
            assert match is not None
            lines[replacement_index] = (
                f'{match.group("prefix")}"{platform}"'
                f"{match.group('suffix')}{match.group('newline')}"
            )
            updated = "".join(lines)

    _validate_and_write(config_path, updated)


def merge_allowed_user(
    path: str | Path,
    table: str,
    user_id: str,
    *,
    existing_users: Iterable[str] | None = None,
) -> bool:
    """Add ``user_id`` to one table's allowlist, returning whether it was new."""

    if not table or not re.fullmatch(r"[A-Za-z0-9_-]+", table):
        raise ValueError("config table name must be a simple TOML table name")
    user_id = user_id.strip()
    if not user_id:
        raise ValueError("allowed user identity must be non-empty")

    config_path = Path(path).expanduser()
    text = _read_text(config_path)
    newline = _line_ending(text)
    raw = _parse_toml(text, config_path) if text.strip() else {}
    table_value = raw.get(table, {})
    if table_value is None:
        table_value = {}
    if not isinstance(table_value, dict):
        raise ConfigMutationError(f"[{table}] must be a TOML table")

    raw_users = table_value.get("allowed_users", [])
    if not isinstance(raw_users, list) or any(
        not isinstance(value, str) or not value.strip() for value in raw_users
    ):
        raise ConfigMutationError(
            f"[{table}].allowed_users must be an array of non-empty strings"
        )
    users = list(existing_users if existing_users is not None else raw_users)
    if any(not isinstance(value, str) or not value.strip() for value in users):
        raise ConfigMutationError(
            f"[{table}].allowed_users must be an array of non-empty strings"
        )
    if user_id in users:
        return False
    users.append(user_id)
    serialized_users = _serialize_string_array(users)

    if not text.strip():
        updated = f"[{table}]{newline}allowed_users = {serialized_users}{newline}"
    else:
        lines = text.splitlines(keepends=True)
        section = _find_table_section(lines, table)
        if section is None:
            separator = "" if text.endswith(("\n", "\r")) else newline
            if text and not text.endswith((newline, newline + newline)):
                separator += newline
            updated = (
                text
                + separator
                + f"[{table}]{newline}"
                + f"allowed_users = {serialized_users}{newline}"
            )
        else:
            section_start, section_end = section
            section_text = "".join(lines[section_start:section_end])
            assignment = _ALLOWED_USERS_ASSIGNMENT_RE.search(section_text)
            if assignment is not None:
                assignment_start = (
                    sum(len(line) for line in lines[:section_start])
                    + assignment.start()
                )
                array_start = _array_start(
                    text, assignment_start + assignment.group(0).__len__()
                )
                array_end = _array_end(text, array_start)
                updated = text[:array_start] + serialized_users + text[array_end:]
            else:
                insertion_offset = sum(len(line) for line in lines[:section_end])
                prefix = (
                    ""
                    if insertion_offset == 0
                    or text[:insertion_offset].endswith(("\n", "\r"))
                    else newline
                )
                insertion = prefix + f"allowed_users = {serialized_users}{newline}"
                updated = text[:insertion_offset] + insertion + text[insertion_offset:]

    _validate_and_write(config_path, updated)
    return True


def _require_platform(platform: str) -> None:
    if platform not in _SUPPORTED_PLATFORMS:
        choices = ", ".join(sorted(_SUPPORTED_PLATFORMS))
        raise ValueError(f"platform must be one of: {choices}")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        raise OSError(f"could not read configuration file {path}") from exc


def _parse_toml(text: str, path: Path) -> dict[str, object]:
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigMutationError(
            f"configuration file {path} is not valid TOML"
        ) from exc
    return payload


def _validate_and_write(path: Path, text: str) -> None:
    _parse_toml(text, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    except OSError as exc:
        raise OSError(f"could not inspect configuration file {path}") from exc

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as config_file:
            config_file.write(text)
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(temporary_path, path)
        if os.name == "posix":
            path.chmod(mode)
    finally:
        temporary_path.unlink(missing_ok=True)


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _serialize_string_array(values: Iterable[str]) -> str:
    return (
        "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"
    )


def _find_table_section(lines: list[str], table: str) -> tuple[int, int] | None:
    section_start: int | None = None
    for index, line in enumerate(lines):
        match = _TABLE_HEADER_RE.match(line)
        if match is None or match.group("array") is not None:
            continue
        name = match.group("table")
        if section_start is not None:
            return section_start, index
        if name is not None and name.strip() == table:
            section_start = index
    if section_start is None:
        return None
    return section_start, len(lines)


def _array_start(text: str, offset: int) -> int:
    index = offset
    while index < len(text) and text[index] in " \t":
        index += 1
    if index >= len(text) or text[index] != "[":
        raise ConfigMutationError("allowed_users must be a TOML array")
    return index


def _array_end(text: str, start: int) -> int:
    depth = 0
    quote: str | None = None
    escaped = False
    index = start
    while index < len(text):
        char = text[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
        elif quote == "'":
            if char == "'":
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char == "#":
            newline = text.find("\n", index)
            if newline == -1:
                break
            index = newline
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise ConfigMutationError("allowed_users must be a complete TOML array")


__all__ = ["ConfigMutationError", "merge_allowed_user", "set_platform"]
