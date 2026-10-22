"""MCP progress notifications for the running tool call."""

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]

# The request context a reporter belongs to, and the reporter
_current: ContextVar[tuple[Any, Progress] | None] = ContextVar(
    "progress_reporter", default=None
)


def progress_reporter() -> Progress | None:
    """The running tool call's progress reporter, created on first use.

    MCP progress values must increase within a request, so everything a call
    runs (workflow steps, rate-limit waits) reports through one counter.
    Returns None outside a request; reporting is a no-op when the client
    sent no progress token.
    """
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
    except Exception:
        return None
    current = _current.get()
    if current is not None and current[0] is ctx:
        return current[1]
    count = 0

    async def report(message: str) -> None:
        nonlocal count
        count += 1
        try:
            await ctx.report_progress(count, None, message)
        except Exception:
            logger.debug("Could not report progress", exc_info=True)

    _current.set((ctx, report))
    return report
