"""Bounded reconstruction of the task output byte stream."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import KimiServerProtocolError


@dataclass(slots=True)
class TaskOutputTail:
    limit: int
    end: int | None = None
    _data: bytes = b""

    @property
    def text(self) -> str:
        # A byte-limited tail can begin inside a UTF-8 code point.
        return self._data.decode("utf-8", errors="ignore")

    def append(self, frame: dict[str, Any]) -> None:
        offset = frame.get("offset")
        text = frame.get("text")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(text, str)
        ):
            raise KimiServerProtocolError("task.output has invalid offset or text")
        data = text.encode("utf-8")
        frame_end = offset + len(data)
        if self.end is not None:
            if offset > self.end:
                raise KimiServerProtocolError("task.output byte stream has a gap")
            retained_start = self.end - len(self._data)
            overlap_start = max(offset, retained_start)
            overlap_end = min(frame_end, self.end)
            if (
                overlap_start < overlap_end
                and self._data[
                    overlap_start - retained_start : overlap_end - retained_start
                ]
                != data[overlap_start - offset : overlap_end - offset]
            ):
                raise KimiServerProtocolError(
                    "task.output changed previously received bytes"
                )
            data = data[max(0, self.end - offset) :]
        self.end = max(self.end or 0, frame_end)
        self._data = (self._data + data)[-self.limit :] if self.limit else b""
