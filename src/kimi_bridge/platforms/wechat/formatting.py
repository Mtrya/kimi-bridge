"""Whole-message text normalization for immutable WeChat replies."""

from __future__ import annotations

from dataclasses import dataclass
import re


_INLINE_IMAGE = re.compile(r"!\[([^]\n]*)\]\([^\n)]*\)")
_REFERENCE_IMAGE = re.compile(r"!\[([^]\n]*)\]\[[^]\n]*\]")
_INLINE_LINK = re.compile(r"(?<!!)\[([^]\n]+)\]\(([^\s)]+)(?:\s+[^)]*)?\)")
_LINK_DEFINITION = re.compile(r"^[ \t]*\[[^]\n]+\]:[ \t]+\S+.*$", re.MULTILINE)
_HEADING = re.compile(r"^(?:#{1,6})[ \t]+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^[ \t]*(?:>[ \t]?)+", re.MULTILINE)
_FENCE_START = re.compile(r"^[ \t]{0,3}([`~]{3,})(.*)$")
_FENCE_END = re.compile(r"^[ \t]{0,3}([`~]+)[ \t]*$")


@dataclass(slots=True)
class MarkdownFormatter:
    """Incrementally sanitize Markdown while retaining fenced-code state."""

    fence_char: str | None = None
    fence_length: int = 0

    def copy(self) -> MarkdownFormatter:
        return MarkdownFormatter(self.fence_char, self.fence_length)

    def sanitize(self, text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("WeChat message text must be a string")

        lines = text.splitlines(keepends=True)
        output: list[str] = []
        prose: list[str] = []
        for line in lines:
            if self.fence_char is not None:
                if self._is_closing_fence(line):
                    output.append(line)
                    self.fence_char = None
                    self.fence_length = 0
                else:
                    output.append(line)
                continue

            opening = _opening_fence(line)
            if opening is not None:
                if prose:
                    output.append(_sanitize_prose("".join(prose)))
                    prose.clear()
                self.fence_char, self.fence_length = opening
                output.append(line)
            else:
                prose.append(line)

        if prose:
            output.append(_sanitize_prose("".join(prose)))
        return "".join(output)

    def _is_closing_fence(self, line: str) -> bool:
        content = _without_line_ending(line)
        match = _FENCE_END.fullmatch(content)
        if match is None:
            return False
        run = match.group(1)
        return (
            run[0] == self.fence_char
            and len(run) >= self.fence_length
            and len(set(run)) == 1
        )


def sanitize_markdown(text: str) -> str:
    """Keep readable text and code while removing unsupported constructs."""

    return MarkdownFormatter().sanitize(text)


def _opening_fence(line: str) -> tuple[str, int] | None:
    content = _without_line_ending(line)
    match = _FENCE_START.fullmatch(content)
    if match is None:
        return None
    run = match.group(1)
    if len(set(run)) != 1:
        return None
    if run[0] == "`" and "`" in match.group(2):
        return None
    return run[0], len(run)


def _without_line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(("\n", "\r")):
        return line[:-1]
    return line


def _sanitize_prose(text: str) -> str:
    parts = _split_inline_code(text)
    for index in range(0, len(parts), 2):
        part = parts[index]
        part = _LINK_DEFINITION.sub("", part)
        part = _HEADING.sub("", part)
        part = _BLOCKQUOTE.sub("", part)
        part = _sanitize_links_and_images(part)
        parts[index] = _sanitize_strikethrough(part)
    return "".join(parts)


def _split_inline_code(text: str) -> list[str]:
    parts: list[str] = []
    prose_start = 0
    index = 0
    while index < len(text):
        if text[index] != "`" or _is_escaped(text, index):
            index += 1
            continue
        closing = _find_inline_code_close(text, index + 1)
        if closing is None:
            index += 1
            continue
        parts.extend((text[prose_start:index], text[index : closing + 1]))
        index = closing + 1
        prose_start = index
    parts.append(text[prose_start:])
    return parts


def _find_inline_code_close(text: str, start: int) -> int | None:
    newline = text.find("\n", start)
    end = len(text) if newline < 0 else newline
    index = start
    while index < end:
        if text[index] == "`" and not _is_escaped(text, index):
            return index
        index += 1
    return None


def _sanitize_links_and_images(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("![", index) and not _is_escaped(text, index):
            match = _INLINE_IMAGE.match(text, index)
            if match is None:
                match = _REFERENCE_IMAGE.match(text, index)
            if match is not None:
                output.append(match.group(1))
                index = match.end()
                continue
        elif (
            text[index] == "["
            and not _is_escaped(text, index)
            and (index == 0 or text[index - 1] != "!")
        ):
            match = _INLINE_LINK.match(text, index)
            if match is not None:
                output.append(f"{match.group(1)} ({match.group(2)})")
                index = match.end()
                continue
        output.append(text[index])
        index += 1
    return "".join(output)


def _sanitize_strikethrough(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("~~", index) and not _is_escaped(text, index):
            closing = _find_strikethrough_close(text, index + 2)
            if closing is not None:
                output.append(text[index + 2 : closing])
                index = closing + 2
                continue
        output.append(text[index])
        index += 1
    return "".join(output)


def _find_strikethrough_close(text: str, start: int) -> int | None:
    line_ends = [
        index
        for marker in ("\n", "\r")
        if (index := text.find(marker, start)) >= 0
    ]
    limit = min(line_ends, default=len(text))
    index = start
    while True:
        index = text.find("~~", index, limit)
        if index < 0:
            return None
        if not _is_escaped(text, index):
            return index
        index += 2


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1
