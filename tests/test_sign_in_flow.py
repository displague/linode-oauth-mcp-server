"""Test the OAuth sign-in round trip over the MCP 2026-07-28 input-required flow."""

import asyncio
import socket
import urllib.parse

import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult
from fastmcp.server.providers.openapi import OpenAPIProvider
from fastmcp.server.transforms.search import BM25SearchTransform

from linode_oauth_mcp_server.auth import AuthManager
from linode_oauth_mcp_server.main import (
    OAUTH_SCOPES_META,
    LinodeAuth,
    SignInMiddleware,
    _component_fn,
)
from linode_oauth_mcp_server.oauth_scopes import OperationScopes

PAT = {"personalAccessToken": []}
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Linode", "version": "4.229.1"},
    "servers": [{"url": "https://api.linode.com"}],
    "paths": {
        "/v4/account": {
            "get": {
                "operationId": "get-account",
                "security": [PAT, {"oauth": ["account:read_only"]}],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/v4/regions": {
            "get": {
                "operationId": "get-regions",
                "security": [],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(monkeypatch):
    for var in (
        "LINODE_API_TOKEN",
        "LINODE_OAUTH_TOKEN",
        "LINODE_CLI_TOKEN",
        "LINODE_OAUTH_SCOPES",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LINODE_CLI_CONFIG", "/nonexistent")
    monkeypatch.setenv("LINODE_OAUTH_CLIENT_ID", "client")
    port = _free_port()
    monkeypatch.setenv(
        "LINODE_OAUTH_REDIRECT_URI", f"http://127.0.0.1:{port}/oauth/callback"
    )
    monkeypatch.setattr(
        "webbrowser.open", lambda url: pytest.fail("browser must not open")
    )

    auth = AuthManager()
    seen = []

    def api(request):
        seen.append((request.url.path, request.headers.get("Authorization")))
        return httpx2.Response(200, json={"ok": True})

    scopes = OperationScopes(SPEC)
    client = httpx2.AsyncClient(
        base_url="https://api.linode.com",
        transport=httpx2.MockTransport(api),
        auth=LinodeAuth(auth, scopes),
    )
    mcp = FastMCP("t")
    mcp.add_provider(
        OpenAPIProvider(
            openapi_spec=SPEC, client=client, mcp_component_fn=_component_fn(scopes)
        ),
        namespace="linode",
    )
    mcp.add_middleware(SignInMiddleware(auth))
    mcp.add_transform(BM25SearchTransform(max_results=5))
    return mcp, auth, port, seen


def _browser(port, elicitations, action="accept"):
    """An elicitation handler that 'opens' the URL by completing the callback."""

    async def handler(message, response_type, params, context):
        elicitations.append(params)
        if action == "accept":
            state = urllib.parse.parse_qs(urllib.parse.urlparse(params.url).query)[
                "state"
            ][0]

            async def complete():
                async with httpx2.AsyncClient() as http:
                    await http.post(
                        f"http://127.0.0.1:{port}/oauth/token",
                        json={
                            "access_token": "oauth-token",
                            "expires_in": "7200",
                            "state": state,
                        },
                    )

            asyncio.get_running_loop().create_task(complete())
        return ElicitResult(action=action)

    return handler


async def test_tools_carry_required_scopes(server):
    mcp, *_ = server
    tool = await mcp.get_tool("linode_get_account")
    assert tool.meta[OAUTH_SCOPES_META] == ["account:read_only"]


async def test_sign_in_link_is_shown_by_client_then_call_completes(server):
    mcp, auth, port, seen = server
    elicitations = []
    async with Client(mcp, elicitation_handler=_browser(port, elicitations)) as client:
        result = await client.call_tool(
            "call_tool", {"name": "linode_get_account", "arguments": {}}
        )
    assert not result.is_error
    assert len(elicitations) == 1
    assert elicitations[0].mode == "url"
    assert "account%3Aread_only" in elicitations[0].url
    assert seen == [("/v4/account", "Bearer oauth-token")]
    assert "account:read_only" in auth.granted_scopes


async def test_declined_sign_in_does_not_call_api(server):
    mcp, _, port, seen = server
    async with Client(
        mcp, elicitation_handler=_browser(port, [], action="decline")
    ) as client:
        result = await client.call_tool(
            "call_tool",
            {"name": "linode_get_account", "arguments": {}},
            raise_on_error=False,
        )
    assert result.is_error
    assert "declined" in result.content[0].text
    assert seen == []


async def test_public_operation_needs_no_sign_in(server):
    mcp, _, port, seen = server
    elicitations = []
    async with Client(mcp, elicitation_handler=_browser(port, elicitations)) as client:
        await client.call_tool(
            "call_tool", {"name": "linode_get_regions", "arguments": {}}
        )
    assert elicitations == []
    assert seen == [("/v4/regions", None)]


async def test_filter_is_sent_as_header_on_list_operations(monkeypatch, temp_dir):
    from linode_oauth_mcp_server.config import Config
    from linode_oauth_mcp_server.spec_manager import SpecManager

    monkeypatch.setenv("LINODE_API_TOKEN", "pat")
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Linode", "version": "4.229.1"},
        "servers": [{"url": "https://api.linode.com"}],
        "paths": {
            "/{apiVersion}/linode/stackscripts": {
                "get": {
                    "operationId": "get-stack-scripts",
                    "parameters": [
                        {"name": "page", "in": "query", "schema": {"type": "integer"}},
                        {
                            "name": "apiVersion",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "security": [PAT, {"oauth": ["stackscripts:read_only"]}],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    manager = SpecManager(Config.load(None), cache_dir=str(temp_dir))
    manager.save_merged_spec("linode", spec)
    spec = manager.get_provider_spec("linode")

    seen = []

    def api(request):
        seen.append((request.url.path, request.headers.get("X-Filter")))
        return httpx2.Response(
            200, json={"data": [], "page": 1, "pages": 1, "results": 0}
        )

    client = httpx2.AsyncClient(
        base_url="https://api.linode.com",
        transport=httpx2.MockTransport(api),
        auth=LinodeAuth(AuthManager(), OperationScopes(spec)),
    )
    mcp = FastMCP("t")
    mcp.add_provider(
        OpenAPIProvider(openapi_spec=spec, client=client), namespace="linode"
    )
    async with Client(mcp) as c:
        await c.call_tool(
            "linode_get_stack_scripts",
            {"apiVersion": "v4", "X-Filter": '{"mine": true}'},
        )
    assert seen == [("/v4/linode/stackscripts", '{"mine": true}')]
