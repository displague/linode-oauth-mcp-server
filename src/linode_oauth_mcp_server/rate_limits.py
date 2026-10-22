"""Retry Linode API requests that were rate limited or briefly unavailable.

Modeled on linode_api4-python's retry settings (408, 429 and 502, honoring
``Retry-After``, five retries), with two changes: gateway errors are retried
only for idempotent methods, since a 502 on a POST may follow a resource
that was created, and all waits for one request share a small budget so a
tool call returns before the MCP client gives up on it. When the budget is
spent, the last response is returned and the tool error says when to retry.
"""

import asyncio
import logging
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx2

from .progress import progress_reporter

logger = logging.getLogger(__name__)

# The API rejected these before acting on them, so any method can be resent
NOT_APPLIED_STATUSES = frozenset({408, 429})
# The request may or may not have been applied
GATEWAY_STATUSES = frozenset({502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
RETRY_EXTENSION = "linode_mcp_retry"

# Waits between retries; tests replace it
_sleep = asyncio.sleep


@dataclass(frozen=True)
class RetryPolicy:
    """How many times, and for how long, to retry one API request."""

    max_retries: int = 5
    max_wait: float = 15.0
    backoff: float = 1.0

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        """Read ``LINODE_MCP_RETRY_MAX`` and ``LINODE_MCP_RETRY_WAIT``.

        ``LINODE_MCP_RETRY_MAX=0`` turns retries off.
        """
        policy = cls()
        try:
            return cls(
                max_retries=int(os.getenv("LINODE_MCP_RETRY_MAX", policy.max_retries)),
                max_wait=float(os.getenv("LINODE_MCP_RETRY_WAIT", policy.max_wait)),
            )
        except ValueError:
            logger.warning("Ignoring invalid LINODE_MCP_RETRY_* settings")
            return policy

    def retryable(self, method: str, status: int) -> bool:
        if status in NOT_APPLIED_STATUSES:
            return True
        return status in GATEWAY_STATUSES and method in IDEMPOTENT_METHODS


@dataclass(frozen=True)
class RetryOutcome:
    """Recorded on a response that was still failing when retries stopped."""

    retries: int
    retry_after: float | None


def retry_after(response: httpx2.Response) -> float | None:
    """Seconds the ``Retry-After`` header asks for, if it gives a usable value."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class RetryTransport(httpx2.AsyncBaseTransport):
    """Resend rate-limited requests, waiting as the API asks."""

    def __init__(self, transport: httpx2.AsyncBaseTransport, policy: RetryPolicy):
        self._transport = transport
        self._policy = policy

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        # Buffer the body so it can be sent again
        await request.aread()
        waited = 0.0
        retries = 0
        while True:
            response = await self._transport.handle_async_request(request)
            if not self._policy.retryable(request.method, response.status_code):
                return response
            asked = retry_after(response)
            delay = (
                asked
                if asked is not None
                else self._policy.backoff * 2**retries * random.uniform(0.5, 1.0)
            )
            if (
                retries >= self._policy.max_retries
                or waited + delay > self._policy.max_wait
            ):
                response.extensions[RETRY_EXTENSION] = RetryOutcome(
                    retries, asked if asked is not None else delay
                )
                return response
            await response.aclose()
            retries += 1
            waited += delay
            message = (
                f"Linode API returned {response.status_code} "
                f"{response.reason_phrase}; retrying in {delay:.0f}s "
                f"(retry {retries} of {self._policy.max_retries})"
            )
            logger.info("%s %s: %s", request.method, request.url.path, message)
            if report := progress_reporter():
                await report(message)
            await _sleep(delay)

    async def aclose(self) -> None:
        await self._transport.aclose()


def failure_hint(response: httpx2.Response) -> str | None:
    """What an agent should know about a rate-limited or gateway error."""
    status = response.status_code
    if status not in NOT_APPLIED_STATUSES | GATEWAY_STATUSES:
        return None
    outcome = response.extensions.get(RETRY_EXTENSION)
    wait = outcome.retry_after if outcome else retry_after(response)
    parts = [f"The Linode API responded HTTP {status} {response.reason_phrase}."]
    if outcome and outcome.retries:
        parts.append(f"The server retried {outcome.retries} times.")
    if status in NOT_APPLIED_STATUSES:
        parts.append("The Linode API did not apply this request.")
        parts.append(
            f"Retry in about {wait:.0f} seconds." if wait else "Retry it later."
        )
    elif response.request.method in IDEMPOTENT_METHODS:
        parts.append(
            "The Linode API was briefly unavailable; this request is safe to retry"
            + (f" in about {wait:.0f} seconds." if wait else " later.")
        )
    else:
        parts.append(
            "The outcome is unknown: the Linode API may have applied this request. "
            "Check the resource's current state before retrying."
        )
    return " ".join(parts)
