from __future__ import annotations

import pytest

from kimi_bridge.platforms.qq.markdown import split_markdown, stable_emphasis


@pytest.mark.parametrize("marker", ["*", "**", "***", "_", "__", "___"])
def test_split_reopens_emphasis_and_counts_synthetic_delimiters(marker: str) -> None:
    body = "文字。" * 40
    parts = split_markdown(marker + body + marker, 32)
    assert all(len(part) <= 32 for part in parts)
    assert all(part.startswith(marker) and part.endswith(marker) for part in parts)
    assert (
        "".join(
            part[len(marker) : -len(marker)].replace("\u200b", "") for part in parts
        )
        == body
    )


def test_split_reopens_nested_emphasis_in_order() -> None:
    body = "text" * 40
    parts = split_markdown("**_" + body + "_**", 32)
    assert all(part.startswith("**_") and part.endswith("_**") for part in parts)
    assert "".join(part[3:-3].replace("\u200b", "") for part in parts) == body


def test_split_preserves_literal_markers_and_escaped_delimiters() -> None:
    source = r"some_identifier and \*literal\* " * 40
    assert "".join(split_markdown(source, 32)) == source
    assert "".join(split_markdown("*" * 100, 32)) == "*" * 100


@pytest.mark.parametrize(
    "suffix", ["**unfinished\n", "*unfinished\n", "__unfinished\n"]
)
def test_stream_holds_an_unclosed_emphasis_span(suffix: str) -> None:
    assert stable_emphasis("prefix\n" + suffix) == "prefix\n"
    assert stable_emphasis("**closed**\n") == "**closed**\n"


def test_split_preserves_whitespace_around_reopened_emphasis() -> None:
    body = "word " * 50 + "end"
    parts = split_markdown("**" + body + "**", 32)
    assert (
        "".join(part.replace("**", "").replace("\u200b", "") for part in parts) == body
    )
    assert all(part.rstrip().endswith("**") for part in parts)
    assert all(not part.rstrip().endswith(" **") for part in parts)


def test_appending_text_does_not_move_completed_boundaries() -> None:
    source = "**" + "text " * 50 + "end**\n"
    parts = split_markdown(source, 32)
    extended = split_markdown(source + "next paragraph " * 30, 32)
    assert extended[: len(parts) - 1] == parts[:-1]
