"""Message boundaries for QQ's rendered Markdown subset."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
import re


_MARKERS = re.compile(r"\*+|_+")
_SPACE = "\u200b"


def _emphasis(text: str) -> tuple[dict[int, str], set[int], int | None]:
    """Track paired emphasis delimiters, leaving literal markers untouched."""
    stack: list[tuple[int, str]] = []
    pairs: dict[int, tuple[str, bool]] = {}
    forbidden: set[int] = set()
    for match in _MARKERS.finditer(text):
        start, end = match.span()
        marker = match.group()
        if len(marker) <= 3:
            forbidden.update(range(start + 1, end))
        escaped = 0
        cursor = start - 1
        while cursor >= 0 and text[cursor] == "\\":
            escaped += 1
            cursor -= 1
        if escaped % 2 or len(marker) > 3:
            continue
        before = text[start - 1] if start else " "
        after = text[end] if end < len(text) else " "
        if marker[0] == "_" and before.isalnum() and after.isalnum():
            continue
        can_close = not before.isspace()
        can_open = not after.isspace()
        cursor = start
        if can_close:
            while cursor < end and stack and stack[-1][1] == marker[0]:
                opening, character = stack.pop()
                pairs[opening] = (character, True)
                pairs[cursor] = (character, False)
                cursor += 1
        if can_open:
            stack.extend((position, marker[0]) for position in range(cursor, end))
    active: list[str] = []
    states = {0: ""}
    for position, (marker, opening) in sorted(pairs.items()):
        if opening:
            active.append(marker)
        else:
            active.pop()
        states[position + 1] = "".join(active)
    # Never separate an escape from its following character.
    for match in re.finditer(r"\\.", text):
        forbidden.add(match.start() + 1)
    return states, forbidden, stack[0][0] if stack else None


def stable_emphasis(text: str) -> str:
    """Hold an unfinished emphasis span until its closing delimiter arrives."""
    _, _, unfinished = _emphasis(text)
    return text if unfinished is None else text[:unfinished]


def split_markdown(
    text: str, limit: int, *, measure: Callable[[str], int] = len
) -> list[str]:
    """Split rendered text, reopening paired emphasis across each boundary.

    Only the last segment can grow. A full segment's cut depends on its own
    prefix, so appending text cannot move an already completed boundary.
    """
    if measure(text) <= limit:
        return [text]
    states, forbidden, _ = _emphasis(text)
    offsets = sorted(states)

    def active_at(position: int) -> str:
        return states[offsets[bisect_right(offsets, position) - 1]]

    def segment(start: int, end: int) -> str:
        body = text[start:end]
        opening = active_at(start)
        closing = active_at(end)[::-1]
        # Put synthetic delimiters inside whitespace, where QQ recognizes them.
        leading = len(body) - len(body.lstrip()) if opening else 0
        trailing = len(body.rstrip()) if closing else len(body)
        if trailing < leading:
            return body
        suffix = ""
        if closing:
            suffix = ("" if body[:trailing].endswith(_SPACE) else _SPACE) + closing
        return (
            body[:leading] + opening + body[leading:trailing] + suffix + body[trailing:]
        )

    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + limit - len(active_at(start)))
        while end > start and (
            end in forbidden or measure(segment(start, end)) > limit
        ):
            end -= 1
        if end == start:
            raise ValueError("QQ message limit cannot fit an emphasis delimiter")
        if end < len(text):
            # Prefer nearby paragraph, line, then word boundaries without making
            # sparse messages when a long token has no suitable boundary.
            floor = start + (end - start) // 2
            for separator in ("\n\n", "\n", " "):
                boundary = text.rfind(separator, floor, end)
                if boundary >= floor:
                    candidate = boundary + len(separator)
                    if (
                        candidate not in forbidden
                        and measure(segment(start, candidate)) <= limit
                    ):
                        end = candidate
                        break
        parts.append(segment(start, end))
        start = end
    return parts
