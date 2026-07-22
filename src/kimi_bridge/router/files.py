"""Workspace-contained inbound and outbound file handling."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from ..platforms.base import InboundFile, OutboundFile


def _load_outbound_file(workspace: Path, argument: str) -> OutboundFile:
    workspace = workspace.expanduser().resolve()
    requested = Path(argument).expanduser()
    candidate = requested if requested.is_absolute() else workspace / requested
    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace):
        raise ValueError("File must stay inside the bound workspace.")
    if not resolved.exists():
        raise ValueError(f"File not found: {argument}")
    if not resolved.is_file():
        raise ValueError(f"Not a regular file: {argument}")
    media_type = mimetypes.guess_type(resolved.name)[0]
    return OutboundFile(
        name=resolved.name,
        data=resolved.read_bytes(),
        media_type=media_type or "application/octet-stream",
    )


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
