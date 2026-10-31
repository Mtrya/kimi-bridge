from __future__ import annotations

from kimi_bridge.kimi_server.events import _EventCursor
from kimi_bridge.kimi_server.transcript import snapshot_from_transcript
from kimi_bridge.router.formatting import (
    _format_history,
    _in_flight_assistant_text,
    _in_flight_thinking_text,
    _persisted_assistant_text,
    _persisted_thinking_text,
)


def test_transcript_preserves_message_boundaries_media_and_prompt_ownership() -> None:
    snapshot = snapshot_from_transcript(
        {
            "seq": 999,
            "has_more": True,
            "attachments": [
                {
                    "attachmentId": "image-1",
                    "mediaType": "image/png",
                    "name": "plot.png",
                }
            ],
            "items": [
                {"kind": "marker", "marker": "compaction"},
                {
                    "kind": "turn",
                    "ordinal": 7,
                    "state": "completed",
                    "triggerPromptId": "prompt-7",
                    "prompt": "Check this",
                    "attachmentIds": ["image-1"],
                    "steps": [
                        {
                            "frames": [
                                {"kind": "thinking", "text": "First thought"},
                                {
                                    "kind": "text",
                                    "role": "assistant",
                                    "text": "Checking",
                                },
                                {
                                    "kind": "tool",
                                    "name": "ReadFile",
                                    "output": "hidden",
                                },
                            ]
                        },
                        {
                            "frames": [
                                {
                                    "kind": "text",
                                    "role": "user",
                                    "text": "Also check units",
                                },
                                {"kind": "thinking", "text": "Final thought"},
                                {"kind": "text", "role": "assistant", "text": "All "},
                                {
                                    "kind": "text",
                                    "role": "assistant",
                                    "text": "correct",
                                },
                            ]
                        },
                    ],
                },
                {"kind": "taskref", "taskId": "task-1"},
                {
                    "kind": "turn",
                    "ordinal": 8,
                    "state": "completed",
                    "origin": {"kind": "cron"},
                    "prompt": "Scheduled check",
                    "steps": [
                        {
                            "frames": [
                                {
                                    "kind": "text",
                                    "role": "assistant",
                                    "text": "Scheduled result",
                                },
                            ]
                        }
                    ],
                },
                {
                    "kind": "turn",
                    "ordinal": 9,
                    "state": "queued",
                    "prompt": "Pending",
                    "steps": [],
                },
            ],
        },
        _EventCursor(42, "events-epoch"),
    )

    assert (snapshot["as_of_seq"], snapshot["epoch"]) == (42, "events-epoch")
    assert snapshot["in_flight_turn"] is None
    assert snapshot["messages"]["has_more"] is True
    assert _persisted_assistant_text(snapshot, prompt_id="prompt-7") == "All correct"
    assert _persisted_thinking_text(snapshot, prompt_id="prompt-7") == "Final thought"
    assert _persisted_assistant_text(snapshot) == "Scheduled result"
    assert "prompt_id" not in snapshot["messages"]["items"][-1]
    history = _format_history(snapshot, 50)
    assert history is not None
    for visible in (
        "Check this",
        "[attachment: plot.png]",
        "Checking",
        "Also check units",
        "All \ncorrect",
        "Scheduled check",
        "Scheduled result",
    ):
        assert visible in history
    for hidden in ("First thought", "Final thought", "ReadFile", "hidden", "Pending"):
        assert hidden not in history


def test_running_transcript_uses_current_step_and_clears_on_completion() -> None:
    turn = {
        "kind": "turn",
        "ordinal": 12,
        "state": "running",
        "triggerPromptId": "prompt-12",
        "steps": [
            {"frames": [{"kind": "text", "role": "assistant", "text": "Earlier step"}]},
            {
                "frames": [
                    {"kind": "thinking", "text": "Current thought"},
                    {"kind": "text", "role": "assistant", "text": "Current answer"},
                ]
            },
        ],
    }
    transcript = {"items": [turn], "attachments": [], "has_more": False}
    cursor = _EventCursor(50, "epoch")
    snapshot = snapshot_from_transcript(transcript, cursor)
    assert _in_flight_assistant_text(snapshot, turn_id=12) == "Current answer"
    assert _in_flight_thinking_text(snapshot, turn_id=12) == "Current thought"
    assert _in_flight_assistant_text(snapshot, turn_id=11) is None
    assert snapshot["in_flight_turn"]["current_prompt_id"] == "prompt-12"

    turn["steps"].append({"frames": []})
    snapshot = snapshot_from_transcript(transcript, cursor)
    assert _in_flight_assistant_text(snapshot, turn_id=12) == ""
    turn["state"] = "completed"
    assert snapshot_from_transcript(transcript, cursor)["in_flight_turn"] is None


def test_tool_only_final_step_does_not_reuse_an_earlier_answer() -> None:
    snapshot = snapshot_from_transcript(
        {
            "items": [
                {
                    "kind": "turn",
                    "ordinal": 1,
                    "state": "failed",
                    "steps": [
                        {
                            "frames": [
                                {
                                    "kind": "text",
                                    "role": "assistant",
                                    "text": "Earlier answer",
                                }
                            ]
                        },
                        {"frames": [{"kind": "tool", "name": "Bash"}]},
                    ],
                }
            ],
            "attachments": [],
            "has_more": False,
        },
        _EventCursor(10, "epoch"),
    )
    assert _persisted_assistant_text(snapshot) == ""
    assert _persisted_thinking_text(snapshot) is None


def test_unresolved_attachment_keeps_a_placeholder_without_losing_the_message() -> None:
    snapshot = snapshot_from_transcript(
        {
            "items": [
                {
                    "kind": "turn",
                    "ordinal": 1,
                    "state": "completed",
                    "attachmentIds": ["missing", "known"],
                    "steps": [],
                }
            ],
            "attachments": [
                {"attachmentId": "known", "mediaType": "image/png", "name": "plot.png"}
            ],
            "has_more": False,
        },
        _EventCursor(10, "epoch"),
    )
    assert snapshot["messages"]["items"] == [
        {
            "role": "user",
            "content": [
                {"type": "attachment", "name": "missing"},
                {"type": "image/png", "name": "plot.png"},
            ],
        }
    ]
