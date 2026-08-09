"""Readable terminal formatting shared by platform QR controls."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TextIO


def write_qr_url(
    stream: TextIO,
    title: str,
    url: str,
    *,
    instructions: Iterable[str] = (),
) -> None:
    """Write one QR URL and its instructions as a spaced terminal block."""

    stream.write(f"\n{title}\n\n")
    stream.write(f"  {url}\n\n")
    for instruction in instructions:
        stream.write(f"{instruction}\n")
    stream.write("\n")
    stream.flush()


__all__ = ["write_qr_url"]
