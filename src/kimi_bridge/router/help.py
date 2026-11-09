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
        summary='start a session in a folder',
        section='Sessions',
        details="""**/new [workspace]**

Start a fresh session in a folder and switch this chat to it.

**Try**
- `/new` — use the configured default folder.
- `/new /path/to/project` — work in another project.

**Remote target**
`/new "/host/project" --env <id> --cwd "/target/project"`
Find configured targets with `/environments`.

Work in the previous session keeps running.""",
    ),
    "/archive": CommandHelp(
        syntax="/archive",
        summary='archive the current session',
        section='Sessions',
        details="""**/archive**

Archive the current session. Kimi stops its running work and keeps its history.

**Return later**
`/sessions archived` → `/restore <number>`

The next ordinary message starts a fresh session. A failed archive keeps the current binding.""",
    ),
    "/sessions": CommandHelp(
        syntax="/sessions [archived]",
        summary='find sessions',
        section='Sessions',
        details="""**/sessions**

Find a session, then switch by its displayed number.

**Use**
- `/sessions` — recent sessions, newest first.
- `/sessions search login` — search titles and folder paths.
- `/sessions archived` — archived sessions; use `/restore` to reopen one.

**Next**
`/switch 2`""",
    ),
    "/sessions search": CommandHelp(
        syntax="/sessions search <keyword>",
        summary="search sessions by title or workspace",
        section='Sessions',
        details="""**/sessions search <keyword>**

Find active sessions by title or folder path, newest first.

**Try**
`/sessions search login`

Switch with `/switch <number>`. Results use your configured list limit.""",
    ),
    "/switch": CommandHelp(
        syntax="/switch <n|id|title>",
        summary='resume another session',
        section='Sessions',
        details="""**/switch <number|id|title>**

Resume another session in this chat, including one in a different workspace.

**Try**
- `/sessions` → `/switch 2`
- `/switch Login refactor` — exact title, ignoring case.

Numbers use your latest session list. Duplicate titles produce a choice list. Work in the previous session keeps running; a failed switch keeps it selected. Archived sessions need `/restore` first.""",
    ),
    '/fork': CommandHelp(
        syntax='/fork',
        summary='branch the conversation',
        section='Sessions',
        details="""**/fork**

Branch the conversation and switch to the new session.

The source keeps running in the same workspace. Kimi decides which completed history belongs in the fork; the unfinished answer is excluded.""",
    ),
    '/restore': CommandHelp(
        syntax='/restore <n|id>',
        summary='reopen an archived session',
        section='Sessions',
        details="""**/restore <number|id>**

Reopen an archived session and switch to it.

**Try**
`/sessions archived` → `/restore 1`

An explicit ID also works. Use `/restore id 123` for a numeric ID. A failed restore keeps the current session.""",
    ),
    "/status": CommandHelp(
        syntax="/status",
        summary='current session at a glance',
        section='Sessions',
        details="""**/status**

See the current session, model, working folder, modes and Kimi version.

For folders use `/workspace`; for context and account limits use `/usage`.""",
    ),
    "/title": CommandHelp(
        syntax="/title [text]",
        summary='show or rename the session',
        section='Sessions',
        details="""**/title [text]**

**Use**
- `/title` — see the current title.
- `/title Login refactor` — rename the session.""",
    ),
    "/usage": CommandHelp(
        syntax="/usage",
        summary='context and account limits',
        section='Sessions',
        details="""**/usage**

See reported session token counters, context usage and managed-account limits.

Context and quota ratios include bars and percentages. Quota reset times use UTC when a time zone is supplied. If account lookup fails, session statistics remain visible.""",
    ),
    "/compact": CommandHelp(
        syntax="/compact [instruction]",
        summary='free context space',
        section='Maintenance',
        details="""**/compact [instruction]**

Summarize older context to free space. The session must be idle.

**Try**
`/compact Keep the API decisions and unresolved test failures`

Without instructions, Kimi chooses what to keep.""",
    ),
    "/history": CommandHelp(
        syntax="/history [count] [full]",
        summary='read recent conversation',
        section='Sessions',
        details="""**/history [count] [full]**

Read the history the agent currently sees after undo or compaction.

**Use**
- `/history` — newest 10 messages.
- `/history 20` — choose 1–50 messages.
- `/history 20 full` — keep complete message bodies.

Thinking and tool traffic are omitted; attachments are labelled.""",
    ),
    "/undo": CommandHelp(
        syntax="/undo [count]",
        summary='undo history steps',
        section='Maintenance',
        details="""**/undo [count]**

Undo history steps while the session is idle.

**Try**
- `/undo` — one step.
- `/undo 3` — three steps.

Kimi enforces history and compaction boundaries.""",
    ),
    "/mode": CommandHelp(
        syntax="/mode <manual|auto|yolo>",
        summary='permission mode',
        section='Settings',
        details="""**/mode <manual|auto|yolo>**

Choose how future permission checks are handled.

- `manual` — answer approvals and questions in chat.
- `auto` — approve ordinary tools and skip questions.
- `yolo` — approve routine tools; questions may still appear.

**Platform rule**
QQ and WeChat use fixed auto mode and auto-approve plans. Feishu and Telegram still ask for plan review. Changing mode does not answer an existing prompt.""",
    ),
    "/model": CommandHelp(
        syntax="/model [alias]",
        summary='choose a model',
        section='Settings',
        details="""**/model [alias]**

**Use**
- `/model` — see available models and the current selection.
- `/model kimi-code/k3` — choose an exact catalog alias.

Changes apply to later requests, including while busy. If the current effort is unsupported, Kimi uses the model's default or off.""",
    ),
    "/secondary-model": CommandHelp(
        syntax="/secondary-model [alias] | set <alias> <description>",
        summary='models for future subagents',
        section='Settings',
        details="""**/secondary-model**

Choose models for newly spawned subagents across all sessions.

**Use**
- `/secondary-model` — see the default, pool and available models.
- `/secondary-model <alias>` — choose the default.
- `/secondary-model set <alias> <description>` — add or describe a pool entry.

**Scope**
This is a global Kimi setting. Existing agents keep their models. With no selection, subagents inherit the primary model. The first pool entry becomes the default; later additions keep it.

Remove entries, reset the pool or change forced selection in Kimi's configuration, then `/reload`.""",
    ),
    "/effort": CommandHelp(
        syntax="/effort [effort]",
        summary='thinking effort',
        section='Settings',
        details="""**/effort [level]**

**Use**
- `/effort` — see the current level and supported choices.
- `/effort high` — choose a supported level.

Changes apply to later requests; an already sent request keeps its settings.""",
    ),
    "/plan": CommandHelp(
        syntax="/plan [on|off]",
        summary='plan mode',
        section='Settings',
        details="""**/plan [on|off]**

**Use**
- `/plan` — see the current mode.
- `/plan on` — plan before implementing.
- `/plan off` — leave plan mode.

Change it while idle. QQ/WeChat auto-approve completed plans; Feishu/Telegram show plan review in every permission mode.""",
    ),
    '/queue': CommandHelp(
        syntax='/queue [message]',
        summary='waiting prompts and steering',
        section='Work',
        details="""**/queue**

Keep follow-up work separate from ordinary steering.

**Use**
- `/queue` — see the active prompt and numbered waiting prompts.
- `/queue <message>` — enqueue a later turn.
- `/queue cancel <number>` — cancel one selected prompt.
- `/queue steer <number>` — steer a waiting prompt into active work.

For messages starting with cancel or steer, use `/queue -- <message>`. Kimi controls ordering and races.""",
    ),
    '/queue cancel': CommandHelp(
        syntax='/queue cancel <n|id>',
        summary='cancel one selected prompt',
        section='Work',
        details="""**/queue cancel <number|id>**

Cancel a prompt from the latest `/queue` list.

**Try**
`/queue` → `/queue cancel 2`

Use `/queue cancel id 123` for a numeric ID. If the prompt has already started, Kimi applies cancellation to that prompt.""",
    ),
    '/queue steer': CommandHelp(
        syntax='/queue steer <n|id>',
        summary='steer one waiting prompt into active work',
        section='Work',
        details="""**/queue steer <number|id>**

Move one waiting prompt into the active work as steering.

**Try**
`/queue` → `/queue steer 1`

Use `/queue steer id 123` for a numeric ID. Kimi decides the result if its state changed.""",
    ),
    '/swarm': CommandHelp(
        syntax='/swarm [on|off]',
        summary='swarm mode',
        section='Settings',
        details="""**/swarm [on|off]**

**Use**
- `/swarm` — see the current state.
- `/swarm on` or `/swarm off` — choose explicitly.

Changing the flag does not submit work. Fresh bridge sessions start with swarm off; forks and restored sessions inherit native settings.""",
    ),
    '/goal edit': CommandHelp(
        syntax='/goal edit <objective>',
        summary='revise the current goal objective',
        section='Work',
        details="""**/goal edit <objective>**

Revise the current goal without resetting its identity, progress, budget or status.

**Try**
`/goal edit Fix login and verify the password-reset flow`

A running turn continues; the next goal turn uses the revised objective. Missing or completed goals follow Kimi's errors.""",
    ),    "/goal": CommandHelp(
        syntax="/goal [status|edit <objective>|pause|resume|cancel|-- <objective>|<objective>]",
        summary='autonomous goals',
        section='Work',
        details="""**/goal [objective]**

Start an autonomous goal, or inspect and control the current one.

**Start**
`/goal Stabilize the login tests`

**Manage**
- `/goal status` — progress and budgets.
- `/goal edit <objective>` — revise the goal in place.
- `/goal pause` or `/goal resume` — pause or continue.
- `/goal cancel` — end the goal.

Bare `/goal` shows status. Creation and resume require an idle session; editing is allowed while busy. Use `/goal -- <objective>` when the objective starts with a command word such as edit or status.

**Example**
`/goal -- status of the migration`""",
    ),
    "/goal status": CommandHelp(
        syntax="/goal status",
        summary="show goal state and budgets",
        section='Work',
        details="""**/goal status**

See the current objective, progress, remaining budgets and any terminal reason.

Bare `/goal` does the same.""",
    ),
    "/goal pause": CommandHelp(
        syntax="/goal pause",
        summary="pause the current goal",
        section='Work',
        details="""**/goal pause**

Pause the current goal and cancel its active prompt or pending interaction.

Continue with `/goal resume`.""",
    ),
    "/goal resume": CommandHelp(
        syntax="/goal resume",
        summary="reactivate a paused or blocked goal",
        section='Work',
        details="""**/goal resume**

Continue a paused or blocked goal while the session is idle.

An ordinary message does not reactivate a blocked goal.""",
    ),
    "/goal cancel": CommandHelp(
        syntax="/goal cancel",
        summary="cancel the current goal",
        section='Work',
        details="""**/goal cancel**

Cancel the current goal and its active prompt or pending interaction.""",
    ),
    "/stop": CommandHelp(
        syntax="/stop",
        summary='stop current main work',
        section='Work',
        details="""**/stop**

Stop the current session's main turn, discard queued prompts and cancel its pending interaction.

Detached tasks keep running; inspect or stop them with `/tasks`. Other sessions are unaffected.""",
    ),
    "/reload": CommandHelp(
        syntax="/reload",
        summary='refresh Kimi configuration',
        section='Maintenance',
        details="""**/reload**

Refresh Kimi configuration, plugins, skills and agent definitions while the session is idle.

The session keeps its history, selected model and bridge permission mode. New sessions use the refreshed default model.""",
    ),
    "/btw": CommandHelp(
        syntax="/btw <question>",
        summary='read-only side question',
        section='Work',
        details="""**/btw <question>**

Ask a read-only side question while main work continues.

**Try**
`/btw What does data.txt contain?`

The answer is labelled Side question. Another `/btw` replaces it; switching or archiving the session closes it. Ordinary messages and `/stop` still address main work.""",
    ),
    "/agents": CommandHelp(
        syntax="/agents [show|background <n|id>]",
        summary='live agents and assignments',
        section='Work',
        details="""**/agents**

Inspect current main, child and nested-agent activity.

**Use**
- `/agents` — numbered agents, states, models and assignments.
- `/agents show <number>` — details and checklist.
- `/agents background <number>` — release a supported foreground wait.

Numbers use the latest list in this chat and session. Explicit IDs work too; use id 123 for a numeric ID. Swarm members cannot be detached individually.""",
    ),
    "/todos": CommandHelp(
        syntax="/todos [all]",
        summary='agent checklists',
        section='Work',
        details="""**/todos [all]**

**Use**
- `/todos` — main agent's checklist.
- `/todos all` — checklists grouped by agent.

✓ done · → in progress · ○ pending""",
    ),
    "/tasks background": CommandHelp(
        syntax="/tasks background <n|id>",
        summary="release a supported task’s foreground wait",
        section='Work',
        details="""**/tasks background <number|id>**

Release a supported task's foreground wait while its work continues.

**Try**
`/tasks` → `/tasks background 1`

The result distinguishes backgrounded, already backgrounded, ended and unsupported work. Inspect output with `/tasks show`; swarm members cannot be detached individually.""",
    ),
    "/tasks": CommandHelp(
        syntax="/tasks [all|completed|failed|cancelled]",
        summary='inspect or stop tasks',
        section='Work',
        details="""**/tasks**

Find a task, then inspect or control it by number.

**Use**
- `/tasks` — running tasks.
- `/tasks all` — every currently discoverable state.
- `/tasks completed`, `/tasks failed`, `/tasks cancelled` — filter.
- `/tasks show <number>` — output and details.
- `/tasks background <number>` — release its foreground wait.
- `/tasks cancel <number>` — stop it.

Numbers stay fixed until another task list, session change or bridge restart. Discovery follows Kimi's running activity, not a durable task archive.""",
    ),
    "/tasks show": CommandHelp(
        syntax="/tasks show <n|id>",
        summary="inspect a task with an 8 KiB output tail",
        section='Work',
        details="""**/tasks show <number|id>**

Inspect a task and up to the last 8 KiB of output.

**Try**
`/tasks` → `/tasks show 2`

Explicit IDs work without a listing. Use `/tasks show id 123` for a numeric ID.""",
    ),
    "/tasks cancel": CommandHelp(
        syntax="/tasks cancel <n|id>",
        summary="cancel a task",
        section='Work',
        details="""**/tasks cancel <number|id>**

Stop one task from the latest `/tasks` list.

**Try**
`/tasks` → `/tasks cancel 2`

Explicit IDs work without a listing. Use `/tasks cancel id 123` for a numeric ID.""",
    ),
    "/skills": CommandHelp(
        syntax="/skills",
        summary='available skills',
        section='Tools',
        details="""**/skills**

List available skills by exact name.

**Next**
`/skills run <name> [args]`""",
    ),
    "/skills run": CommandHelp(
        syntax="/skills run <name> [args]",
        summary="activate an exact skill",
        section='Tools',
        details="""**/skills run <name> [args]**

Activate a skill in the current session.

**Try**
`/skills` → `/skills run <name> your instructions`

Use an exact skill name from the list.""",
    ),
    "/plugins": CommandHelp(
        syntax="/plugins [show <name>]",
        summary='installed plugins',
        section='Tools',
        details="""**/plugins**

Inspect plugins already installed in Kimi.

**Use**
- `/plugins` — enabled, disabled and error states.
- `/plugins show <name>` — skills, MCP contributions and available tools.

Manage installation and configuration in Kimi. Detailed diagnostics stay there.""",
    ),
    "/mcp": CommandHelp(
        syntax="/mcp [servers|show <server>|restart <server>]",
        summary='tools and server connections',
        section='Tools',
        details="""**/mcp**

Inspect tools and connections, or restart a server.

**Use**
- `/mcp` — tools available to this session.
- `/mcp servers` — connection and authentication states.
- `/mcp show <server>` — one server's details and tools.
- `/mcp restart <server>` — request a reconnect, then inspect again.

**Scope**
Native connection inspection/restart uses the most recently created session, only if live. Tools and configured authentication use your current session. Manage authentication and configuration in Kimi.""",
    ),
    "/environments": CommandHelp(
        syntax="/environments",
        summary='available execution targets',
        section='Workspaces & files',
        details="""**/environments**

List local, SSH, Docker and command targets already configured in Kimi.

**Use one**
`/new /host/project --env <id> --cwd /target/project`

The workspace belongs to the host; file operations use the selected target. Configure targets and credentials in Kimi.""",
    ),
    "/workspace": CommandHelp(
        syntax="/workspace [add|remove|primary <path>]",
        summary='current and shared folders',
        section='Workspaces & files',
        details="""**/workspace**

See the current folder and shared folders.

**Manage folders**
- `/workspace add <path>` — share another folder.
- `/workspace remove <path>` — unshare an additional folder; keep its files.
- `/workspace primary <path>` — promote a folder; keep the old primary shared.

**Work in another project**
`/new /path/to/project`, or `/sessions search <keyword>` then `/switch <number>`.

Folder changes apply to sessions sharing this workspace. Existing sessions keep their working directories. Paths can contain spaces.""",
    ),
    "/workspace add": CommandHelp(
        syntax="/workspace add <path>",
        summary="share another folder",
        section="Workspaces & files",
        details="""**/workspace add <path>**

Share an existing folder with sessions using this workspace.

**Try**
`/workspace add /path/to/data`

Relative paths start at the workspace primary folder. Paths with spaces need no quotes. The current working directory stays the same.""",
    ),
    "/workspace remove": CommandHelp(
        syntax="/workspace remove <path>",
        summary="unshare an additional folder",
        section="Workspaces & files",
        details="""**/workspace remove <path>**

Remove an additional folder from the shared set. Its files stay on disk.

An existing session keeps access to its own original working directory even if that folder is removed. To change the primary folder, use `/workspace primary <path>`.""",
    ),
    "/workspace primary": CommandHelp(
        syntax="/workspace primary <path>",
        summary="choose the shared primary folder",
        section="Workspaces & files",
        details="""**/workspace primary <path>**

Promote a shared folder to primary. The old primary becomes an additional folder.

**Current session**
Its working directory stays the same. To work in another project, use `/new /path/to/project` or switch to an existing session there.""",
    ),
    "/files": CommandHelp(
        syntax="/files [directory] | /files search <query> | /files show|send|diff <n|path> | /files changes",
        summary='browse, preview and send files',
        section='Workspaces & files',
        details="""**/files**

Browse a folder, then preview or send a file.

**Use**
- `/files [directory]` — list files and folders.
- `/files search <query>` — search filenames.
- `/files show <number|path>` — preview supported text/source formats.
- `/files send <number|path>` — send any file format.
- `/files changes` — recorded changes from the latest finished turn.
- `/files diff <number|path>` — inspect a recorded change.

Folders end in /; links are marked [symlink]. Numbers use the latest file list in this chat/session and survive ordinary turns.

**More**
`/files show ?` · `/files send ?` · `/files diff ?`""",
    ),
    "/files show": CommandHelp(
        syntax="/files show <number|path>",
        summary="preview a text file",
        section="Workspaces & files",
        details="""**/files show <number|path>**

Preview up to 8 KiB of a supported text/source file.

**Try**
- `/files` → `/files show 2`
- `/files show notes with spaces.txt`

Other formats and binary content get a download hint. For a folder, use `/files <directory>`.""",
    ),
    "/files send": CommandHelp(
        syntax="/files send <number|path>",
        summary="send any file format",
        section="Workspaces & files",
        details="""**/files send <number|path>**

Send a file as native media or a generic attachment. Every file extension is accepted.

**Try**
- `/files` → `/files send 2`
- `/files send ./123` — a file whose name is a number.
- `/files send notes with spaces.txt`

Folders must be listed with `/files <directory>`. Kimi checks workspace and symlink access before sending.""",
    ),
    "/files search": CommandHelp(
        syntax="/files search <query>",
        summary="search filenames",
        section="Workspaces & files",
        details="""**/files search <query>**

Find up to 50 matching filenames under Kimi's ignore rules.

**Try**
`/files search config` → `/files show 1`

Results replace the current file numbers. To list a directory named search, use `/files ./search`.""",
    ),
    "/files changes": CommandHelp(
        syntax="/files changes",
        summary="list recorded file changes",
        section="Workspaces & files",
        details="""**/files changes**

List recorded changes from the latest finished main turn, including added and deleted files.

**Next**
`/files diff 1`

The list replaces your file numbers and keeps that turn's reference. While a new turn runs, the latest finished turn is still used. Missing recordings are reported separately from no changes.""",
    ),
    "/files diff": CommandHelp(
        syntax="/files diff <number|path>",
        summary="preview a recorded change",
        section="Workspaces & files",
        details="""**/files diff <number|path>**

Compare Kimi's recorded before/after content, including deleted files.

**Try**
`/files changes` → `/files diff 1`

A displayed number keeps its recorded turn; an explicit path uses the latest finished turn. Binary, oversize or unavailable recordings are labelled. Preview output is bounded; this is not a whole working-tree Git diff.""",
    ),
    "/render-thinking": CommandHelp(
        syntax="/render-thinking [on|off]",
        summary='separate thinking output',
        section='Settings',
        details="""**/render-thinking [on|off]**

Show the model's thinking separately from its answer.

**Use**
- `/render-thinking` — see the current setting.
- `/render-thinking on` or `/render-thinking off` — choose explicitly.

Turning it on backfills current thinking; turning it off freezes the visible thinking. QQ and WeChat keep this off.""",
    ),
    "/help": CommandHelp(
        syntax="/help",
        summary="show this help",
        section='General',
        details="""**/help**

Browse commands by task.

**Get examples**
`/files ?` · `/workspace ?` · `/goal edit ?`

Add ? to a command to see its focused help.""",
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
            (
                f"{command} {' '.join(argument[: -len(suffix)].split())}" in COMMAND_HELP
                if command in {"/files", "/workspace"}
                else _has_sub_forms(command)
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
    groups: dict[str, list[CommandHelp]] = {}
    for key, entry in COMMAND_HELP.items():
        if " " not in key and key != "/help":
            groups.setdefault(entry.section, []).append(entry)
    lines = ["**Commands**", "Send `/files ?` for options and examples."]
    for section in ("Sessions", "Workspaces & files", "Work", "Settings", "Tools", "Maintenance"):
        lines.append(f"\n**{section}**")
        lines.extend(
            f"- **{entry.syntax.split(' ', 1)[0]}** · {entry.summary}"
            for entry in groups[section]
        )
    return "\n".join(lines)
