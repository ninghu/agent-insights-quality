from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


async def drain_on_cancel(operation: Coroutine[Any, Any, T]) -> T:
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A cancelled waiter cannot stop an executor thread or its remote effect.
        # Drain the started operation before permits and ownership can unwind.
        drained = asyncio.gather(task, return_exceptions=True)
        while not drained.done():
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError:
                continue
        raise
