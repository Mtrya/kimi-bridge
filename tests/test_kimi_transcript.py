from __future__ import annotations

import pytest

from kimi_bridge.kimi_server import KimiServerProtocolError
from kimi_bridge.kimi_server.events import TranscriptProjection
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
        }
    )

    assert snapshot["as_of_seq"] == 999
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
    transcript = {"items": [turn], "attachments": [], "has_more": False, "seq": 50}
    snapshot = snapshot_from_transcript(transcript)
    assert _in_flight_assistant_text(snapshot, turn_id=12) == "Current answer"
    assert _in_flight_thinking_text(snapshot, turn_id=12) == "Current thought"
    assert _in_flight_assistant_text(snapshot, turn_id=11) is None
    assert snapshot["in_flight_turn"]["current_prompt_id"] == "prompt-12"

    turn["steps"].append({"frames": []})
    snapshot = snapshot_from_transcript(transcript)
    assert _in_flight_assistant_text(snapshot, turn_id=12) == ""
    turn["state"] = "completed"
    assert snapshot_from_transcript(transcript)["in_flight_turn"] is None


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
            "seq": 10,
        }
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
            "seq": 10,
        }
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


def _ops_frame(ops: list[dict], seq: int) -> dict:
    """One transcript.ops payload, as extracted from the frame by the client."""

    return {"ops": ops, "seq": seq}


def test_projection_folds_turn_step_delta_and_prompt_lifecycle() -> None:
    from kimi_bridge.kimi_server.events import TranscriptProjection

    projection = TranscriptProjection("session-1")
    streaming = projection.apply_ops(
        _ops_frame(
            [
                {
                    "op": "prompt.upsert",
                    "prompt": {
                        "promptId": "prompt-1",
                        "status": "running",
                    },
                },
                {
                    "op": "turn.upsert",
                    "turn": {
                        "kind": "turn",
                        "turnId": "t0",
                        "ordinal": 0,
                        "state": "running",
                        "triggerPromptId": "prompt-1",
                    },
                },
                {
                    "op": "step.upsert",
                    "turnId": "t0",
                    "step": {
                        "kind": "step",
                        "stepId": "t0.1",
                        "turnId": "t0",
                        "ordinal": 1,
                        "state": "running",
                    },
                },
                {
                    "op": "frame.upsert",
                    "turnId": "t0",
                    "stepId": "t0.1",
                    "frame": {
                        "kind": "thinking",
                        "frameId": "t0.1.f1",
                        "text": "",
                    },
                },
                {
                    "op": "append",
                    "target": {
                        "type": "frame",
                        "turnId": "t0",
                        "stepId": "t0.1",
                        "frameId": "t0.1.f1",
                    },
                    "offset": 0,
                    "text": "Thinking ",
                },
                {
                    "op": "append",
                    "target": {
                        "type": "frame",
                        "turnId": "t0",
                        "stepId": "t0.1",
                        "frameId": "t0.1.f1",
                    },
                    "offset": 9,
                    "text": "hard",
                },
                {
                    "op": "frame.upsert",
                    "turnId": "t0",
                    "stepId": "t0.1",
                    "frame": {
                        "kind": "text",
                        "frameId": "t0.1.f2",
                        "role": "assistant",
                        "text": "",
                    },
                },
                {
                    "op": "append",
                    "target": {
                        "type": "frame",
                        "turnId": "t0",
                        "stepId": "t0.1",
                        "frameId": "t0.1.f2",
                    },
                    "offset": 0,
                    "text": "ok",
                },
            ],
            2,
        )
    )
    assert [event["payload"] for event in streaming] == [
        {"type": "turn.started", "turnId": 0},
        {"type": "turn.step.started", "step": 1},
        {"type": "thinking.delta", "delta": "Thinking "},
        {"type": "thinking.delta", "delta": "hard"},
        {"type": "assistant.delta", "delta": "ok"},
    ]
    events = []
    for event in projection.apply_ops(
        _ops_frame(
            [
                {
                    "op": "frame.upsert",
                    "turnId": "t0",
                    "stepId": "t0.1",
                    "frame": {
                        "kind": "thinking",
                        "frameId": "t0.1.f1",
                        "text": "Thinking hard",
                    },
                },
                {
                    "op": "step.upsert",
                    "turnId": "t0",
                    "step": {
                        "kind": "step",
                        "stepId": "t0.1",
                        "turnId": "t0",
                        "ordinal": 1,
                        "state": "completed",
                        "usage": {
                            "inputOther": 10,
                            "output": 2,
                            "inputCacheRead": 4,
                            "inputCacheCreation": 1,
                        },
                    },
                },
                {
                    "op": "turn.upsert",
                    "turn": {
                        "kind": "turn",
                        "turnId": "t0",
                        "ordinal": 0,
                        "state": "completed",
                        "triggerPromptId": "prompt-1",
                    },
                },
                {
                    "op": "prompt.upsert",
                    "prompt": {
                        "promptId": "prompt-1",
                        "status": "completed",
                    },
                },
            ],
            3,
        )
    ):
        events.append(event)
    # Frame consolidation and terminal upserts deduplicate streaming events.
    assert events == [
        {
            "type": "session_event",
            "session_id": "session-1",
            "seq": 3,
            "payload": {"type": "turn.ended", "turnId": 0, "reason": "completed"},
        },
        {
            "type": "session_event",
            "session_id": "session-1",
            "seq": 3,
            "payload": {"type": "prompt.completed", "promptId": "prompt-1"},
        },
    ]
    # Replayed batches at or below the applied sequence are ignored.
    assert (
        projection.apply_ops(_ops_frame([{"op": "items.remove", "ids": []}], 3)) == []
    )


def test_projection_emits_delta_events_for_append_ops() -> None:
    from kimi_bridge.kimi_server.events import TranscriptProjection

    projection = TranscriptProjection("session-1")
    projection.apply_ops(
        _ops_frame(
            [
                {
                    "op": "turn.upsert",
                    "turn": {
                        "kind": "turn",
                        "turnId": "t0",
                        "ordinal": 0,
                        "state": "running",
                    },
                },
                {
                    "op": "step.upsert",
                    "turnId": "t0",
                    "step": {
                        "kind": "step",
                        "stepId": "t0.1",
                        "turnId": "t0",
                        "ordinal": 1,
                        "state": "running",
                    },
                },
                {
                    "op": "frame.upsert",
                    "turnId": "t0",
                    "stepId": "t0.1",
                    "frame": {
                        "kind": "text",
                        "frameId": "f3",
                        "role": "user",
                        "text": "",
                    },
                },
                {
                    "op": "frame.upsert",
                    "turnId": "t0",
                    "stepId": "t0.1",
                    "frame": {"kind": "thinking", "frameId": "f1", "text": ""},
                },
                {
                    "op": "frame.upsert",
                    "turnId": "t0",
                    "stepId": "t0.1",
                    "frame": {
                        "kind": "text",
                        "frameId": "f2",
                        "role": "assistant",
                        "text": "",
                    },
                },
            ],
            2,
        )
    )
    deltas = projection.apply_ops(
        _ops_frame(
            [
                {
                    "op": "append",
                    "target": {
                        "type": "frame",
                        "turnId": "t0",
                        "stepId": "t0.1",
                        "frameId": "f1",
                    },
                    "offset": 0,
                    "text": "think",
                },
                {
                    "op": "append",
                    "target": {
                        "type": "frame",
                        "turnId": "t0",
                        "stepId": "t0.1",
                        "frameId": "f2",
                    },
                    "offset": 0,
                    "text": "answer",
                },
                {
                    "op": "append",
                    "target": {
                        "type": "frame",
                        "turnId": "t0",
                        "stepId": "t0.1",
                        "frameId": "f3",
                    },
                    "offset": 0,
                    "text": "tool result",
                },
            ],
            3,
        )
    )
    assert deltas == [
        {
            "type": "session_event",
            "session_id": "session-1",
            "seq": 3,
            "offset": 0,
            "payload": {"type": "thinking.delta", "delta": "think"},
        },
        {
            "type": "session_event",
            "session_id": "session-1",
            "seq": 3,
            "offset": 0,
            "payload": {"type": "assistant.delta", "delta": "answer"},
        },
    ]


def test_projection_maps_notices_and_compaction_markers() -> None:
    from kimi_bridge.kimi_server.events import (
        TranscriptProjection,
        session_notice_from_event,
    )

    projection = TranscriptProjection("session-1")
    events = projection.apply_ops(
        _ops_frame(
            [
                {
                    "op": "marker.upsert",
                    "item": {
                        "kind": "marker",
                        "markerId": "m1",
                        "marker": "compaction",
                        "payload": {"phase": "started", "trigger": "manual"},
                    },
                },
                {
                    "op": "marker.upsert",
                    "item": {
                        "kind": "marker",
                        "markerId": "m2",
                        "marker": "notice",
                        "payload": {
                            "level": "error",
                            "message": "Model not set",
                            "event": {
                                "code": "model.not_configured",
                                "message": "Model not set",
                                "retryable": False,
                            },
                        },
                    },
                },
                {
                    "op": "marker.upsert",
                    "item": {
                        "kind": "marker",
                        "markerId": "m3",
                        "marker": "notice",
                        "payload": {
                            "level": "warning",
                            "message": "Low context",
                        },
                    },
                },
                {
                    "op": "marker.upsert",
                    "item": {
                        "kind": "marker",
                        "markerId": "m4",
                        "marker": "compaction",
                        "payload": {
                            "phase": "completed",
                            "result": {
                                "compactedCount": 3,
                                "tokensBefore": 100,
                                "tokensAfter": 80,
                            },
                        },
                    },
                },
            ],
            2,
        )
    )
    payloads = [event["payload"] for event in events]
    assert payloads == [
        {"type": "compaction.started", "trigger": "manual"},
        {
            "type": "error",
            "message": "Model not set",
            "code": "model.not_configured",
            "retryable": False,
        },
        {"type": "warning", "message": "Low context"},
        {
            "type": "compaction.completed",
            "result": {
                "compactedCount": 3,
                "tokensBefore": 100,
                "tokensAfter": 80,
            },
        },
    ]
    assert session_notice_from_event(events[1]) is not None
    assert session_notice_from_event(events[1]).kind == "error"
    assert session_notice_from_event(events[2]).kind == "warning"


def test_projection_reset_rebuilds_state_and_reports_resync() -> None:
    from kimi_bridge.kimi_server.events import TranscriptProjection

    projection = TranscriptProjection("session-1")
    projection.apply_ops(
        _ops_frame(
            [
                {
                    "op": "turn.upsert",
                    "turn": {
                        "kind": "turn",
                        "turnId": "t0",
                        "ordinal": 0,
                        "state": "running",
                    },
                }
            ],
            4,
        )
    )
    resync_events = projection.apply_reset(
        {
            "type": "transcript.reset",
            "agent_id": "main",
            "seq": 7,
            "snapshot": {
                "items": [
                    {
                        "kind": "turn",
                        "turnId": "t0",
                        "ordinal": 0,
                        "state": "completed",
                        "triggerPromptId": "prompt-1",
                        "steps": [],
                    }
                ],
                "interactions": [],
                "attachments": [],
                "prompts": [],
                "hasMoreOlder": False,
            },
            "has_more_older": False,
        }
    )
    assert len(resync_events) == 1
    resync = resync_events[0]
    assert resync["type"] == "resync_required"
    assert resync["payload"]["reason"] == "transcript_reset"
    assert resync["snapshot"]["as_of_seq"] == 7
    assert projection.seq == 7
    # An idle initial reset does not require a resync.
    fresh = TranscriptProjection("session-1")
    assert fresh.apply_reset({"agent_id": "main", "seq": 1, "snapshot": {"items": []}}) == []


def test_initial_reset_restores_the_running_answer_and_thinking_prefixes() -> None:
    from kimi_bridge.kimi_server.events import TranscriptProjection

    projection = TranscriptProjection("session-1")
    events = projection.apply_reset({
        "seq": 5,
        "snapshot": {
            "items": [{
                "kind": "turn",
                "turnId": "turn-1",
                "ordinal": 1,
                "state": "running",
                "steps": [{
                    "stepId": "step-1",
                    "ordinal": 1,
                    "state": "running",
                    "frames": [
                        {"frameId": "thought-1", "kind": "thinking", "text": "existing thought"},
                        {"frameId": "answer-1", "kind": "text", "role": "assistant", "text": "existing "},
                    ],
                }],
            }],
        },
    })
    assert len(events) == 1
    assert events[0]["type"] == "resync_required"
    snapshot = events[0]["snapshot"]
    assert _in_flight_assistant_text(snapshot) == "existing "
    assert _in_flight_thinking_text(snapshot) == "existing thought"

    suffix = projection.apply_ops({
        "seq": 6,
        "ops": [{
            "op": "append",
            "target": {"type": "frame", "turnId": "turn-1", "stepId": "step-1", "frameId": "answer-1"},
            "offset": 9,
            "text": "suffix",
        }],
    })
    assert [event["payload"] for event in suffix] == [{"type": "assistant.delta", "delta": "suffix"}]


def _running_projection() -> TranscriptProjection:
    projection = TranscriptProjection("session-1")
    projection.apply_reset(
        {
            "seq": 1,
            "snapshot": {
                "items": [
                    {
                        "kind": "turn",
                        "turnId": "t1",
                        "ordinal": 1,
                        "state": "running",
                        "origin": {"kind": "user"},
                        "triggerPromptId": "prompt-1",
                        "prompt": "Hello",
                        "steps": [
                            {
                                "kind": "step",
                                "stepId": "s1",
                                "turnId": "t1",
                                "ordinal": 1,
                                "state": "running",
                                "frames": [
                                    {
                                        "frameId": "f1",
                                        "kind": "text",
                                        "role": "assistant",
                                        "text": "prefix ",
                                    },
                                    {
                                        "frameId": "thought",
                                        "kind": "thinking",
                                        "text": "thinking",
                                    },
                                ],
                            }
                        ],
                    }
                ],
                "attachments": [],
                "interactions": [],
                "prompts": [],
            },
        }
    )
    return projection


@pytest.mark.parametrize("state", ["cancelled", "failed", "completed"])
@pytest.mark.parametrize("operation", ["append", "frame.upsert"])
def test_ended_queued_turn_does_not_interrupt_running_output(
    state: str, operation: str
) -> None:
    projection = _running_projection()
    queued = {
        "kind": "turn",
        "turnId": "t2",
        "ordinal": 2,
        "state": "queued",
        "origin": {"kind": "user"},
    }
    assert (
        projection.apply_ops({"seq": 2, "ops": [{"op": "turn.upsert", "turn": queued}]})
        == []
    )
    assert (
        projection.apply_ops(
            {
                "seq": 3,
                "ops": [{"op": "turn.upsert", "turn": {**queued, "state": state}}],
            }
        )
        == []
    )

    # Reattachment must choose the same running turn as the existing stream.
    restored = TranscriptProjection("session-1")
    restored.apply_reset(
        {
            "seq": 3,
            "snapshot": {
                "items": [
                    {
                        "kind": "turn",
                        "turnId": "t1",
                        "ordinal": 1,
                        "state": "running",
                        "steps": [
                            {
                                "stepId": "s1",
                                "ordinal": 1,
                                "state": "running",
                                "frames": [
                                    {
                                        "frameId": "f1",
                                        "kind": "text",
                                        "role": "assistant",
                                        "text": "prefix ",
                                    },
                                    {
                                        "frameId": "thought",
                                        "kind": "thinking",
                                        "text": "thinking",
                                    },
                                ],
                            }
                        ],
                    },
                    {**queued, "state": state},
                ],
            },
        }
    )
    ops = []
    for frame_id, kind, prefix in (
        ("f1", "text", "prefix "),
        ("thought", "thinking", "thinking"),
    ):
        if operation == "append":
            ops.append(
                {
                    "op": operation,
                    "target": {
                        "type": "frame",
                        "turnId": "t1",
                        "stepId": "s1",
                        "frameId": frame_id,
                    },
                    "offset": len(prefix),
                    "text": "tail",
                }
            )
        else:
            frame = {"frameId": frame_id, "kind": kind, "text": prefix + "tail"}
            if kind == "text":
                frame["role"] = "assistant"
            ops.append(
                {"op": operation, "turnId": "t1", "stepId": "s1", "frame": frame}
            )

    for stream in (projection, restored):
        events = stream.apply_ops({"seq": 4, "ops": ops})
        assert [(event["payload"], event["offset"]) for event in events] == [
            ({"type": "assistant.delta", "delta": "tail"}, 7),
            ({"type": "thinking.delta", "delta": "tail"}, 8),
        ]
        assert _in_flight_assistant_text(stream.snapshot(), turn_id=1) == "prefix tail"
        assert _in_flight_thinking_text(stream.snapshot(), turn_id=1) == "thinkingtail"


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
def test_latest_terminal_turn_still_emits_output_without_running_work(
    state: str,
) -> None:
    projection = _running_projection()
    events = projection.apply_ops(
        {
            "seq": 2,
            "ops": [
                {
                    "op": "turn.upsert",
                    "turn": {
                        "kind": "turn",
                        "turnId": "t1",
                        "ordinal": 1,
                        "state": state,
                    },
                }
            ],
        }
    )
    assert [event["payload"]["type"] for event in events] == ["turn.ended"]
    events = projection.apply_ops(
        {
            "seq": 3,
            "ops": [
                {
                    "op": "append",
                    "target": {
                        "type": "frame",
                        "turnId": "t1",
                        "stepId": "s1",
                        "frameId": "f1",
                    },
                    "offset": 7,
                    "text": "tail",
                }
            ],
        }
    )
    assert [event["payload"] for event in events] == [
        {"type": "assistant.delta", "delta": "tail"}
    ]
    assert _persisted_assistant_text(projection.snapshot()) == "prefix tail"


@pytest.mark.parametrize("operation", ["append", "frame.upsert"])
def test_text_corrections_emit_the_reduced_snapshot(operation: str) -> None:
    projection = _running_projection()
    if operation == "append":
        op = {
            "op": operation,
            "target": {
                "type": "frame",
                "turnId": "t1",
                "stepId": "s1",
                "frameId": "f1",
            },
            "offset": 0,
            "text": "corrected",
        }
    else:
        op = {
            "op": operation,
            "turnId": "t1",
            "stepId": "s1",
            "frame": {
                "kind": "text",
                "frameId": "f1",
                "role": "assistant",
                "text": "corrected",
            },
        }
    batch = {"seq": 2, "ops": [op]}
    events = projection.apply_ops(batch)
    assert len(events) == 1
    assert events[0]["type"] == "resync_required"
    assert events[0]["snapshot"] == projection.snapshot()
    assert _in_flight_assistant_text(events[0]["snapshot"]) == "corrected"
    assert _in_flight_thinking_text(events[0]["snapshot"]) == "thinking"
    assert events[0]["snapshot"]["in_flight_turn"]["step"] == 1
    assert projection.apply_ops(batch) == []


def test_multiple_frames_use_aggregate_offsets_and_survive_parent_updates() -> None:
    projection = _running_projection()
    events = projection.apply_ops(
        {
            "seq": 2,
            "ops": [
                {
                    "op": "frame.upsert",
                    "turnId": "t1",
                    "stepId": "s1",
                    "frame": {
                        "kind": "text",
                        "frameId": "f2",
                        "role": "assistant",
                        "text": "tail",
                    },
                },
                {
                    "op": "append",
                    "target": {
                        "type": "frame",
                        "turnId": "t1",
                        "stepId": "s1",
                        "frameId": "f2",
                    },
                    "offset": 4,
                    "text": "!",
                },
                {
                    "op": "step.upsert",
                    "turnId": "t1",
                    "step": {
                        "kind": "step",
                        "stepId": "s1",
                        "turnId": "t1",
                        "ordinal": 1,
                        "state": "completed",
                    },
                },
                {
                    "op": "turn.upsert",
                    "turn": {
                        "kind": "turn",
                        "turnId": "t1",
                        "ordinal": 1,
                        "state": "running",
                        "origin": {"kind": "user"},
                    },
                },
            ],
        }
    )
    assert [(e["offset"], e["payload"]["delta"]) for e in events] == [
        (7, "tail"),
        (11, "!"),
    ]
    assert _in_flight_assistant_text(projection.snapshot()) == "prefix tail!"


def test_native_utf16_offsets_become_semantic_python_offsets() -> None:
    projection = _running_projection()
    projection.apply_ops(
        {
            "seq": 2,
            "ops": [
                {
                    "op": "frame.upsert",
                    "turnId": "t1",
                    "stepId": "s1",
                    "frame": {
                        "kind": "text",
                        "frameId": "f1",
                        "role": "assistant",
                        "text": "A🙂",
                    },
                }
            ],
        }
    )
    events = projection.apply_ops(
        {
            "seq": 3,
            "ops": [
                {
                    "op": "append",
                    "target": {
                        "type": "frame",
                        "turnId": "t1",
                        "stepId": "s1",
                        "frameId": "f1",
                    },
                    "offset": 3,
                    "text": "tail",
                }
            ],
        }
    )
    assert events[0]["offset"] == 2
    assert events[0]["payload"]["delta"] == "tail"
    assert _in_flight_assistant_text(projection.snapshot()) == "A🙂tail"
    with pytest.raises(KimiServerProtocolError, match="UTF-16"):
        projection.apply_ops(
            {
                "seq": 4,
                "ops": [
                    {
                        "op": "append",
                        "target": {
                            "type": "frame",
                            "turnId": "t1",
                            "stepId": "s1",
                            "frameId": "f1",
                        },
                        "offset": 2,
                        "text": "broken",
                    }
                ],
            }
        )


def test_removing_a_turn_prunes_its_steps_and_frames() -> None:
    projection = _running_projection()
    events = projection.apply_ops(
        {"seq": 2, "ops": [{"op": "items.remove", "ids": ["t1"]}]}
    )
    assert events[0]["snapshot"]["messages"]["items"] == []
    assert events[0]["snapshot"]["in_flight_turn"] is None
    assert events[0]["snapshot"]["turn_ids"] == []
    with pytest.raises(KimiServerProtocolError, match="unknown turn"):
        projection.apply_ops(
            {
                "seq": 3,
                "ops": [
                    {
                        "op": "append",
                        "target": {
                            "type": "frame",
                            "turnId": "t1",
                            "stepId": "s1",
                            "frameId": "f1",
                        },
                        "offset": 0,
                        "text": "orphan",
                    }
                ],
            }
        )


def test_native_undo_reset_operation_replaces_nonempty_history() -> None:
    projection = _running_projection()
    # Native Kimi 2.5 emits this operation inside transcript.ops after undo.
    events = projection.apply_ops(
        {
            "seq": 9,
            "ops": [
                {
                    "op": "reset",
                    "agentId": "main",
                    "snapshot": {
                        "items": [],
                        "interactions": [],
                        "attachments": [],
                        "prompts": [],
                    },
                }
            ],
        }
    )
    assert events[0]["snapshot"]["as_of_seq"] == 9
    assert events[0]["snapshot"]["messages"]["items"] == []
    assert projection.snapshot()["turn_ids"] == []


@pytest.mark.parametrize("state", ["completed", "interrupted", "failed"])
def test_terminal_only_steps_still_advance_the_render_boundary(state: str) -> None:
    projection = _running_projection()
    events = projection.apply_ops(
        {
            "seq": 2,
            "ops": [
                {
                    "op": "step.upsert",
                    "turnId": "t1",
                    "step": {
                        "kind": "step",
                        "stepId": "s2",
                        "turnId": "t1",
                        "ordinal": 2,
                        "state": state,
                    },
                }
            ],
        }
    )
    assert [event["payload"] for event in events] == [
        {"type": "turn.step.started", "step": 2}
    ]
    assert _in_flight_assistant_text(projection.snapshot()) == ""
    assert _persisted_assistant_text(projection.snapshot()) == ""


def test_attachment_upserts_are_available_in_recovery_snapshots() -> None:
    projection = _running_projection()
    projection.apply_ops(
        {
            "seq": 2,
            "ops": [
                {
                    "op": "attachment.upsert",
                    "attachment": {
                        "attachmentId": "image-1",
                        "mediaType": "image/png",
                        "name": "plot.png",
                    },
                },
                {
                    "op": "frame.upsert",
                    "turnId": "t1",
                    "stepId": "s1",
                    "frame": {
                        "kind": "text",
                        "frameId": "f1",
                        "role": "assistant",
                        "text": "prefix ",
                        "attachmentIds": ["image-1"],
                    },
                },
            ],
        }
    )
    assert {"type": "image/png", "name": "plot.png"} in projection.snapshot()[
        "messages"
    ]["items"][-1]["content"]


def test_missing_optional_transcript_sequence_preserves_an_unknown_watermark() -> None:
    snapshot = snapshot_from_transcript(
        {"items": [], "attachments": [], "has_more": False}
    )
    assert snapshot["as_of_seq"] is None


def test_projection_maps_failed_turns_to_terminal_notices() -> None:
    from kimi_bridge.kimi_server.events import (
        TranscriptProjection,
        session_notice_from_event,
    )

    projection = TranscriptProjection("session-1")
    (event,) = projection.apply_ops(
        _ops_frame(
            [
                {
                    "op": "turn.upsert",
                    "turn": {
                        "kind": "turn",
                        "turnId": "t0",
                        "ordinal": 0,
                        "state": "failed",
                        "error": "Model not set",
                    },
                }
            ],
            2,
        )
    )
    notice = session_notice_from_event(event)
    assert notice is not None
    assert notice.kind == "error"
    assert notice.message == "The turn failed before completing."
