"""Declared service ordering for logical-time and live agent populations."""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator


class Scheduler:
    def __init__(self, mode: str, agents: list[str], seed: int, action_time: float = 1.0) -> None:
        self.mode = mode
        order = list(agents)
        random.Random(seed).shuffle(order)
        self.order = {agent: index for index, agent in enumerate(order)}
        self.active = set(agents)
        self.ready_at = dict.fromkeys(agents, 0.0)
        self.action_time = action_time
        self.logical_time = 0.0
        self.round = 0
        self.started = time.monotonic()
        self._condition = asyncio.Condition()
        self._owner: str | None = None

    def _next(self) -> str | None:
        if not self.active:
            return None
        return min(self.active, key=lambda agent: (self.ready_at[agent], self.order[agent]))

    @asynccontextmanager
    async def operation(self, agent_id: str, extra_delay: float = 0.0) -> AsyncIterator[float]:
        if self.mode == "controlled_async":
            async with self._condition:
                await self._condition.wait_for(
                    lambda: agent_id not in self.active or (self._owner is None and self._next() == agent_id)
                )
                if agent_id not in self.active:
                    raise RuntimeError("inactive agent requested an operation")
                self._owner = agent_id
                self.logical_time = max(self.logical_time, self.ready_at[agent_id])
                now = self.logical_time
            try:
                yield now
            finally:
                async with self._condition:
                    self.ready_at[agent_id] = now + self.action_time + extra_delay
                    self._owner = None
                    self._condition.notify_all()
        elif self.mode == "rounds":
            yield float(self.round)
        else:
            yield time.monotonic() - self.started

    async def finish(self, agent_id: str) -> None:
        async with self._condition:
            self.active.discard(agent_id)
            self._condition.notify_all()

