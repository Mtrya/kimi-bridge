"""Project native transcript turns into the bridge's message snapshot."""

from __future__ import annotations

from typing import Any


def snapshot_from_transcript(transcript: dict[str, Any]) -> dict[str, Any]:
    """Keep the transcript's own sequence watermark on the bridge snapshot."""

    attachments = {item["attachmentId"]: item for item in transcript["attachments"]}
    messages: list[dict[str, Any]] = []
    in_flight: dict[str, Any] | None = None
    for turn in transcript["items"]:
        if turn["kind"] != "turn" or turn["state"] == "queued":
            continue
        prompt_id = turn.get("triggerPromptId")
        opener = _text_parts(turn.get("prompt", ""), turn, attachments)
        if opener:
            messages.append(_message("user", opener, prompt_id))
        for step in turn.get("steps") or ():
            assistant: list[dict[str, Any]] = []
            for frame in step["frames"]:
                if frame["kind"] == "thinking":
                    assistant.append({"type": "thinking", "thinking": frame["text"]})
                elif frame["kind"] == "tool":
                    # Preserve an empty-text assistant step so reconciliation
                    # cannot reuse an earlier answer for a tool-only ending.
                    assistant.append({"type": "tool_use"})
                elif frame["kind"] == "text":
                    parts = _text_parts(frame["text"], frame, attachments)
                    if frame["role"] == "user":
                        if assistant:
                            messages.append(_message("assistant", assistant, prompt_id))
                            assistant = []
                        messages.append(_message("user", parts, prompt_id))
                    else:
                        assistant.extend(parts)
            messages.append(_message("assistant", assistant, prompt_id))
        if turn["state"] == "running":
            current_step = (turn.get("steps") or [{}])[-1]
            answer, thinking = stream_text_from_step(current_step)
            in_flight = {
                "turn_id": turn["ordinal"],
                "step": current_step.get("ordinal"),
                "assistant_text": answer,
                "thinking_text": thinking,
            }
            if prompt_id is not None:
                in_flight["current_prompt_id"] = prompt_id
    return {
        "as_of_seq": transcript.get("seq"),
        "turn_ids": [
            turn["ordinal"] for turn in transcript["items"] if turn["kind"] == "turn"
        ],
        "messages": {
            "items": messages[-100:],
            "has_more": transcript["has_more"] or len(messages) > 100,
        },
        "in_flight_turn": in_flight,
    }


def stream_text_from_step(step: dict[str, Any]) -> tuple[str, str]:
    """Project the current assistant segment consistently for snapshots and deltas."""

    answer: list[str] = []
    thinking: list[str] = []
    for frame in step.get("frames") or ():
        if frame["kind"] == "text":
            if frame["role"] == "user":
                answer.clear()
                thinking.clear()
            else:
                answer.append(frame["text"])
        elif frame["kind"] == "thinking":
            thinking.append(frame["text"])
    return "".join(answer), "".join(thinking)


def _message(
    role: str, parts: list[dict[str, Any]], prompt_id: str | None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": role, "content": parts}
    if prompt_id is not None:
        message["prompt_id"] = prompt_id
    return message


def _text_parts(
    text: str, source: dict[str, Any], attachments: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    for attachment_id in source.get("attachmentIds", []):
        attachment = attachments.get(attachment_id)
        if attachment is None:
            parts.append({"type": "attachment", "name": attachment_id})
            continue
        part = {"type": attachment["mediaType"]}
        if "name" in attachment:
            part["name"] = attachment["name"]
        parts.append(part)
    return parts
