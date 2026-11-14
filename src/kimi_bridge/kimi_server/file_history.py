"""Recorded terminal-turn comparisons from native checkpoints."""

from __future__ import annotations

import asyncio
import difflib
import ntpath
import posixpath
from io import StringIO
from pathlib import PureWindowsPath

from .types import FileChange, FileChanges, FileDiff, RecordedTurn
from .workspace import _local_input


class FileHistoryOperations:
    async def get_file_changes(
        self, session_id: str, *, turn: RecordedTurn | None = None
    ) -> FileChanges:
        if turn is None:
            turn = await self._latest_recorded_turn(session_id)
            if turn is None:
                return FileChanges(None, False, ())
        data = await self._request_operation(
            "file_changes",
            path_parameters={"session_id": session_id},
            params={"turn_number": turn.number},
        )
        changes = tuple(
            FileChange(
                value["path"],
                value["status"],
                value["additions"],
                value["deletions"],
                value.get("binary", False),
                value.get("oversize", False),
            )
            for value in data["changes"]
        )
        return FileChanges(turn, data["recorded"], changes)

    async def _latest_recorded_turn(self, session_id: str) -> RecordedTurn | None:
        async for page in self._turn_pages(session_id, "main", page_size=200):
            for turn in reversed(page["turns"]):
                number = turn.get("turn_number")
                if (
                    turn.get("kind") == "agent"
                    and turn.get("status") == "ended"
                    and turn.get("steps")
                    and isinstance(number, int)
                    and not isinstance(number, bool)
                ):
                    return RecordedTurn(number)
        return None

    async def get_file_diff(
        self, session_id: str, path: str, *, turn: RecordedTurn | None = None
    ) -> FileDiff:
        view = await self.get_file_changes(session_id, turn=turn)
        if not view.recorded or view.turn is None:
            return FileDiff(None, "unrecorded")
        context = await self.get_file_context(session_id)
        paths = ntpath if PureWindowsPath(context.cwd).is_absolute() else posixpath

        def key(value: str) -> str:
            return paths.normcase(
                paths.normpath(
                    value if paths.isabs(value) else paths.join(context.cwd, value)
                )
            )

        change = next(
            (change for change in view.changes if key(change.path) == key(path)), None
        )
        if change is None:
            expanded = _local_input(context, path)
            change = next(
                (
                    change
                    for change in view.changes
                    if key(change.path) == key(expanded)
                ),
                None,
            )
        if change is None:
            return FileDiff(None, "not_recorded")
        path = change.path
        if change.binary or change.oversize:
            return FileDiff(None, "binary" if change.binary else "oversize")

        async def content(phase: str):
            return (
                await self._request_operation(
                    "file_change_content",
                    path_parameters={"session_id": session_id},
                    params={
                        "turn_number": view.turn.number,
                        "path": path,
                        "phase": phase,
                    },
                )
            )["content"]

        before, after = await asyncio.gather(content("start"), content("end"))
        if before is None or after is None:
            return FileDiff(None, "missing")
        if before.get("binary") or after.get("binary"):
            return FileDiff(None, "binary")
        return await asyncio.to_thread(
            _text_diff, before.get("content", ""), after.get("content", ""), path
        )


def _text_diff(before: str, after: str, path: str) -> FileDiff:
    # Bound matching work as well as output; repetitive text can be quadratic.
    if max(len(before), len(after)) > 131072:
        return FileDiff(None, "oversize")
    old_lines, new_lines = (
        list(StringIO(before, newline="")),
        list(StringIO(after, newline="")),
    )
    if max(len(old_lines), len(new_lines)) > 2000:
        return FileDiff(None, "oversize")
    chunks = []
    size = 0
    for line in difflib.unified_diff(
        old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}"
    ):
        if not line.endswith(("\n", "\r")):
            line += "\n\\ No newline at end of file\n"
        chunks.append(line)
        size += len(line)
        if size > 8192:
            return FileDiff("".join(chunks)[:8192], truncated=True)
    return FileDiff("".join(chunks))
