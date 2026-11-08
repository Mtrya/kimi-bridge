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
        syntax="/new [workspace] [--env <id> [--cwd <environment-path>]]",
        summary="create and bind a session",
        section="Sessions",
        details="""**/new [workspace] [--env <id> [--cwd <environment-path>]]**

Create a new Kimi session and bind this conversation to it. An optional --env selects a configured native target; --cwd supplies its working directory without host path expansion. Quote paths containing spaces when combining them with flags. /environments inspects native targets; define targets and credentials in Kimi.

Arguments:
- `cwd` — workspace directory for the session. Must exist.

Defaults: without `cwd`, the configured default workspace is used.

Side effects: replaces the current binding; work running in the previously bound session is not aborted.

Example:
- `/new`
- `/new /tmp/experiment`""",
    ),
    "/archive": CommandHelp(
        syntax="/archive",
        summary="archive the bound session and clear its chat bindings",
        section="Sessions",
        details="""**/archive**

Archive the bound Kimi session and hide it from normal session listings. Takes no arguments. The command is sent directly to Kimi, including while the session is busy; Kimi stops its running work when archiving.

Side effects: preserves Kimi's stored history, closes the session's bridge stream and pending controls, and clears every bridge chat binding to that session. The next ordinary message creates a new session in `default_workspace`; use `/new <cwd>` to choose a different workspace. Cached listings containing the archived session are invalidated; run `/sessions` again before switching by number. If archiving fails, the bindings remain intact.

Example:
- `/archive`""",
    ),
    "/sessions": CommandHelp(
        syntax="/sessions [archived]",
        summary="list recent sessions",
        section="Sessions",
        details="""**/sessions**

List recent Kimi sessions with one-based indices and idle/busy state. Use `/sessions archived` for archived sessions and `/restore` to return to one. The list size is the configured `session_list_limit` (default 5).

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
    '/fork': CommandHelp(
        syntax='/fork',
        summary='fork and switch to a conversation branch',
        section='Sessions',
        details="**/fork**\n\nCreate a native conversation fork and switch this chat to it. Takes no arguments. The source can continue working in the same workspace; its in-progress answer is excluded according to Kimi's fork boundary.",
    ),
    '/restore': CommandHelp(
        syntax='/restore <n|id>',
        summary='restore and switch to an archived session',
        section='Sessions',
        details='**/restore <n|id>**\n\nRestore an archived session and bind this chat to it. Numbers refer to the latest /sessions archived list. An explicit ID needs no listing; use /restore id <id> for a numeric native ID. A failed restore preserves the old binding.',
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
        summary="show session tokens/context and managed account quota",
        section="Sessions",
        details="""**/usage**

Show cumulative session input/output/cache counters, live context usage, and a separate managed-account quota section with available window percentages and UTC reset times. Account-only failures keep valid session statistics. Takes no arguments; read-only.

Example:
- `/usage`""",
    ),
    "/compact": CommandHelp(
        syntax="/compact [instruction]",
        summary="compact session context and report event metrics",
        section="Sessions",
        details="""**/compact [instruction]**

Start context compaction and report the outcome. Optional instructions tell Kimi what to preserve in the summary; omit them for default compaction.

Side effects: rejected while the session is busy; mutates session history by compacting earlier prompts.

Example:
- `/compact`
- `/compact Preserve the API decisions and remaining test failures`""",
    ),
    "/history": CommandHelp(
        syntax="/history [count] [full]",
        summary="show the newest session messages",
        section="Sessions",
        details="""**/history [count] [full]**

Show the newest visible messages in the bound session's authoritative history — the post-undo, post-compaction view the agent currently sees. Thinking, tool calls, and tool results are omitted; other non-text content becomes `[attachment: <name>]` placeholders.

Arguments:
- `count` — messages to show, 1-50.
- `full` — show complete message bodies instead of truncating each one to 500 characters; may appear before or after `count`.

Defaults: `10` when omitted.

Side effects: none. `/undo` and `/switch` confirmations automatically include the newest 2 messages.

Example:
- `/history`
- `/history 20`
- `/history full`
- `/history 20 full`""",
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
- `auto` — approves ordinary tools and skips questions; Feishu/Telegram still ask for plan review.
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

Changing the model is allowed while busy. Later native requests use the new selection; an already sent request keeps its settings. The current thinking effort is preserved when supported, otherwise the model's advertised default (or `off`) is selected.

Example:
- `/model`
- `/model kimi-code/k3`""",
    ),
    "/secondary-model": CommandHelp(
        syntax="/secondary-model [alias] | set <alias> <description>",
        summary="show the subagent pool, select its default, or edit an entry",
        section="Control",
        details="""**/secondary-model [alias]**

Show the global default model, pool descriptions, and exact catalog aliases for newly spawned subagents, or select a default.

Arguments:
- `alias` — an exact alias from the live Kimi catalog, not a display name.
- `set <alias> <description>` — add a pool entry or replace its description. The description is the remaining text, including spaces; quotes are not needed.

Defaults: with no arguments, show the current selection, pool, and catalog. With no configured selection, subagents inherit their primary agent's model. Adding the first entry makes it the default; later additions keep the existing default.

Side effects: updates Kimi Code's global `[secondary_model]` configuration and affects later subagent spawns in every session, including sessions that are currently busy. Existing subagents and primary session models are unchanged. Pool edits preserve Kimi's `force` setting; when enabled, subagent selection remains forced to the default. To remove entries, reset the pool, or disable `force`, edit Kimi Code's configuration and run `/reload` in the session to refresh.

Example:
- `/secondary-model`
- `/secondary-model kimi-code/kimi-for-coding-highspeed`
- `/secondary-model set kimi-code/k3 Complex reasoning and debugging`""",
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

Setting an effort is allowed while busy. Later native requests use the new selection; an already sent request keeps its settings.

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

Side effects: setting plan mode is rejected while the session is busy. QQ/WeChat automatically approve completed plans; Feishu/Telegram present plan review in every permission mode.

Example:
- `/plan`
- `/plan on`""",
    ),
    '/queue': CommandHelp(
        syntax='/queue [message]',
        summary='inspect prompts or enqueue a subsequent turn',
        section='Control',
        details='**/queue [message]**\n\nWithout an argument, show the active prompt and numbered waiting prompts. /queue <message> submits separate work without steering. Use /queue -- <message> for text beginning with cancel or steer. The server owns ordering and execution.',
    ),
    '/queue cancel': CommandHelp(
        syntax='/queue cancel <n|id>',
        summary='cancel one selected prompt',
        section='Control',
        details='**/queue cancel <n|id>**\n\nCancel a selected waiting prompt using its native ID. Numbers refer to the latest queue list in this chat/session; use id <id> for a numeric native ID. Cancellation follows native behavior if the selected prompt has already started. /stop addresses active work and the whole waiting queue.',
    ),
    '/queue steer': CommandHelp(
        syntax='/queue steer <n|id>',
        summary='steer one waiting prompt into active work',
        section='Control',
        details='**/queue steer <n|id>**\n\nSteer one waiting prompt by its latest displayed number or explicit ID. Use id <id> for a numeric native ID. Kimi decides whether the prompt is still waiting; a failed request is not resubmitted.',
    ),
    '/swarm': CommandHelp(
        syntax='/swarm [on|off]',
        summary='inspect or set native swarm mode',
        section='Control',
        details='**/swarm [on|off]**\n\nShow the current native setting, or explicitly enable/disable swarm mode. Fresh /new sessions start with swarm mode off. Forking, restoring, and switching preserve native settings. Enabling swarm does not submit an objective.',
    ),
    '/goal edit': CommandHelp(
        syntax='/goal edit <objective>',
        summary='revise the current goal objective',
        section='Control',
        details='**/goal edit <objective>**\n\nEdit the current goal in place, preserving its identity, progress, budget, and status. Allowed while busy: the running turn continues and the revised objective applies from the next turn. Kimi reports missing or completed goals. Use /goal -- edit ... to create a literal objective beginning with edit.',
    ),    "/goal": CommandHelp(
        syntax="/goal [status|edit <objective>|pause|resume|cancel|-- <objective>|<objective>]",
        summary="inspect or control a goal",
        section="Control",
        details="""**/goal [status|edit <objective>|pause|resume|cancel|-- <objective>|<objective>]**

Inspect or control the session goal.

Forms:
- `/goal` or `/goal status` — show the goal state and remaining budgets.
- `/goal <objective>` — create a goal and submit the objective as a normal turn.
- `/goal edit <objective>` — revise the current goal without resetting identity, progress, status, or budget.
- `/goal -- <objective>` — required when the objective begins with `status`, `edit`, `pause`, `resume`, or `cancel`.
- `/goal pause` — pause the goal and cancel its active prompt.
- `/goal resume` — reactivate a paused or blocked goal.
- `/goal cancel` — cancel the goal and its active prompt.

Defaults: bare `/goal` is equivalent to `/goal status`.

Side effects: goal creation and `/goal resume` are rejected while the session is busy. `/goal edit` is available during work and applies to the next turn. Only one goal exists at a time. Details: `/goal status ?`, `/goal edit ?`, `/goal pause ?`, `/goal resume ?`, `/goal cancel ?`.

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
    "/reload": CommandHelp(
        syntax="/reload",
        summary="reload Kimi settings and refresh the bound session",
        section="Control",
        details="""**/reload**

Reload Kimi Code's configuration, plugins, skills, and agent definitions, then rebuild the bound session with its existing history. Takes no arguments and requires a bound session.

Side effects: rejected during running agent turns or compaction. The server and bridge stay online; the session ID, selected model, and bridge permission mode are preserved. Global Kimi settings are refreshed, and new sessions use the latest default model. The bridge's own configuration is loaded at bridge startup.

Example:
- `/reload`""",
    ),
    "/btw": CommandHelp(
        syntax="/btw <question>",
        summary="ask a native read-only side question",
        section="Activity",
        details="Ask one side question using the current native context. Its answer is labelled Side question; main work and ordinary steering continue independently. Another /btw replaces the previous side question. Switching sessions, archiving, or shutting down closes it as in the native interface. Native read-only tools are enforced by Kimi; the bridge does not resubmit uncertain work.",
    ),
    "/agents": CommandHelp(
        syntax="/agents [show|background <n|id>]",
        summary="inspect live agents or background their owning task",
        section="Activity",
        details="/agents shows the main agent and discoverable subagents, their parents, assignment, model and state. /agents show <n|id> is read-only. /agents background <n|id> releases a supported foreground wait; swarm members cannot be detached individually. Numbers refer to the latest display in this chat/session; use id <id> for numeric native IDs. Ordinary messages still target main.",
    ),
    "/todos": CommandHelp(
        syntax="/todos [all]",
        summary="show main or all available agent checklists",
        section="Activity",
        details="/todos shows the main checklist. /todos all groups available checklists by agent. Counts show completed/total, with ✓ done, → in progress, and ○ pending. These are on-demand native snapshots; no prompt is submitted.",
    ),
    "/tasks background": CommandHelp(
        syntax="/tasks background <n|id>",
        summary="release a supported task’s foreground wait",
        section="Activity",
        details="Background a task by the latest task number in this chat/session or explicit native ID. Use id <id> for numeric IDs. The result distinguishes newly backgrounded, already backgrounded, ended and unsupported work. Native swarm members cannot be detached individually. /tasks show remains the on-demand output command.",
    ),
    "/tasks": CommandHelp(
        syntax="/tasks [all|completed|failed|cancelled]",
        summary="list tasks",
        section="Tasks and tools",
        details="""**/tasks [all|completed|failed|cancelled]**

List numbered background tasks of the bound session. Use the displayed number with `/tasks show` or `/tasks cancel`. Numbers refer to your latest list in this chat and session, including any status filter, and stay fixed until you list tasks again. Changing the bound session clears your task numbers across chats.

Arguments:
- `all` — include tasks in every status.
- an optional terminal status filter: `completed`, `failed`, or `cancelled`.

Defaults: without an argument, lists running tasks.

Details: `/tasks show ?`, `/tasks cancel ?`.

Example:
- `/tasks`
- `/tasks all`""",
    ),
    "/tasks show": CommandHelp(
        syntax="/tasks show <n|id>",
        summary="inspect a task with an 8 KiB output tail",
        section="Tasks and tools",
        details="""**/tasks show <n|id>**

Inspect one task, including at most the last 8 KiB of its output.

Arguments:
- `n` — a number from your latest `/tasks` list in this chat and session.
- `id` — a task ID from `/tasks`.

Bare numbers select list positions. Use `/tasks show id <id>` to select an exact ID, including a numeric ID.

Example:
- `/tasks show 2`
- `/tasks show id 123`""",
    ),
    "/tasks cancel": CommandHelp(
        syntax="/tasks cancel <n|id>",
        summary="cancel a task",
        section="Tasks and tools",
        details="""**/tasks cancel <n|id>**

Cancel a background task.

Arguments:
- `n` — a number from your latest `/tasks` list in this chat and session.
- `id` — a task ID from `/tasks`.

Bare numbers select list positions. Use `/tasks cancel id <id>` to select an exact ID, including a numeric ID.

Side effects: executes immediately, even while a turn is busy.

Example:
- `/tasks cancel 1`
- `/tasks cancel id 123`""",
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
    "/plugins": CommandHelp(
        syntax="/plugins [show <name>]",
        summary="inspect installed native plugins and contributions",
        section="Tasks and tools",
        details="List installed plugins with enabled/error state, or inspect a plugin ID/name to see declared skills/MCP contributions, diagnostic severity counts and tools currently available in the bound session. Raw configuration and diagnostic messages stay in Kimi. Installation, enable/disable, removal and credentials are managed there.",
    ),
    "/mcp": CommandHelp(
        syntax="/mcp [servers|show <server>|restart <server>]",
        summary="inspect tools, native connections or restart a server",
        section="Tasks and tools",
        details="Bare /mcp lists available bound-session MCP tools. servers lists native runtime connection states and bound-session configuration authentication states; show adds available bound-session tools for one server. The native runtime list/restart addresses the most recently created session only if that session is live, which can differ from the bound session. restart acknowledges a reconnect request; use show to inspect later health. Configuration/authentication remain managed in Kimi.",
    ),
    "/environments": CommandHelp(
        syntax="/environments",
        summary="inspect configured native execution targets",
        section="Sessions",
        details="List native local/SSH/Docker/command targets and their connection state for the bound session. Create target definitions and credentials in Kimi. Select a target with /new [workspace] --env <id> [--cwd <environment-path>]. Workspace paths belong to the bridge host; target cwd and file paths belong to the selected environment. Existing sessions restore their native target on resume; target failures do not fall back to host files.",
    ),
    "/workspace": CommandHelp(
        syntax="/workspace [add|remove|primary <path>]",
        summary="inspect or change shared native workspace folders",
        section="Sessions",
        details="Show the native workspace ID, primary/additional folders and this session’s working directory. add appends a folder; remove removes an additional folder without deleting files; primary atomically swaps primary/additional roles. Changes apply to every session using this workspace. Existing sessions retain their native working directories, including access to that directory after removal from the shared set. Paths consume the remaining text, including spaces.",
    ),
    "/files": CommandHelp(
        syntax="/files [directory] | /files search <query> | /files show|send|diff <n|path> | /files changes",
        summary="browse, preview or send native workspace files",
        section="Output",
        details="List up to 100 entries in the session working directory or a specified directory; search returns up to 50 filename matches. Native ignore rules apply. A successful listing/search/changes view replaces this chat/session’s numbered choices; switching sessions or changing folders invalidates them. show previews up to 8 KiB of text; binary files are identified. send delivers a file through the platform’s native file/media handling. Use ./123 for a numeric filename, paths with spaces directly, and /files -- search for a directory named search. Native workspace containment and symlink checks apply to each file access. /files changes lists the latest finished main turn’s recorded changes. /files diff compares its start/end checkpoints, including deleted files. A numbered changes selection retains that turn; explicit paths use the latest finished turn. Binary, oversize, missing, unrecorded and truncated results are identified. Recorded diffs do not read the current working tree.",
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
        if argument.endswith(suffix) and (
            _has_sub_forms(command)
            or (
                command == "/files"
                and argument[: -len(suffix)].strip()
                in {"show", "send", "diff", "search", "changes"}
            )
        ):
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
    for entry in COMMAND_HELP.values():
        if entry.section != section:
            section = entry.section
            lines.append(f"\n**{section}**")
        lines.append(f"- **{entry.syntax}** — {entry.summary}")
    return "\n".join(lines)
