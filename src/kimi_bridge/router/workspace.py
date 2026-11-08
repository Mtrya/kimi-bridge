"""Workspace commands and one current file selection per chat/session."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..kimi_server import WorkspaceFile
from ..platforms.base import ConversationRef, OutboundFile, PlatformAdapter


@dataclass(frozen=True, slots=True)
class _FileChoice:
    path: str


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
        if verb in {"show", "send"}:
            selected = rest.strip()
            if not selected:
                await self._send_chunked(
                    adapter, conversation, f"Usage: /files {verb} <n|path>"
                )
                return
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
                selected = choices[2][int(selected) - 1].path
            if verb == "show":
                preview = await self._client.preview_workspace_file(
                    binding.session_id, selected
                )
                if preview.binary:
                    text = f"{selected} · binary · {preview.size} bytes\nUse /files send to download it."
                else:
                    body = preview.text or ""
                    fence = chr(96) * max(
                        3,
                        max(
                            (len(run) + 1 for run in re.findall(chr(96) + "+", body)),
                            default=3,
                        ),
                    )
                    text = (
                        f"{selected} · {preview.size} bytes\n{fence}\n{body}\n{fence}"
                    )
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
