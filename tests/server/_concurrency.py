"""Synchronization helpers for REST concurrency tests."""

import asyncio


class ActiveHold:
    """Track concurrent fake-handler entries and hold them until released."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0
        self._lock = asyncio.Lock()

    async def wait_until_entered(self) -> None:
        """Wait for handler startup, not a one-second response-time guarantee.

        Allow scheduling headroom for startup in full wheel-qualification
        runs. The event still orders requests deterministically;
        this deadline is only a watchdog, not the concurrency assertion.
        """
        await asyncio.wait_for(self.entered.wait(), timeout=10)

    async def enter(self) -> None:
        async with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.entered.set()

    async def leave(self) -> None:
        async with self._lock:
            self.active -= 1
