"""Workspace commands and one current file selection per chat/session."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..kimi_server import RecordedTurn, WorkspaceFile
from ..platforms.base import ConversationRef, OutboundFile, PlatformAdapter


@dataclass(frozen=True, slots=True)
class _FileChoice:
    path: str
    turn: RecordedTurn | None = None


class _WorkspaceMixin:
    async def _handle_workspace(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        if argument:
            action, _, path = argument.partition(" ")
            if action not in {"add", "remove", "primary"} or not path.strip():
                await self._send_chunked(
                    adapter,
                    conversation,
                    "Usage: /workspace [add|remove|primary <path>]",
                )
                return
            info = await self._client.change_workspace(
                binding.session_id, action, path.strip()
            )
            for choice_key, (_, current, _) in tuple(self._file_choices.items()):
                if current.workspace.id == info.id:
                    self._file_choices.pop(choice_key)
        context = await self._client.get_file_context(binding.session_id)
        info = context.workspace
        lines = [
            f"Workspace {info.id}",
            f"Primary: {info.primary}",
            "Additional folders:",
            *(info.additional or ("None",)),
            f"Session working directory: {context.cwd}",
        ]
        if argument:
            lines.append(
                "The shared folder set applies to every session using this workspace. Existing sessions keep their working directories."
            )
        await self._send_chunked(adapter, conversation, "\n".join(lines))

    async def _handle_files(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        context = await self._client.get_file_context(binding.session_id)
        key = (conversation_key, conversation)
        verb, _, rest = argument.partition(" ")
        if verb == "changes":
            if rest.strip():
                await self._send_chunked(adapter, conversation, "Usage: /files changes")
                return
            self._file_choices.pop(key, None)
            changes = await self._client.get_file_changes(binding.session_id)
            if changes.turn is None:
                text = "No completed turn is available."
            elif not changes.recorded:
                text = "File history was not recorded for the latest completed turn."
            else:
                lines = [f"Recorded changes · turn {changes.turn.number}"]
                for number, change in enumerate(changes.changes, 1):
                    flags = (
                        " · binary"
                        if change.binary
                        else " · oversize"
                        if change.oversize
                        else ""
                    )
                    lines.append(
                        f"{number}. {change.path} · {change.status} · +{change.additions}/−{change.deletions}{flags}"
                    )
                text = (
                    "\n".join(lines)
                    if changes.changes
                    else "No recorded file changes in the latest completed turn."
                )
            await self._send_chunked(adapter, conversation, text)
            if changes.recorded:
                self._file_choices[key] = (
                    binding.session_id,
                    context,
                    tuple(
                        _FileChoice(change.path, changes.turn)
                        for change in changes.changes
                    ),
                )
            return
        if verb in {"show", "send", "diff"}:
            selected = rest.strip()
            if not selected:
                await self._send_chunked(
                    adapter, conversation, f"Usage: /files {verb} <n|path>"
                )
                return
            selected_turn = None
            if selected.isdecimal():
                choices = self._file_choices.get(key)
                if (
                    choices is None
                    or choices[0] != binding.session_id
                    or choices[1] != context
                    or len(selected) > len(str(len(choices[2])))
                    or not 1 <= int(selected) <= len(choices[2])
                ):
                    await self._send_chunked(
                        adapter,
                        conversation,
                        "File number unavailable. Run /files again, or use an explicit path such as ./123.",
                    )
                    return
                choice = choices[2][int(selected) - 1]
                selected, selected_turn = choice.path, choice.turn
            if verb == "diff":
                diff = await self._client.get_file_diff(
                    binding.session_id, selected, turn=selected_turn
                )
                text = (
                    f"Diff unavailable: {diff.unavailable}."
                    if diff.unavailable
                    else f"{selected}\n{_code_block(diff.text or 'No textual difference.')}"
                )
                if diff.truncated:
                    text += "\nDiff truncated."
                await self._send_chunked(adapter, conversation, text)
            elif verb == "show":
                preview = await self._client.preview_workspace_file(
                    binding.session_id, selected
                )
                if preview.binary:
                    text = f"{selected} · binary · {preview.size} bytes\nUse /files send to download it."
                else:
                    body = preview.text or ""
                    text = f"{selected} · {preview.size} bytes\n{_code_block(body)}"
                    if preview.truncated:
                        text += "\nPreview truncated."
                await self._send_chunked(adapter, conversation, text)
            else:
                download = await self._client.download_workspace_file(
                    binding.session_id, selected
                )
                try:
                    await adapter.send_file(
                        conversation,
                        OutboundFile(download.name, download.data, download.media_type),
                    )
                except Exception as exc:
                    await self._send_chunked(
                        adapter, conversation, f"File send failed: {exc}"
                    )
            return
        if verb == "search" and not rest.strip():
            await self._send_chunked(
                adapter, conversation, "Usage: /files search <query>"
            )
            return
        directory = rest.strip() if verb == "--" else (argument or ".")
        self._file_choices.pop(key, None)
        listing = await self._client.list_workspace_files(
            binding.session_id,
            directory,
            query=rest.strip() if verb == "search" else None,
        )
        await self._send_chunked(
            adapter,
            conversation,
            _file_listing_text(listing.entries, listing.truncated),
        )
        self._file_choices[key] = (
            binding.session_id,
            context,
            tuple(_FileChoice(entry.path) for entry in listing.entries),
        )


def _file_listing_text(entries: tuple[WorkspaceFile, ...], truncated: bool) -> str:
    lines = [f"Files · {len(entries)}"]
    lines.extend(
        f"{number}. {entry.path}{'/' if entry.kind == 'directory' else ''}"
        for number, entry in enumerate(entries, 1)
    )
    if truncated:
        lines.append("Listing truncated. Choose a narrower directory or search.")
    return "\n".join(lines)


def _code_block(text: str) -> str:
    fence = chr(96) * max(
        3, max((len(run) + 1 for run in re.findall(chr(96) + "+", text)), default=3)
    )
    return f"{fence}\n{text}\n{fence}"


def _parse_new_options(argument: str) -> tuple[str, str | None, str | None]:
    tokens = list(re.finditer(r'"[^"\n]*"|\'[^\'\n]*\'|\S+', argument))
    flags = [
        (match.start(), match.end(), match.group()[2:])
        for match in tokens
        if match.group() in {"--env", "--cwd"}
    ]
    if not flags:
        return argument, None, None

    def unquote(value: str) -> str:
        if len(value) >= 2 and value[0] in {"'", chr(34)} and value[-1] == value[0]:
            return value[1:-1]
        return value

    workspace = unquote(argument[: flags[0][0]].strip())
    options = {}
    for index, (_, end, flag) in enumerate(flags):
        value = unquote(
            argument[
                end : flags[index + 1][0] if index + 1 < len(flags) else len(argument)
            ].strip()
        )
        if not value or flag in options:
            raise ValueError(
                "Usage: /new [workspace] --env <id> [--cwd <environment-path>]"
            )
        options[flag] = value
    if "cwd" in options and "env" not in options:
        raise ValueError("--cwd requires --env")
    return workspace, options.get("env"), options.get("cwd")
