"""Recorded terminal-turn comparisons from native checkpoints."""

from __future__ import annotations

import asyncio
import difflib
import ntpath
import posixpath
from pathlib import PureWindowsPath

from .types import FileChange, FileChanges, FileDiff, RecordedTurn


class FileHistoryOperations:
    async def get_file_changes(
        self, session_id: str, *, turn: RecordedTurn | None = None
    ) -> FileChanges:
        if turn is None:
            transcript = await self._request_operation(
                "transcript",
                path_parameters={"session_id": session_id},
                params={"agent_id": "main", "page_size": 100},
            )
            terminal = [
                item
                for item in transcript["items"]
                if item["kind"] == "turn"
                and item["state"] in {"completed", "failed", "cancelled"}
                and item.get("steps")
            ]
            if not terminal:
                return FileChanges(None, False, ())
            turn = RecordedTurn(max(item["ordinal"] for item in terminal))
        data = await self._request_operation(
            "file_changes",
            path_parameters={"session_id": session_id},
            params={"turn_id": turn.number},
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
            return FileDiff(None, "missing")
        path = change.path
        if change.binary or change.oversize:
            return FileDiff(None, "binary" if change.binary else "oversize")

        async def content(phase: str):
            return (
                await self._request_operation(
                    "file_change_content",
                    path_parameters={"session_id": session_id},
                    params={"turn_id": view.turn.number, "path": path, "phase": phase},
                )
            )["content"]

        before, after = await asyncio.gather(content("start"), content("end"))
        if before is None or after is None:
            return FileDiff(None, "missing")
        if before.get("binary") or after.get("binary"):
            return FileDiff(None, "binary")
        chunks = []
        size = 0
        for line in difflib.unified_diff(
            before.get("content", "").splitlines(),
            after.get("content", "").splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        ):
            chunks.append(line + "\n")
            size += len(line) + 1
            if size > 8192:
                return FileDiff("".join(chunks)[:8192], truncated=True)
        return FileDiff("".join(chunks))
