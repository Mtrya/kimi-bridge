"""Platform-neutral agent interaction models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


ApprovalDecision: TypeAlias = Literal["approved", "rejected", "cancelled"]


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: str
    session_id: str
    tool_name: str
    action: str
    input_display: object | None = None


@dataclass(frozen=True, slots=True)
class QuestionOption:
    id: str
    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    text: str
    options: tuple[QuestionOption, ...]
    header: str | None = None
    body: str | None = None
    multi_select: bool = False
    allow_other: bool = False
    other_label: str | None = None


@dataclass(frozen=True, slots=True)
class QuestionRequest:
    id: str
    session_id: str
    questions: tuple[Question, ...]


@dataclass(frozen=True, slots=True)
class SkippedAnswer:
    question_id: str


@dataclass(frozen=True, slots=True)
class SingleChoiceAnswer:
    question_id: str
    option_id: str


@dataclass(frozen=True, slots=True)
class MultipleChoiceAnswer:
    question_id: str
    option_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OtherAnswer:
    question_id: str
    text: str


@dataclass(frozen=True, slots=True)
class MultipleChoiceWithOtherAnswer:
    question_id: str
    option_ids: tuple[str, ...]
    text: str


QuestionAnswer: TypeAlias = (
    SkippedAnswer
    | SingleChoiceAnswer
    | MultipleChoiceAnswer
    | OtherAnswer
    | MultipleChoiceWithOtherAnswer
)


@dataclass(frozen=True, slots=True)
class ApprovalPrompt:
    interaction_id: str
    request: ApprovalRequest
    session_title: str
    workspace: str


@dataclass(frozen=True, slots=True)
class QuestionPrompt:
    interaction_id: str
    request: QuestionRequest
    session_title: str
    workspace: str


InteractionPrompt: TypeAlias = ApprovalPrompt | QuestionPrompt


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    decision: ApprovalDecision


@dataclass(frozen=True, slots=True)
class QuestionResponse:
    answers: tuple[QuestionAnswer, ...]


InteractionResponse: TypeAlias = ApprovalResponse | QuestionResponse
InteractionState: TypeAlias = Literal["completed", "timed_out", "stale", "cancelled"]


@dataclass(frozen=True, slots=True)
class InteractionOutcome:
    state: InteractionState
    detail: str
    approval_decision: ApprovalDecision | None = None
