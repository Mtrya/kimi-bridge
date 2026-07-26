"""Per-command help registry backing ``/help`` and ``/<command> ?``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandHelp:
    """Help for one command or sub-form, keyed like ``/tasks show``."""

    syntax: str
    summary: str
    details: str
    section: str


HELP_TOKENS = ("?",)

COMMAND_HELP: dict[str, CommandHelp] = {
    "/new": CommandHelp(
        syntax="/new [cwd]",
        summary="create and bind a session",
        section="Sessions",
        details="""**/new [cwd]**

Create a new Kimi session and bind this conversation to it.

Arguments:
- `cwd` — workspace directory for the session. Must exist.

Defaults: without `cwd`, the configured default workspace is used.

Side effects: replaces the current binding; work running in the previously bound session is not aborted.

Example:
- `/new`
- `/new /tmp/experiment`""",
    ),
    "/sessions": CommandHelp(
        syntax="/sessions",
        summary="list recent sessions",
        section="Sessions",
        details="""**/sessions**

List recent Kimi sessions with one-based indices and idle/busy state. The list size is the configured `session_list_limit` (default 10).

Side effects: remembers the displayed indices so `/switch <n>` can reference them.

Details: `/sessions search ?`.

Example:
- `/sessions`""",
    ),
    "/sessions search": CommandHelp(
        syntax="/sessions search <keyword>",
        summary="search sessions by title or workspace",
        section="Sessions",
        details="""**/sessions search <keyword>**

Search all active (non-archived) Kimi sessions, not just the recent window.

Arguments:
- `keyword` — case-insensitive substring matched against session titles and workspace paths.

Results are ranked by recency, capped at the configured `session_list_limit`, presented like `/sessions`, and remembered for `/switch <n>`.

Example:
- `/sessions search login`
- `/sessions search /tmp/experiment`""",
    ),
    "/switch": CommandHelp(
        syntax="/switch <n|id|title>",
        summary="bind a listed, explicit, or titled session",
        section="Sessions",
        details="""**/switch <n|id|title>**

Bind this conversation to another session. Precedence: numeric argument is a list index, an id-shaped argument is a session ID, anything else is a title.

Arguments:
- `n` — a one-based index from the most recent `/sessions` or `/sessions search` listing.
- `id` — an explicit session ID.
- `title` — an exact case-insensitive session title among active (non-archived) sessions. Multiple matches produce a numbered candidate list for `/switch <n>`; no match reports `Session not found`.

Side effects: replaces the current binding; work running in the previously bound session is not aborted. Render-thinking preference is preserved.

Example:
- `/switch 2`
- `/switch 01932f4a-...`
- `/switch Login refactor`""",
    ),
    "/status": CommandHelp(
        syntax="/status",
        summary="show bound session and runtime state",
        section="Sessions",
        details="""**/status**

Show session ID, workspace, busy state, pending interaction, model, effort, plan mode, permission mode, and Kimi Code version. Takes no arguments.

Example:
- `/status`""",
    ),
    "/title": CommandHelp(
        syntax="/title [text]",
        summary="show or rename the session",
        section="Sessions",
        details="""**/title [text]**

Show the current session title, or rename the session.

Arguments:
- `text` — the new title. May contain spaces.

Defaults: without `text`, only shows the current title.

Example:
- `/title`
- `/title Login refactor`""",
    ),
    "/usage": CommandHelp(
        syntax="/usage",
        summary="show live session token totals and context usage",
        section="Sessions",
        details="""**/usage**

Show live input, output, cache-read, cache-creation, and context-window token values when exposed by Kimi. Takes no arguments.

Example:
- `/usage`""",
    ),
    "/compact": CommandHelp(
        syntax="/compact",
        summary="compact session context and report event metrics",
        section="Sessions",
        details="""**/compact**

Start context compaction and edit one progress message with the outcome. Takes no arguments.

Side effects: rejected while the session is busy; mutates session history by compacting earlier prompts.

Example:
- `/compact`""",
    ),
    "/undo": CommandHelp(
        syntax="/undo [count]",
        summary="undo one or more history steps",
        section="Sessions",
        details="""**/undo [count]**

Undo history steps in the bound session.

Arguments:
- `count` — a positive decimal integer.

Defaults: `1` when omitted.

Side effects: rejected while the session is busy. Kimi enforces history and compaction boundaries.

Example:
- `/undo`
- `/undo 3`""",
    ),
    "/mode": CommandHelp(
        syntax="/mode <manual|auto|yolo>",
        summary="set the session permission mode",
        section="Control",
        details="""**/mode <manual|auto|yolo>**

Set the session permission mode. The argument is required.

Arguments:
- `manual` — approvals and questions are answered in chat.
- `auto` — fully autonomous; the agent never asks.
- `yolo` — regular tools are auto-approved; the agent may still ask questions.

Side effects: affects later permission checks only; it does not answer a currently displayed approval or question.

Example:
- `/mode yolo`""",
    ),
    "/model": CommandHelp(
        syntax="/model [alias]",
        summary="show or set the exact session model",
        section="Control",
        details="""**/model [alias]**

Show the current model and exact catalog aliases, or select one.

Arguments:
- `alias` — an exact alias from the live Kimi catalog, not a display name.

Defaults: without `alias`, only lists the catalog.

Side effects: changing the model is rejected while the session is busy. The current thinking effort is preserved when supported, otherwise the model's advertised default (or `off`) is selected.

Example:
- `/model`
- `/model kimi-code/k3`""",
    ),
    "/effort": CommandHelp(
        syntax="/effort [effort]",
        summary="show or set thinking effort for the current model",
        section="Control",
        details="""**/effort [effort]**

Show the current thinking effort and valid choices, or set one.

Arguments:
- `effort` — a value advertised for the active model.

Defaults: without `effort`, only shows the current value and choices.

Side effects: setting an effort is rejected while the session is busy.

Example:
- `/effort`
- `/effort high`""",
    ),
    "/plan": CommandHelp(
        syntax="/plan [on|off]",
        summary="show or explicitly set plan mode",
        section="Control",
        details="""**/plan [on|off]**

Show the current plan-mode state, or set it explicitly.

Arguments:
- `on` / `off` — the desired state.

Defaults: without an argument, only shows the current state.

Side effects: setting plan mode is rejected while the session is busy.

Example:
- `/plan`
- `/plan on`""",
    ),
    "/goal": CommandHelp(
        syntax="/goal [status|pause|resume|cancel|-- <objective>|<objective>]",
        summary="inspect or control a goal",
        section="Control",
        details="""**/goal [status|pause|resume|cancel|-- <objective>|<objective>]**

Inspect or control the session goal.

Forms:
- `/goal` or `/goal status` — show the goal state and remaining budgets.
- `/goal <objective>` — create a goal and submit the objective as a normal turn.
- `/goal -- <objective>` — required when the objective begins with `status`, `pause`, `resume`, or `cancel`.
- `/goal pause` — pause the goal and cancel its active prompt.
- `/goal resume` — reactivate a paused or blocked goal.
- `/goal cancel` — cancel the goal and its active prompt.

Defaults: bare `/goal` is equivalent to `/goal status`.

Side effects: goal creation and `/goal resume` are rejected while the session is busy. Only one goal exists at a time; replace it by cancelling first. Details: `/goal status ?`, `/goal pause ?`, `/goal resume ?`, `/goal cancel ?`.

Example:
- `/goal Stabilize the flaky login tests`
- `/goal -- status of the migration`""",
    ),
    "/goal status": CommandHelp(
        syntax="/goal status",
        summary="show goal state and budgets",
        section="Control",
        details="""**/goal status**

Show the public goal state and remaining budgets. Equivalent to bare `/goal`.

Example:
- `/goal status`""",
    ),
    "/goal pause": CommandHelp(
        syntax="/goal pause",
        summary="pause the current goal",
        section="Control",
        details="""**/goal pause**

Pause the current goal and cancel its active prompt or pending interaction. Reports `No active goal.` when none exists.

Example:
- `/goal pause`""",
    ),
    "/goal resume": CommandHelp(
        syntax="/goal resume",
        summary="reactivate a paused or blocked goal",
        section="Control",
        details="""**/goal resume**

Reactivate a paused or blocked goal. A blocked goal stays blocked until resumed; an ordinary follow-up prompt does not reactivate it.

Side effects: rejected while the session is busy. Reports `No active goal.` when none exists.

Example:
- `/goal resume`""",
    ),
    "/goal cancel": CommandHelp(
        syntax="/goal cancel",
        summary="cancel the current goal",
        section="Control",
        details="""**/goal cancel**

Cancel the current goal and its active prompt or pending interaction. Reports `No active goal.` when none exists.

Example:
- `/goal cancel`""",
    ),
    "/stop": CommandHelp(
        syntax="/stop",
        summary="stop the active turn and discard queued prompts",
        section="Control",
        details="""**/stop**

Abort the active main-agent turn, discard active and queued prompts, and cancel its pending interaction. Takes no arguments.

Side effects: idempotent; reports `Stopped.` even when the session is already idle. Detached tasks keep running and stay available through `/tasks`.

Example:
- `/stop`""",
    ),
    "/tasks": CommandHelp(
        syntax="/tasks [running|completed|failed|cancelled]",
        summary="list tasks",
        section="Tasks and tools",
        details="""**/tasks [running|completed|failed|cancelled]**

List background tasks of the bound session.

Arguments:
- an optional status filter: `running`, `completed`, `failed`, or `cancelled`.

Defaults: without a filter, lists all tasks.

Details: `/tasks show ?`, `/tasks cancel ?`.

Example:
- `/tasks`
- `/tasks running`""",
    ),
    "/tasks show": CommandHelp(
        syntax="/tasks show <id>",
        summary="inspect a task with an 8 KiB output tail",
        section="Tasks and tools",
        details="""**/tasks show <id>**

Inspect one task, including at most the last 8 KiB of its output.

Arguments:
- `id` — a task ID from `/tasks`.

Example:
- `/tasks show 3f2a1b`""",
    ),
    "/tasks cancel": CommandHelp(
        syntax="/tasks cancel <id>",
        summary="cancel a task",
        section="Tasks and tools",
        details="""**/tasks cancel <id>**

Cancel a background task.

Arguments:
- `id` — a task ID from `/tasks`.

Side effects: executes immediately, even while a turn is busy.

Example:
- `/tasks cancel 3f2a1b`""",
    ),
    "/skills": CommandHelp(
        syntax="/skills",
        summary="list skills available to the session",
        section="Tasks and tools",
        details="""**/skills**

List the skills available to the bound session, with exact names for `/skills run`.

Details: `/skills run ?`.

Example:
- `/skills`""",
    ),
    "/skills run": CommandHelp(
        syntax="/skills run <name> [args]",
        summary="activate an exact skill",
        section="Tasks and tools",
        details="""**/skills run <name> [args]**

Activate a skill as a normal streamed turn.

Arguments:
- `name` — an exact skill name from `/skills`.
- `args` — optional arguments passed to the skill activation.

Side effects: rejected while the session is busy.

Example:
- `/skills run commit`
- `/skills run pdf report.pdf`""",
    ),
    "/mcp": CommandHelp(
        syntax="/mcp",
        summary="list session-derived MCP tools",
        section="Tasks and tools",
        details="""**/mcp**

List the MCP tools resolved for the bound session. Read-only; takes no arguments.

Example:
- `/mcp`""",
    ),
    "/send": CommandHelp(
        syntax="/send <path>",
        summary="send one file from the bound workspace",
        section="Output",
        details="""**/send <path>**

Send one regular file from the bound workspace into the chat.

Arguments:
- `path` — a relative path resolved from the bound workspace, or an absolute path that stays inside it.

Side effects: missing paths, directories, globs, multiple files, and symlinks escaping the workspace are rejected. Feishu sends JPEG/PNG natively, MP4 as native media, and everything else as a file; Telegram sends JPEG/PNG as photos and everything else as documents.

Example:
- `/send build/report.pdf`
- `/send /tmp/workspace/chart.png`""",
    ),
    "/render-thinking": CommandHelp(
        syntax="/render-thinking [on|off]",
        summary="show or set separate thinking output",
        section="Output",
        details="""**/render-thinking [on|off]**

Show or set whether thinking streams separately from the answer in this conversation.

Arguments:
- `on` / `off` — the desired state.

Defaults: without an argument, only shows the current state.

Side effects: the preference persists per conversation. Enabling during a live turn backfills the current thinking snapshot; disabling freezes the visible thinking while the answer continues.

Example:
- `/render-thinking`
- `/render-thinking on`""",
    ),
    "/help": CommandHelp(
        syntax="/help",
        summary="show this help",
        section="General",
        details="""**/help**

Show the compact command index.

Every command also answers `/<command> ?` with detailed usage, including sub-forms such as `/tasks show ?`.

Example:
- `/help`
- `/goal ?`""",
    ),
}


def command_help_details(command: str, argument: str) -> str | None:
    """Return detailed help when *argument* is a help request, else None.

    A bare `?` argument (`/goal ?`) always requests help. A trailing `?`
    token (`/tasks show ?`) requests sub-form help only for commands with
    registered sub-forms, so free-form arguments such as `/title hello ?`
    keep their literal meaning; a sub-path starting with `--` is the
    `/goal` objective escape and is never a help request. Lookup tries
    the longest registered key first and falls back to the parent command
    for unregistered sub-forms.
    """
    path: str | None = None
    for token in HELP_TOKENS:
        if argument == token:
            path = ""
            break
        suffix = f" {token}"
        if argument.endswith(suffix) and _has_sub_forms(command):
            path = argument[: -len(suffix)]
            break
    if path is None or path.startswith("--"):
        return None
    path = " ".join(path.split())
    key = f"{command} {path}".strip()
    while key:
        entry = COMMAND_HELP.get(key)
        if entry is not None:
            return entry.details
        key, separator, _ = key.rpartition(" ")
        if not separator:
            break
    return None


def _has_sub_forms(command: str) -> bool:
    return any(key.startswith(f"{command} ") for key in COMMAND_HELP)


def render_help_index() -> str:
    """Render the compact `/help` index from the registry."""
    lines = ["**Commands**"]
    section = ""
    for key, entry in COMMAND_HELP.items():
        if entry.section != section:
            section = entry.section
            lines.append(f"\n**{section}**")
        lines.append(f"- **{entry.syntax}** — {entry.summary} (details: `{key} ?`)")
    return "\n".join(lines)
