"""Registry for the single warm Claude conversation actor."""

import asyncio
from collections.abc import Callable
from time import monotonic

from claude_agent_sdk import ClaudeAgentOptions

from app.actor import ActorBusyError, ConvActor


IDLE_TTL_SECONDS = 900
REAPER_INTERVAL_SECONDS = 15


class ConvRegistry:
    def __init__(self, project_dir: str) -> None:
        self.project_dir = project_dir
        self._actor: ConvActor | None = None
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(
                self._reaper(),
                name="claude-actor-reaper",
            )

    async def stop(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None
        await self.invalidate()

    async def submit(
        self,
        conv_id: str,
        prompt: str,
        options: ClaudeAgentOptions,
        fingerprint: str,
        timing_callback: Callable[[str], None] | None,
    ) -> asyncio.Queue:
        async with self._lock:
            actor = self._actor
            if actor is not None and not actor.alive:
                self._actor = None
                actor = None
            if actor is not None and actor.conv_id != conv_id:
                if actor.busy:
                    raise ActorBusyError("另一段会话仍在回复")
                await actor.close()
                self._actor = None
                actor = None
            if actor is None:
                actor = ConvActor(conv_id, self.project_dir)
                self._actor = actor
            stage = (
                "actor_warm_hit"
                if actor.is_warm_for(fingerprint)
                else "actor_cold_start"
            )
            outbox = await actor.submit(
                prompt,
                options,
                fingerprint,
                timing_callback,
            )
            if timing_callback:
                timing_callback(stage)
            return outbox

    async def assert_available(self) -> None:
        async with self._lock:
            actor = self._actor
            if actor is not None and actor.alive and actor.busy:
                raise ActorBusyError("上一条消息仍在回复")

    async def invalidate(self, conv_id: str | None = None) -> None:
        async with self._lock:
            actor = self._actor
            if actor is None or (conv_id is not None and actor.conv_id != conv_id):
                return
            await actor.close()
            if self._actor is actor:
                self._actor = None

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(REAPER_INTERVAL_SECONDS)
            async with self._lock:
                actor = self._actor
                if (
                    actor is not None
                    and not actor.busy
                    and monotonic() - actor.last_active >= IDLE_TTL_SECONDS
                ):
                    await actor.close()
                    if self._actor is actor:
                        self._actor = None


registry: ConvRegistry | None = None


def configure_registry(project_dir: str) -> ConvRegistry:
    global registry
    if registry is None:
        registry = ConvRegistry(project_dir)
    return registry


def get_registry() -> ConvRegistry:
    if registry is None:
        raise RuntimeError("Claude registry 尚未启动")
    return registry
