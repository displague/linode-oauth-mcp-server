"""Test retries of rate-limited and briefly unavailable API requests."""

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx2
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_tool_output import SPEC

from linode_oauth_mcp_server import rate_limits
from linode_oauth_mcp_server.main import LinodeMCPServer
from linode_oauth_mcp_server.rate_limits import (
    RetryPolicy,
    RetryTransport,
    retry_after,
)


@pytest.fixture
def waits(monkeypatch):
    waited: list[float] = []

    async def sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr(rate_limits, "_sleep", sleep)
    return waited


def _responder(*statuses, headers=None):
    """Answer requests with the given statuses in turn, then 200."""
    sent: list[httpx2.Request] = []
    queue = list(statuses)

    def handle(request):
        sent.append(request)
        status = queue.pop(0) if queue else 200
        return httpx2.Response(
            status, headers=headers if status != 200 else None, json={"ok": status}
        )

    return handle, sent


async def _send(handler, method="GET", policy: RetryPolicy | None = None, **kwargs):
    transport = RetryTransport(httpx2.MockTransport(handler), policy or RetryPolicy())
    async with httpx2.AsyncClient(
        transport=transport, base_url="https://api.linode.com"
    ) as client:
        return await client.request(method, "/v4/linode/instances", **kwargs)


async def test_429_waits_for_retry_after_then_succeeds(waits):
    handler, sent = _responder(429, 429, headers={"Retry-After": "2"})
    response = await _send(handler)
    assert response.status_code == 200
    assert len(sent) == 3
    assert waits == [2.0, 2.0]


async def test_post_body_is_resent_after_429(waits):
    handler, sent = _responder(429, headers={"Retry-After": "1"})
    response = await _send(handler, "POST", json={"region": "us-east"})
    assert response.status_code == 200
    assert [r.content for r in sent] == [b'{"region":"us-east"}'] * 2


async def test_429_beyond_budget_is_returned_without_waiting(waits):
    handler, sent = _responder(429, headers={"Retry-After": "60"})
    response = await _send(handler)
    assert response.status_code == 429
    assert len(sent) == 1 and waits == []
    outcome = response.extensions[rate_limits.RETRY_EXTENSION]
    assert outcome.retries == 0 and outcome.retry_after == 60


async def test_retries_stop_at_max_retries(waits):
    handler, sent = _responder(*[429] * 10, headers={"Retry-After": "0"})
    response = await _send(handler, policy=RetryPolicy(max_retries=2))
    assert response.status_code == 429
    assert len(sent) == 3


async def test_gateway_errors_are_retried_only_for_idempotent_methods(waits):
    handler, sent = _responder(502)
    assert (await _send(handler, "GET")).status_code == 200
    assert len(sent) == 2

    handler, sent = _responder(502)
    assert (await _send(handler, "POST", json={})).status_code == 502
    assert len(sent) == 1


async def test_backoff_without_retry_after_grows(waits, monkeypatch):
    monkeypatch.setattr(rate_limits.random, "uniform", lambda a, b: b)
    handler, _ = _responder(503, 503, 503)
    await _send(handler, policy=RetryPolicy(max_wait=100))
    assert waits == [1.0, 2.0, 4.0]


async def test_waiting_can_be_cancelled(monkeypatch):
    monkeypatch.setattr(rate_limits, "_sleep", asyncio.sleep)
    handler, sent = _responder(429, headers={"Retry-After": "10"})
    task = asyncio.create_task(_send(handler))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sent) == 1


def test_retry_after_accepts_seconds_and_http_dates():
    def header(value):
        return httpx2.Response(429, headers={"Retry-After": value})

    assert retry_after(header("7")) == 7
    soon = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 28 <= (retry_after(header(soon)) or 0) <= 30
    assert retry_after(header("soon")) is None
    assert retry_after(httpx2.Response(429)) is None


def test_policy_reads_environment(monkeypatch):
    monkeypatch.setenv("LINODE_MCP_RETRY_MAX", "0")
    monkeypatch.setenv("LINODE_MCP_RETRY_WAIT", "3.5")
    assert RetryPolicy.from_env() == RetryPolicy(max_retries=0, max_wait=3.5)
    monkeypatch.setenv("LINODE_MCP_RETRY_MAX", "lots")
    assert RetryPolicy.from_env() == RetryPolicy()


# --- Through the MCP server ---------------------------------------------


@pytest.fixture
async def server_with(monkeypatch):
    monkeypatch.setenv("LINODE_API_TOKEN", "pat")

    async def build(handler):
        class TestServer(LinodeMCPServer):
            def _api_transport(self):
                return httpx2.MockTransport(handler)

            async def _refresh_specs(self):
                pass

        server = TestServer()
        server.spec_manager.save_merged_spec("linode", SPEC)
        await server.initialize()
        return server

    return build


CALL = {"name": "linode_get_linode_instances", "arguments": {"apiVersion": "v4"}}
CREATE = {
    "name": "linode_post_linode_instance",
    "arguments": {"apiVersion": "v4", "region": "us-east"},
}


async def test_tool_call_reports_rate_limit_waits_as_progress(server_with, waits):
    handler, sent = _responder(429, headers={"Retry-After": "3"})
    server = await server_with(handler)
    messages: list[str] = []

    async def on_progress(progress, total, message):
        messages.append(message)

    async with Client(server.mcp, progress_handler=on_progress) as client:
        result = await client.call_tool("call_tool", CALL)
    assert not result.is_error
    assert len(sent) == 2
    assert messages == [
        "Linode API returned 429 Too Many Requests; retrying in 3s (retry 1 of 5)"
    ]


async def test_rate_limit_error_says_request_was_not_applied(server_with, waits):
    handler, _ = _responder(*[429] * 10, headers={"Retry-After": "90"})
    server = await server_with(handler)
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError) as error:
            await client.call_tool("call_tool", CREATE)
    text = str(error.value)
    assert "HTTP 429 Too Many Requests" in text
    assert text.count("did not apply") == 1
    assert "did not apply this request" in text
    assert "Retry in about 90 seconds" in text


async def test_gateway_error_on_create_warns_outcome_unknown(server_with, waits):
    handler, sent = _responder(502)
    server = await server_with(handler)
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError) as error:
            await client.call_tool("call_tool", CREATE)
    assert len(sent) == 1
    assert "outcome is unknown" in str(error.value)
