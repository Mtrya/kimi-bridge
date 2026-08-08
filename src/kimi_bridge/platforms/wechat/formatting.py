"""Whole-message text normalization for immutable WeChat replies."""

from __future__ import annotations

import re


_INLINE_CODE = re.compile(r"(`[^`\n]*`)")
_INLINE_IMAGE = re.compile(r"!\[([^]\n]*)\]\([^\n)]*\)")
_REFERENCE_IMAGE = re.compile(r"!\[([^]\n]*)\]\[[^]\n]*\]")
_INLINE_LINK = re.compile(r"(?<!!)\[([^]\n]+)\]\(([^\s)]+)(?:\s+[^)]*)?\)")
_LINK_DEFINITION = re.compile(r"^[ \t]*\[[^]\n]+\]:[ \t]+\S+.*$", re.MULTILINE)
_HEADING = re.compile(r"^(?:#{1,6})[ \t]+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^[ \t]*(?:>[ \t]?)+", re.MULTILINE)


def sanitize_markdown(text: str) -> str:
    """Keep readable text and code while removing unsupported constructs."""

    if not isinstance(text, str):
        raise TypeError("WeChat message text must be a string")
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    prose: list[str] = []
    in_fence = False

    def flush_prose() -> None:
        if prose:
            output.append(_sanitize_prose("".join(prose)))
            prose.clear()

    for line in lines:
        if line.lstrip().startswith("```"):
            flush_prose()
            output.append(line)
            in_fence = not in_fence
        elif in_fence:
            output.append(line)
        else:
            prose.append(line)
    flush_prose()
    return "".join(output)


def _sanitize_prose(text: str) -> str:
    parts = _INLINE_CODE.split(text)
    for index in range(0, len(parts), 2):
        part = parts[index]
        part = _INLINE_IMAGE.sub(lambda match: match.group(1), part)
        part = _REFERENCE_IMAGE.sub(lambda match: match.group(1), part)
        part = _LINK_DEFINITION.sub("", part)
        part = _INLINE_LINK.sub(
            lambda match: f"{match.group(1)} ({match.group(2)})",
            part,
        )
        part = _HEADING.sub("", part)
        part = _BLOCKQUOTE.sub("", part)
        part = part.replace("~~", "")
        parts[index] = part
    return "".join(parts)
