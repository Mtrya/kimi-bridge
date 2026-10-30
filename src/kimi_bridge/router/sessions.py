"""Conversation binding and managed session-stream lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

from ..kimi_server import (
    InteractionSnapshot,
    InteractionUpdate,
    KimiServerError,
    KimiServerOperationError,
    KimiServerProtocolError,
    PermissionMode,
    SessionArchiveUncertain,
    SessionStatus,
)
from ..platforms.base import (
    ActorRef,
    ConversationRef,
    PlatformAdapter,
)
from ..state import (
    PERMISSION_MODES,
    ConversationBinding,
    DeliveryRoute,
)
from .files import _expand_fullwidth_tilde
from .formatting import _session_recency_key
from .models import _ActiveStream, _CompactionOutcome


LOGGER = logging.getLogger(__name__)
DEFAULT_PERMISSION_MODE = "manual"


class _SessionMixin:
    def _set_binding(
        self, conversation_key: str, binding: ConversationBinding
    ) -> None:
        current = self._state.bindings.get(conversation_key)
        if current is None or current.session_id != binding.session_id:
            for key in tuple(self._task_choices):
                if key[0] == conversation_key:
                    del self._task_choices[key]
        self._state.bindings[conversation_key] = binding
        self._state_store.save(self._state)

    def _remember_delivery(
        self,
        conversation_key: str,
        conversation: ConversationRef,
        actor: ActorRef,
    ) -> None:
        binding = self._state.bindings.get(conversation_key)
        if binding is None:
            return
        delivery = DeliveryRoute(
            platform=conversation.platform,
            bot_id=conversation.bot_id,
            conversation_id=conversation.conversation_id,
            actor_id=actor.id,
            actor_aliases=actor.aliases,
        )
        if binding.delivery == delivery:
            return
        self._set_binding(conversation_key, replace(binding, delivery=delivery))

    def _remember_active_conversation(
        self, conversation_key: str | None
    ) -> None:
        if self._state.active_conversation == conversation_key:
            return
        self._state.active_conversation = conversation_key
        self._state_store.save(self._state)

    def _default_permission_mode(self, adapter: PlatformAdapter) -> str:
        return DEFAULT_PERMISSION_MODE if adapter.supports_interactions else "auto"

    def _coerce_binding_capabilities(
        self, conversation_key: str, adapter: PlatformAdapter
    ) -> None:
        """Force interaction-less adapters into auto mode and hide thinking output."""

        if adapter.supports_interactions:
            return
        binding = self._state.bindings.get(conversation_key)
        if binding is None or (
            binding.permission_mode == "auto" and not binding.render_thinking
        ):
            return
        self._set_binding(
            conversation_key,
            replace(
                binding,
                permission_mode="auto",
                render_thinking=False,
            ),
        )

    async def _require_binding(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
    ) -> ConversationBinding | None:
        binding = self._state.bindings.get(conversation_key)
        if binding is None:
            await self._send_chunked(adapter, conversation, "No bound session.")
        return binding

    async def _require_idle(
        self,
        binding: ConversationBinding,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        operation: str,
    ) -> SessionStatus | None:
        status = await self._client.get_session_status(binding.session_id)
        if status.busy:
            await self._send_chunked(
                adapter,
                conversation,
                f"Session is busy. {operation} can only run while idle.",
            )
            return None
        return status

    async def _archive_session(self, session_id: str) -> None:
        active = self._active
        if active is not None and active.session_id == session_id:
            # Archive emits terminal events while disposing the runtime. Stop
            # snapshot reads first so they cannot materialize it again.
            await self._stop_active_stream(preserve_compaction_waiter=True)
        else:
            active = None
        async with self._interaction_lock:
            try:
                await self._client.archive_session(session_id)
            except SessionArchiveUncertain:
                self._verified_session_profiles.discard(session_id)
                self._fail_compaction_waiter(
                    session_id, KimiServerError("Archive outcome is unknown")
                )
                raise
            except Exception as archive_error:
                if active is not None:
                    try:
                        await self._ensure_active_stream(
                            active.conversation_key,
                            session_id,
                            active.adapter,
                            active.conversation,
                            active.actor,
                        )
                        waiter = self._compaction_waiters.get(session_id)
                        if waiter is not None and not waiter.future.done():
                            status = await self._client.get_session_status(session_id)
                            if not status.busy:
                                waiter.future.set_result(_CompactionOutcome("unavailable"))
                    except Exception as restore_error:
                        self._fail_compaction_waiter(session_id, KimiServerError(str(restore_error)))
                        raise KimiServerOperationError(
                            f"{archive_error}; event delivery could not resume: {restore_error}"
                        ) from archive_error
                raise
            await self._detach_archived_session(session_id)

    async def _detach_archived_session(self, session_id: str) -> None:
        """Drop bridge references only after Kimi confirms the archive."""

        for key, binding in tuple(self._state.bindings.items()):
            if binding.session_id == session_id:
                del self._state.bindings[key]
        if self._state.active_conversation not in self._state.bindings:
            self._state.active_conversation = None
        self._verified_session_profiles.discard(session_id)
        # Invalidate the whole listing so remembered numeric choices never shift.
        for key, choices in tuple(self._session_choices.items()):
            if any(str(session["id"]) == session_id for session in choices):
                del self._session_choices[key]
        for key, (bound_session, _choices) in tuple(self._task_choices.items()):
            if bound_session == session_id:
                del self._task_choices[key]
        self._fail_compaction_waiter(
            session_id, KimiServerError("Session was archived")
        )
        if self._active is not None and self._active.session_id == session_id:
            await self._stop_active_stream()
        await self._finish_archived_interactions(session_id)
        self._state_store.save(self._state)

    async def _list_recent_sessions(self) -> list[dict[str, Any]]:
        idle, busy = await asyncio.gather(
            self._client.list_sessions(busy=False, page_size=self._session_list_limit),
            self._client.list_sessions(busy=True, page_size=self._session_list_limit),
        )
        by_id = {str(session["id"]): session for session in [*idle, *busy]}
        sessions = list(by_id.values())
        sessions.sort(key=_session_recency_key, reverse=True)
        return sessions[: self._session_list_limit]

    async def _list_all_sessions(self) -> list[dict[str, Any]]:
        """Full session set across both busy states, for discovery.

        Pagination uses the client's internal page size, independent of
        the display-facing ``session_list_limit``. Results are merged by
        session ID: a session that flips busy state between the two
        requests would otherwise appear twice.
        """
        idle, busy = await asyncio.gather(
            self._client.list_all_sessions(busy=False),
            self._client.list_all_sessions(busy=True),
        )
        by_id = {str(session["id"]): session for session in [*idle, *busy]}
        return list(by_id.values())

    async def _search_sessions(self, keyword: str) -> list[dict[str, Any]]:
        """Case-insensitive substring match on title and workspace path."""
        needle = keyword.casefold()
        matches = [
            session
            for session in await self._list_all_sessions()
            if needle in str(session.get("title") or "").casefold()
            or needle in str(session.get("metadata", {}).get("cwd", "")).casefold()
        ]
        matches.sort(key=_session_recency_key, reverse=True)
        return matches[: self._session_list_limit]

    async def _match_sessions_by_title(self, title: str) -> list[dict[str, Any]]:
        """Exact case-insensitive title matches, most recent first.

        Not truncated here: callers must see every match to tell a
        unique title from an ambiguous one before applying display caps.
        """
        needle = title.casefold()
        matches = [
            session
            for session in await self._list_all_sessions()
            if str(session.get("title") or "").casefold() == needle
        ]
        matches.sort(key=_session_recency_key, reverse=True)
        return matches

    async def _resolve_new_workspace(self, argument: str) -> Path:
        if not argument:
            self._default_workspace.mkdir(parents=True, exist_ok=True)
            return self._default_workspace
        workspace = _expand_fullwidth_tilde(
            Path(argument).expanduser(), argument
        ).resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace}")
        return workspace

    async def _create_and_bind(
        self,
        conversation_key: str,
        workspace: Path,
        title: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        actor: ActorRef,
    ) -> ConversationBinding:
        permission_mode = self._default_permission_mode(adapter)
        model = await self._client.get_default_model()
        session_id = await self._client.create_session(
            str(workspace),
            title=title,
            model=model,
            permission_mode=permission_mode,
        )
        await self._verify_session_profile(
            session_id,
            permission_mode,
            replace_existing=True,
            expected_model=model,
        )
        current = self._state.bindings.get(conversation_key)
        binding = ConversationBinding(
            session_id=session_id,
            workspace=str(workspace),
            permission_mode=permission_mode,
            render_thinking=(
                current is not None
                and current.render_thinking
                and adapter.supports_interactions
            ),
            delivery=DeliveryRoute(
                platform=conversation.platform,
                bot_id=conversation.bot_id,
                conversation_id=conversation.conversation_id,
                actor_id=actor.id,
                actor_aliases=actor.aliases,
            ),
        )
        self._set_binding(conversation_key, binding)
        return binding

    async def _verify_session_profile(
        self,
        session_id: str,
        permission_mode: str,
        *,
        replace_existing: bool,
        expected_model: str | None = None,
    ) -> None:
        if session_id in self._verified_session_profiles:
            return
        status = await self._client.get_session_status(session_id)
        model = expected_model or self._model
        update_model = status.model is None or (
            replace_existing and status.model != model
        )
        update_permission = status.permission_mode != permission_mode
        if update_model or update_permission:
            await self._client.update_profile(
                session_id,
                model=model if update_model else None,
                permission_mode=cast(PermissionMode, permission_mode),
            )
            status = await self._client.get_session_status(session_id)
            if update_model and status.model != model:
                raise KimiServerProtocolError(
                    f"kimi session {session_id} did not bind model {model!r}"
                )
            if status.permission_mode != permission_mode:
                raise KimiServerProtocolError(
                    f"kimi session {session_id} did not bind permission mode "
                    f"{permission_mode!r}"
                )
        self._verified_session_profiles.add(session_id)

    def _binding_from_session(
        self,
        session: dict[str, Any],
        *,
        adapter: PlatformAdapter,
        render_thinking: bool,
        conversation: ConversationRef,
        actor: ActorRef,
    ) -> ConversationBinding:
        workspace = str(session["metadata"]["cwd"])
        agent_config = session.get("agent_config")
        mode = (
            agent_config.get("permission_mode")
            if isinstance(agent_config, dict)
            else None
        )
        if mode not in PERMISSION_MODES:
            mode = DEFAULT_PERMISSION_MODE
        if not adapter.supports_interactions:
            mode = "auto"
            render_thinking = False
        binding = ConversationBinding(
            session_id=str(session["id"]),
            workspace=workspace,
            permission_mode=mode,
            render_thinking=render_thinking,
            delivery=DeliveryRoute(
                platform=conversation.platform,
                bot_id=conversation.bot_id,
                conversation_id=conversation.conversation_id,
                actor_id=actor.id,
                actor_aliases=actor.aliases,
            ),
        )
        return binding

    async def _resolve_session(
        self, conversation_key: str, selector: str
    ) -> dict[str, Any] | None:
        if selector.isdecimal():
            index = int(selector) - 1
            choices = self._session_choices.get(conversation_key, [])
            if 0 <= index < len(choices):
                return choices[index]
            return None
        return await self._client.find_session(selector)

    async def _ensure_active_stream(
        self,
        conversation_key: str,
        session_id: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        actor: ActorRef,
        permission_mode: str | None = None,
    ) -> None:
        if permission_mode is None:
            binding = self._state.bindings.get(conversation_key)
            permission_mode = (
                binding.permission_mode
                if binding is not None and binding.session_id == session_id
                else DEFAULT_PERMISSION_MODE
            )
        await self._verify_session_profile(
            session_id,
            permission_mode,
            replace_existing=False,
        )
        active = self._active
        if active is not None and active.task is not None and not active.task.done():
            if active.conversation_key != conversation_key:
                raise KimiServerOperationError(
                    "another conversation is still receiving a response; "
                    "retry after it finishes"
                )
            if active.session_id == session_id:
                active.adapter = adapter
                active.conversation = conversation
                active.actor = actor
                self._remember_active_conversation(conversation_key)
                return

        await self._stop_active_stream()
        active = _ActiveStream(
            conversation_key=conversation_key,
            session_id=session_id,
            adapter=adapter,
            conversation=conversation,
            actor=actor,
        )
        active.task = asyncio.create_task(
            self._consume_events(active),
            name=f"kimi-events-{session_id}",
        )
        stream_task = active.task
        self._active = active
        subscription_wait = asyncio.create_task(
            self._client.wait_until_subscribed(session_id),
            name=f"kimi-subscription-ready-{session_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {stream_task, subscription_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stream_task in done:
                active.task = None
                stream_task.result()
                raise KimiServerOperationError("kimi event stream ended before subscribing")
            await subscription_wait
            self._remember_active_conversation(conversation_key)
        except BaseException:
            if not subscription_wait.done():
                subscription_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await subscription_wait
            await self._stop_active_stream()
            raise
        stream_task.add_done_callback(self._stream_done)

    async def restore(self, adapter: PlatformAdapter) -> None:
        """Restore event delivery for the conversation active before restart."""

        async with self._inbound_lock:
            await self._restore_active_stream(adapter)

    async def _restore_active_stream(self, adapter: PlatformAdapter) -> None:
        conversation_key = self._state.active_conversation
        binding = (
            self._state.bindings.get(conversation_key)
            if conversation_key is not None
            else None
        )
        if binding is None or binding.delivery is None:
            return
        assert conversation_key is not None
        delivery = binding.delivery
        conversation = ConversationRef(
            delivery.platform, delivery.bot_id, delivery.conversation_id
        )
        actor = ActorRef(delivery.actor_id, aliases=delivery.actor_aliases)
        if not adapter.restore_conversation(conversation, actor):
            LOGGER.warning(
                "saved conversation %s is not allowed by the current adapter",
                conversation_key,
            )
            return
        self._coerce_binding_capabilities(conversation_key, adapter)
        await self._restore_bound_stream(
            conversation_key, adapter, conversation, actor
        )

    async def _restore_bound_stream(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        actor: ActorRef,
    ) -> None:
        binding = self._state.bindings.get(conversation_key)
        if binding is None or binding.delivery is None:
            return
        active = self._active
        if (
            active is not None
            and active.task is not None
            and not active.task.done()
            and active.conversation_key != conversation_key
        ):
            return
        try:
            if await self._client.is_session_archived(binding.session_id):
                async with self._interaction_lock:
                    await self._detach_archived_session(binding.session_id)
                return
            await self._ensure_active_stream(
                conversation_key,
                binding.session_id,
                adapter,
                conversation,
                actor,
            )
        except (KimiServerError, TimeoutError):
            LOGGER.warning(
                "could not restore event delivery for %s",
                conversation_key,
                exc_info=True,
            )

    async def _stop_active_stream(self, *, preserve_compaction_waiter: bool = False) -> None:
        active = self._active
        self._active = None
        if active is None:
            return
        active.preserve_compaction_waiter = preserve_compaction_waiter
        await self._cancel_delayed_flush(active.render)
        await self._cancel_delayed_flush(active.thinking)
        if active.interaction_retry_task is not None:
            active.interaction_retry_task.cancel()
            await asyncio.gather(active.interaction_retry_task, return_exceptions=True)
        if active.failure_notice_task is not None:
            active.failure_notice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active.failure_notice_task
        if active.task is not None:
            stream_task = active.task
            active.task = None
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)

    async def _consume_events(self, active: _ActiveStream) -> None:
        failure: KimiServerError | None = None
        try:
            async for event in self._client.subscribe_events(active.session_id):
                if self._active is not active:
                    return
                if isinstance(event, (InteractionUpdate, InteractionSnapshot)):
                    await self._handle_interaction_event(active, event)
                else:
                    await self.dispatch_event(active.conversation_key, event)
        except asyncio.CancelledError:
            failure = KimiServerError("kimi event stream stopped")
            raise
        except Exception as exc:
            failure = KimiServerError(f"kimi event stream failed: {exc}")
            raise
        else:
            failure = KimiServerError("kimi event stream ended")
        finally:
            if failure is None:
                failure = KimiServerError("kimi event stream stopped")
            if not active.preserve_compaction_waiter:
                self._fail_compaction_waiter(active.session_id, failure)

    def _dispatch_compaction_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        waiter = self._compaction_waiters.get(session_id)
        if waiter is None or waiter.future.done():
            return
        if event_type == "compaction.started":
            trigger = payload.get("trigger")
            if trigger in {"manual", "auto"}:
                waiter.active_trigger = cast(Literal["manual", "auto"], trigger)
            return
        if event_type not in {
            "compaction.completed",
            "compaction.blocked",
            "compaction.cancelled",
        }:
            return
        if waiter.active_trigger == "auto":
            waiter.active_trigger = None
            return
        if waiter.active_trigger != "manual":
            return

        if event_type == "compaction.completed":
            result = payload.get("result")
            if not isinstance(result, dict):
                waiter.future.set_exception(
                    KimiServerProtocolError("compaction.completed event has no result")
                )
                return
            try:
                outcome = _CompactionOutcome(
                    state="completed",
                    compacted_count=int(result["compactedCount"]),
                    tokens_before=int(result["tokensBefore"]),
                    tokens_after=int(result["tokensAfter"]),
                )
            except (KeyError, TypeError, ValueError):
                waiter.future.set_exception(
                    KimiServerProtocolError(
                        "compaction.completed event has invalid metrics"
                    )
                )
                return
        elif event_type == "compaction.blocked":
            outcome = _CompactionOutcome(state="blocked")
        else:
            outcome = _CompactionOutcome(state="cancelled")
        waiter.future.set_result(outcome)

    def _fail_compaction_waiter(self, session_id: str, error: KimiServerError) -> None:
        waiter = self._compaction_waiters.get(session_id)
        if waiter is not None and not waiter.future.done():
            waiter.future.set_exception(error)

    def _fail_all_compaction_waiters(self, error: KimiServerError) -> None:
        for session_id in tuple(self._compaction_waiters):
            self._fail_compaction_waiter(session_id, error)

    def _stream_done(self, task: asyncio.Task[None]) -> None:
        active = self._active
        if active is not None and active.task is task:
            active.task = None
        else:
            active = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("kimi event stream stopped unexpectedly")
            if active is not None:
                active.failure_notice_task = asyncio.create_task(
                    self._report_stream_failure(active),
                    name=f"kimi-stream-failure-notice-{active.session_id}",
                )
                active.failure_notice_task.add_done_callback(
                    self._stream_failure_notice_done
                )

    async def _report_stream_failure(self, active: _ActiveStream) -> None:
        await self._send_chunked(
            active.adapter,
            active.conversation,
            "Kimi event delivery stopped unexpectedly. Check the bridge logs and "
            "send a regular message to retry.",
        )

    def _stream_failure_notice_done(self, task: asyncio.Task[None]) -> None:
        active = self._active
        if active is not None and active.failure_notice_task is task:
            active.failure_notice_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("failed to report stopped kimi event stream")
