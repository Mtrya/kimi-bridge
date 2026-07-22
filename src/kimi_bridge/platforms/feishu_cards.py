"""Render and decode Feishu interactive cards."""

from __future__ import annotations

import json
from difflib import unified_diff
from typing import Any

from ..interactions import (
    ApprovalPrompt,
    ApprovalResponse,
    InteractionOutcome,
    InteractionPrompt,
    InteractionResponse,
    MultipleChoiceAnswer,
    MultipleChoiceWithOtherAnswer,
    OtherAnswer,
    Question,
    QuestionPrompt,
    QuestionResponse,
    SingleChoiceAnswer,
    SkippedAnswer,
)


INTERACTION_SUMMARY_LIMIT = 1200
APPROVAL_PREVIEW_LIMIT = 1600
APPROVAL_PATH_LIMIT = 400


def render_interaction(prompt: InteractionPrompt) -> dict[str, Any]:
    if isinstance(prompt, ApprovalPrompt):
        return _approval_card(prompt)
    return _question_card(prompt)


def render_outcome(outcome: InteractionOutcome) -> dict[str, Any]:
    if outcome.state == "completed" and outcome.approval_decision is not None:
        title, template = {
            "approved": ("Approval approved", "green"),
            "rejected": ("Approval rejected", "red"),
            "cancelled": ("Approval cancelled", "grey"),
        }[outcome.approval_decision]
        return _status_card(title, outcome.detail, template=template)
    title, template = {
        "completed": ("Interaction complete", "green"),
        "timed_out": ("Interaction timed out", "red"),
        "stale": ("Interaction expired", "grey"),
        "cancelled": ("Interaction cancelled", "grey"),
    }[outcome.state]
    return _status_card(title, outcome.detail, template=template)


def interaction_id_from_value(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    interaction_id = value.get("interaction_id")
    return interaction_id if isinstance(interaction_id, str) else None


def decode_interaction_response(
    prompt: InteractionPrompt,
    *,
    value: object,
    form_value: object,
    action_name: object,
) -> InteractionResponse | None:
    callback_value = value if isinstance(value, dict) else {}
    callback_form = form_value if isinstance(form_value, dict) else {}
    if isinstance(prompt, ApprovalPrompt):
        decision = callback_value.get("decision")
        if decision not in {"approved", "rejected", "cancelled"}:
            raise ValueError("approval card callback has an invalid decision")
        return ApprovalResponse(decision)
    return _decode_question_response(
        prompt,
        value=callback_value,
        form_value=callback_form,
        action_name=action_name if isinstance(action_name, str) else None,
    )


def _card_shell(
    title: str,
    subtitle: str,
    *,
    template: str,
    icon_token: str,
    tag_text: str,
    tag_color: str,
    elements: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default"},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": template,
            "icon": {"tag": "standard_icon", "token": icon_token},
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": tag_text},
                    "color": tag_color,
                }
            ],
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "12px",
            "elements": elements,
        },
    }


def _context_block(*lines: str) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "background_style": "grey-50",
                "padding": "12px",
                "vertical_spacing": "4px",
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "plain_text",
                            "content": line,
                            "lines": 8,
                        },
                    }
                    for line in lines
                    if line
                ],
            }
        ],
    }


def _approval_card(prompt: ApprovalPrompt) -> dict[str, Any]:
    request = prompt.request
    buttons = [
        ("Approve", "approved", "primary_filled"),
        ("Reject", "rejected", "danger"),
        ("Cancel", "cancelled", "default"),
    ]
    button_block = {
        "tag": "column_set",
        "flex_mode": "trisect",
        "horizontal_spacing": "8px",
        "columns": [
            {
                "tag": "column",
                "elements": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": label},
                        "type": button_type,
                        "width": "fill",
                        "behaviors": [
                            {
                                "type": "callback",
                                "value": {
                                    "interaction_id": prompt.interaction_id,
                                    "decision": decision,
                                },
                            }
                        ],
                    }
                ],
            }
            for label, decision, button_type in buttons
        ],
    }
    return _card_shell(
        "Approval required",
        prompt.session_title,
        template="default",
        icon_token="approve_colorful",
        tag_text="Pending",
        tag_color="yellow",
        elements=[
            _approval_context(prompt),
            _approval_preview(request.input_display),
            button_block,
        ],
    )


def _approval_context(prompt: ApprovalPrompt) -> dict[str, Any]:
    workspace_name = prompt.workspace.rstrip("/").rsplit("/", 1)[-1] or "/"
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "background_style": "grey-50",
                "padding": "12px",
                "vertical_spacing": "4px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            "<text_tag color='neutral'>cwd</text_tag> "
                            f"**{_escape_markdown(workspace_name)}**"
                        ),
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "plain_text",
                            "content": _bounded(
                                prompt.workspace, APPROVAL_PATH_LIMIT
                            ),
                            "lines": 4,
                        },
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "plain_text",
                            "content": (
                                f"{prompt.request.tool_name} · "
                                f"{prompt.request.action}"
                            ),
                            "lines": 3,
                        },
                    },
                ],
            }
        ],
    }


def _approval_preview(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        command = _first_string(value, "command", "cmd")
        if command is not None:
            return _preview_block("Command", command, language="shell")

        path = _first_string(
            value,
            "path",
            "file_path",
            "target_path",
            "filename",
        )
        before = _first_string(value, "before", "old_string", "old_text")
        after = _first_string(value, "after", "new_string", "new_text")
        if before is not None and after is not None:
            return _preview_block(
                "Diff",
                _replacement_diff(before, after, path),
                language="diff",
                path=path,
            )
        diff = _first_string(value, "diff", "patch")
        if diff is not None:
            return _preview_block("Diff", diff, language="diff", path=path)

        content = _first_string(
            value,
            "content",
            "file_content",
            "new_content",
            "text",
        )
        if content is not None:
            return _preview_block(
                "File write" if path else "Content",
                content,
                path=path,
            )
        if path is not None:
            return _preview_block("Path", path)

    return _preview_block("Input", _summarize(value), language="json")


def _replacement_diff(before: str, after: str, path: str | None) -> str:
    label = path or "content"
    return "\n".join(
        unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=label,
            tofile=label,
            lineterm="",
        )
    )


def _preview_block(
    title: str,
    preview: str,
    *,
    language: str = "",
    path: str | None = None,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**{title}**",
        }
    ]
    if path:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": _bounded(path, APPROVAL_PATH_LIMIT),
                    "lines": 4,
                },
            }
        )
    bounded = _bounded(preview, APPROVAL_PREVIEW_LIMIT)
    if title == "Path" and path is None:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": bounded,
                    "lines": 8,
                },
            }
        )
    else:
        safe_preview = bounded.replace("```", "`` `")
        elements.append(
            {
                "tag": "markdown",
                "content": f"```{language}\n{safe_preview}\n```",
            }
        )
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "background_style": "grey-50",
                "padding": "12px",
                "vertical_spacing": "4px",
                "elements": elements,
            }
        ],
    }


def _question_card(prompt: QuestionPrompt) -> dict[str, Any]:
    questions = prompt.request.questions
    elements: list[dict[str, Any]] = [
        _context_block(
            f"Session: {prompt.session_title}",
            f"Workspace: {prompt.workspace}",
        )
    ]
    if len(questions) == 1 and not questions[0].multi_select:
        question = questions[0]
        elements.append(_context_block(_question_description(question)))
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "flow",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            {
                                "tag": "button",
                                "text": {
                                    "tag": "plain_text",
                                    "content": option.label[:100],
                                },
                                "type": "default",
                                "behaviors": [
                                    {
                                        "type": "callback",
                                        "value": {
                                            "interaction_id": prompt.interaction_id,
                                            "question_id": question.id,
                                            "option_id": option.id,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                    for option in question.options
                ]
                + [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            {
                                "tag": "button",
                                "text": {
                                    "tag": "plain_text",
                                    "content": "Skip",
                                },
                                "type": "default",
                                "behaviors": [
                                    {
                                        "type": "callback",
                                        "value": {
                                            "interaction_id": prompt.interaction_id,
                                            "question_id": question.id,
                                            "skipped": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        if question.allow_other:
            elements.append(
                {
                    "tag": "form",
                    "name": "other_answer",
                    "direction": "vertical",
                    "vertical_spacing": "8px",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "other_0",
                            "input_type": "multiline_text",
                            "rows": 3,
                            "max_length": 1000,
                            "width": "fill",
                            "label": {
                                "tag": "plain_text",
                                "content": question.other_label or "Other answer",
                            },
                        },
                        {
                            "tag": "button",
                            "name": "submit_other",
                            "form_action_type": "submit",
                            "text": {
                                "tag": "plain_text",
                                "content": "Submit answer",
                            },
                            "type": "primary_filled",
                            "width": "fill",
                        },
                    ],
                }
            )
    else:
        elements.append(_question_form(questions))
    return _card_shell(
        "Question from Kimi",
        prompt.session_title,
        template="blue",
        icon_token="myai_colorful",
        tag_text="Answer needed",
        tag_color="blue",
        elements=elements,
    )


def _question_form(questions: tuple[Question, ...]) -> dict[str, Any]:
    form_elements: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        form_elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": _question_description(question),
                    "text_size": "normal",
                    "lines": 8,
                },
            }
        )
        selector = {
            "tag": (
                "multi_select_static" if question.multi_select else "select_static"
            ),
            "name": f"q_{index}",
            "required": False,
            "width": "fill",
            "placeholder": {
                "tag": "plain_text",
                "content": "Choose one or more options",
            },
            "options": [
                {
                    "text": {"tag": "plain_text", "content": option.label},
                    "value": option.id,
                }
                for option in question.options
            ],
        }
        form_elements.append(selector)
        if question.allow_other:
            form_elements.append(
                {
                    "tag": "input",
                    "name": f"other_{index}",
                    "input_type": "multiline_text",
                    "rows": 2,
                    "max_length": 1000,
                    "width": "fill",
                    "label": {
                        "tag": "plain_text",
                        "content": question.other_label or "Other answer",
                    },
                }
            )
    form_elements.append(
        {
            "tag": "button",
            "name": "submit_answers",
            "form_action_type": "submit",
            "text": {"tag": "plain_text", "content": "Submit answers"},
            "type": "primary_filled",
            "width": "fill",
        }
    )
    return {
        "tag": "form",
        "name": "question_answers",
        "direction": "vertical",
        "vertical_spacing": "8px",
        "elements": form_elements,
    }


def _decode_question_response(
    prompt: QuestionPrompt,
    *,
    value: dict[str, Any],
    form_value: dict[str, Any],
    action_name: str | None,
) -> QuestionResponse | None:
    questions = prompt.request.questions
    question_id = value.get("question_id")
    if value.get("skipped") is True and isinstance(question_id, str):
        if not any(item.id == question_id for item in questions):
            raise ValueError("question card callback has an invalid question")
        return QuestionResponse((SkippedAnswer(question_id),))

    option_id = value.get("option_id")
    if isinstance(option_id, str) and isinstance(question_id, str):
        question = next((item for item in questions if item.id == question_id), None)
        if question is None or option_id not in {
            option.id for option in question.options
        }:
            raise ValueError("question card callback has an invalid option")
        return QuestionResponse((SingleChoiceAnswer(question_id, option_id),))

    if not form_value and action_name != "submit_answers":
        return None
    answers = []
    for index, question in enumerate(questions):
        selected = _selected_values(form_value.get(f"q_{index}"))
        option_ids = {option.id for option in question.options}
        if any(option_id not in option_ids for option_id in selected):
            raise ValueError("question card callback has an invalid option")
        if not question.multi_select and len(selected) > 1:
            raise ValueError("single-select question received multiple options")
        other_value = form_value.get(f"other_{index}")
        other = other_value.strip() if isinstance(other_value, str) else ""
        if other and not question.allow_other:
            raise ValueError("question does not allow a free-text answer")
        if question.multi_select:
            if other:
                answers.append(
                    MultipleChoiceWithOtherAnswer(
                        question.id, tuple(selected), other
                    )
                )
            elif selected:
                answers.append(MultipleChoiceAnswer(question.id, tuple(selected)))
            else:
                answers.append(SkippedAnswer(question.id))
        elif other:
            answers.append(OtherAnswer(question.id, other))
        elif selected:
            answers.append(SingleChoiceAnswer(question.id, selected[0]))
        else:
            answers.append(SkippedAnswer(question.id))
    return QuestionResponse(tuple(answers))


def _selected_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _question_description(question: Question) -> str:
    lines: list[str] = []
    if question.header:
        lines.append(question.header)
    if not lines or lines[-1] != question.text:
        lines.append(question.text)
    if question.body:
        lines.append(question.body)
    for option in question.options:
        if option.description:
            lines.append(f"{option.label}: {option.description}")
    return "\n".join(lines)


def _summarize(value: object) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return _bounded(text, INTERACTION_SUMMARY_LIMIT)


def _first_string(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None


def _bounded(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _escape_markdown(text: str) -> str:
    escaped = text.replace("\\", "\\\\")
    for character in "*~><[]()#:_`":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _status_card(title: str, detail: str, *, template: str) -> dict[str, Any]:
    tag_color = {
        "green": "green",
        "red": "red",
        "grey": "neutral",
    }.get(template, "blue")
    return _card_shell(
        title,
        "Kimi bridge",
        template=template,
        icon_token="notice_colorful",
        tag_text=title,
        tag_color=tag_color,
        elements=[_context_block(f"Interaction status: {detail}")],
    )
