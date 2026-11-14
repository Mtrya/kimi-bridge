"""Native workspace membership and session-scoped file operations."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Any

from ..interactions import ApprovalRequest

from .types import (
    EnvironmentInfo,
    PlanDocument,
    FileContext,
    FileDownload,
    FileAccessError,
    FileListing,
    FilePreview,
    KimiServerOperationError,
    KimiServerAPIError,
    KimiServerProtocolError,
    WorkspaceFile,
    WorkspaceInfo,
)


def _workspace(value: dict[str, Any]) -> WorkspaceInfo:
    return WorkspaceInfo(value["id"], value["root"], tuple(value["additional_dirs"]))


class WorkspaceOperations:
    async def _file_request(self, operation: str, **kwargs: Any) -> Any:
        try:
            return await self._request_operation(operation, **kwargs)
        except KimiServerAPIError as exc:
            if exc.code == 40906:
                raise FileAccessError("directory") from exc
            if exc.code == 41304:
                raise FileAccessError("path_rejected") from exc
            raise

    async def list_environments(self, session_id: str) -> tuple[EnvironmentInfo, ...]:
        data = await self._request_operation(
            "session_environments", path_parameters={"session_id": session_id}
        )
        return tuple(
            EnvironmentInfo(value["environment_id"], value["type"], value["status"])
            for value in data["environments"]
        )

    async def get_plan_document(self, request: ApprovalRequest) -> PlanDocument | None:
        if request.tool_name != "ExitPlanMode" or request.tool_call_id is None:
            return None
        async for page in self._turn_pages(
            request.session_id, request.agent_id, page_size=200
        ):
            for turn in page["turns"]:
                for item in turn.get("items") or ():
                    if (
                        item.get("type") != "tool_call"
                        or item.get("item_id") != request.tool_call_id
                    ):
                        continue
                    display = item.get("display")
                    if not isinstance(display, dict):
                        return None
                    if display.get("kind") != "plan_review":
                        return None
                    plan = display.get("plan")
                    if not isinstance(plan, str):
                        raise KimiServerProtocolError(
                            "plan_review display must carry plan text"
                        )
                    name = (
                        (display.get("path") or "plan.md")
                        .replace("\\", "/")
                        .rsplit("/", 1)[-1]
                    )
                    return PlanDocument(name, plan)
        return None

    async def get_file_context(self, session_id: str) -> FileContext:
        session = await self.get_session(session_id)
        binding = await self._request_operation(
            "session_environment", path_parameters={"session_id": session_id}
        )
        workspaces = await self._request_operation("list_workspaces")
        info = next(
            (
                _workspace(value)
                for value in workspaces["items"]
                if value["id"] == binding["workspace_id"]
            ),
            None,
        )
        if info is None:
            # Kimi's list deduplicates implicit IDs by root after primary changes.
            # Its workspace-scoped session list resolves those aliases natively.
            for value in workspaces["items"]:
                members = await self.list_all_sessions(
                    workspace_id=value["id"], include_archive=True, busy=None
                )
                if any(member["id"] == session_id for member in members):
                    info = _workspace(value)
                    break
        if info is None:
            raise KimiServerOperationError(
                "The session workspace is no longer registered"
            )
        cwd = binding.get("cwd")
        if binding["environment_id"] != "local" and not cwd:
            raise KimiServerProtocolError("remote environment binding omitted its working directory")
        return FileContext(
            info,
            cwd or session["metadata"]["cwd"],
            binding["environment_id"],
        )

    async def change_workspace(
        self, session_id: str, action: str, path: str
    ) -> WorkspaceInfo:
        context = await self.get_file_context(session_id)
        info = context.workspace
        path = _local_input(FileContext(info, info.primary), path)
        if action == "primary":
            return _workspace(
                await self._request_operation(
                    "workspace_primary",
                    path_parameters={"workspace_id": info.id},
                    json_body={"path": path},
                )
            )
        # Membership belongs to the bridge host workspace, independently of target cwd.
        requested = Path(path).expanduser()
        requested = (
            requested if requested.is_absolute() else Path(info.primary) / requested
        )
        normalized = os.path.normcase(str(requested.resolve()))
        if action == "add":
            directories = [*info.additional, str(requested)]
        elif action == "remove":
            if normalized not in {
                os.path.normcase(str(Path(value).resolve()))
                for value in info.additional
            }:
                raise KimiServerOperationError(
                    "Choose an additional workspace folder to remove"
                )
            directories = [
                value
                for value in info.additional
                if os.path.normcase(str(Path(value).resolve())) != normalized
            ]
        else:
            raise ValueError("unknown workspace action")
        return _workspace(
            await self._request_operation(
                "update_workspace",
                path_parameters={"workspace_id": info.id},
                json_body={"additional_dirs": directories},
            )
        )

    async def list_workspace_files(
        self, session_id: str, directory: str = ".", *, query: str | None = None
    ) -> FileListing:
        context = await self.get_file_context(session_id)
        directory = _local_input(context, directory)
        body = (
            {"query": query, "limit": 50}
            if query is not None
            else {
                "path": directory,
                "depth": 1,
                "limit": 100,
                "allow_external_symlinks": False,
            }
        )
        data = await self._file_request(
            "search_files" if query is not None else "list_files",
            path_parameters={"session_id": session_id},
            json_body=body,
        )
        entries = []
        absolute = directory.startswith("/") or PureWindowsPath(directory).is_absolute()
        separator = "\\" if "\\" in directory and "/" not in directory else "/"
        for value in data["items"]:
            path = value["path"]
            if query is None and absolute:
                path = directory.rstrip("/\\") + separator + value["name"]
            entries.append(
                WorkspaceFile(path, value["name"], value["kind"], value.get("size"))
            )
        return FileListing(tuple(entries), data["truncated"])

    async def preview_workspace_file(self, session_id: str, path: str) -> FilePreview:
        context = await self.get_file_context(session_id)
        data = await self._file_request(
            "read_file",
            path_parameters={"session_id": session_id},
            json_body={
                "path": _local_input(context, path),
                "length": 8192,
                "encoding": "auto",
                "allow_external_symlinks": False,
            },
        )
        if data["encoding"] not in {"utf-8", "base64"}:
            raise KimiServerProtocolError("unknown file content encoding")
        return FilePreview(
            data["path"],
            data["content"] if not data["is_binary"] else None,
            data["size"],
            data["is_binary"],
            data["truncated"],
        )

    async def download_workspace_file(self, session_id: str, path: str) -> FileDownload:
        context = await self.get_file_context(session_id)
        path = _local_input(context, path)
        response = await self._file_request(
            "download_file",
            path_parameters={"session_id": session_id, "path": path},
            raw_response=True,
        )
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        return FileDownload(
            name,
            response.content,
            response.headers.get("content-type", "application/octet-stream").split(
                ";", 1
            )[0],
        )


def _local_input(context: FileContext, argument: str) -> str:
    if context.environment_id != "local":
        return argument
    requested = Path(argument).expanduser()
    candidate = requested if requested.is_absolute() else Path(context.cwd) / requested
    if (
        argument.startswith("～")
        and (len(argument) == 1 or argument[1] in {os.sep, os.altsep})
        and not candidate.exists()
    ):
        return str(Path("~" + argument[1:]).expanduser())
    return str(requested)
