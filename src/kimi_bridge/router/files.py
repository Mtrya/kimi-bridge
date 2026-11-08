"""Workspace-contained inbound and outbound file handling."""

from __future__ import annotations

import os
from pathlib import Path

from ..platforms.base import InboundFile


# Mobile IMEs often auto-convert a leading "~" into the fullwidth "～"
# (U+FF5E), which Path.expanduser() does not recognize.
_FULLWIDTH_TILDE = "\uff5e"


def _expand_fullwidth_tilde(candidate: Path, argument: str) -> Path:
    """Retry a user-typed path whose leading tilde a mobile IME widened.

    The literal candidate wins first; the "~" retry only runs when the
    candidate does not exist and the argument starts with "～" (alone or
    followed by a platform path separator), so a real directory named "～"
    keeps working.
    """

    if candidate.exists():
        return candidate
    separators = {os.sep, os.altsep} - {None}
    if argument.startswith(_FULLWIDTH_TILDE) and (
        len(argument) == 1 or argument[1] in separators
    ):
        return Path("~" + argument[1:]).expanduser()
    return candidate


def _save_inbound_files(
    workspace: Path,
    inbox_subdir: str,
    files: tuple[InboundFile, ...],
) -> list[Path]:
    inbox = workspace / inbox_subdir
    inbox.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for inbound in files:
        filename = Path(inbound.name).name.strip()
        if filename in {"", ".", ".."}:
            filename = "attachment"
        stem = Path(filename).stem or "attachment"
        suffix = Path(filename).suffix
        candidate = inbox / filename
        index = 1
        while candidate.exists():
            candidate = inbox / f"{stem}-{index}{suffix}"
            index += 1
        candidate.write_bytes(inbound.data)
        saved.append(candidate.resolve())
    return saved
