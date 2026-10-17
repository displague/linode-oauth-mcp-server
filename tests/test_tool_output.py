"""Test compact search results, schema lookup, and response formatting."""

import httpx2
import pytest
import yaml
from fastmcp import Client

from linode_oauth_mcp_server.main import LinodeMCPServer
from linode_oauth_mcp_server.search import tokenize

PAT = {"personalAccessToken": []}
LONG = "Lots of detail. " * 200


def _op(operation_id, summary, scope, **extra):
    return {
        "operationId": operation_id,
        "summary": summary,
        "description": f"{summary} on your account.\n\n{LONG}",
        "security": [PAT, {"oauth": [scope]}],
        "responses": {
            "200": {
                "description": "ok",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                f"field{i}": {"type": "string", "description": LONG}
                                for i in range(20)
                            },
                        }
                    }
                },
            }
        },
        **extra,
    }


API_VERSION = {
    "name": "apiVersion",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
}
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Linode", "version": "4.229.1"},
    "servers": [{"url": "https://api.linode.com"}],
    "paths": {
        "/{apiVersion}/linode/instances": {
            "parameters": [API_VERSION],
            "get": _op(
                "get-linode-instances",
                "List Linodes",
                "linodes:read_only",
                parameters=[
                    {"name": "page", "in": "query", "schema": {"type": "integer"}}
                ],
            ),
            "post": _op(
                "post-linode-instance",
                "Create a Linode",
                "linodes:read_write",
                requestBody={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["region"],
                                "properties": {
                                    "region": {
                                        "type": "string",
                                        "description": "The region. " + LONG,
                                        "x-akamai": {"labels": ["x"]},
                                        "example": "us-east",
                                    }
                                },
                            }
                        }
                    },
                },
            ),
        },
        "/{apiVersion}/databases/instances": {
            "parameters": [API_VERSION],
            "get": _op(
                "get-databases-instances",
                "List Managed Databases",
                "databases:read_only",
                description="All database instances. " + "instances " * 50,
            ),
        },
    },
}


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("LINODE_API_TOKEN", "pat")

    def api(request):
        return httpx2.Response(
            200, json={"data": [{"id": 1, "label": "web"}], "results": 1}
        )

    class TestServer(LinodeMCPServer):
        def _build_api_client(self, operation_scopes):
            client = super()._build_api_client(operation_scopes)
            client._transport = httpx2.MockTransport(api)
            return client

        async def _refresh_specs(self):
            pass

    server = TestServer()
    server.spec_manager.save_merged_spec("linode", SPEC)
    await server.initialize()
    async with Client(server.mcp) as c:
        yield c


async def test_only_search_call_and_schema_tools_are_listed(client):
    names = {t.name for t in await client.list_tools()}
    assert names == {"search_tools", "call_tool", "get_tool_schema"}


async def test_search_results_are_compact_summaries(client):
    text = (
        (await client.call_tool("search_tools", {"query": "list linodes"}))
        .content[0]
        .text
    )
    assert text.index("### linode_get_linode_instances — List Linodes") < text.index(
        "### linode_get_databases_instances"
    )
    assert "Read-only; scopes: linodes:read_only" in text
    assert "- `page` (integer)" in text
    assert "Lots of detail" not in text  # only the first paragraph
    assert "field0" not in text  # no output schemas
    assert len(text) < 3000


async def test_get_tool_schema_returns_stripped_input_schema(client):
    result = await client.call_tool(
        "get_tool_schema", {"name": "linode_post_linode_instance"}
    )
    schema = result.structured_content["inputSchema"]
    region = schema["properties"]["region"]
    assert region["description"].startswith("The region.")
    assert "x-akamai" not in region and "example" not in region
    assert "region" in schema["required"]


async def test_call_tool_results_are_rendered_as_yaml(client):
    result = await client.call_tool(
        "call_tool",
        {"name": "linode_get_linode_instances", "arguments": {"apiVersion": "v4"}},
    )
    text = result.content[0].text
    assert not text.lstrip().startswith("{")
    assert yaml.safe_load(text) == {"data": [{"id": 1, "label": "web"}], "results": 1}


def test_tokenize_folds_plurals():
    assert tokenize("Linodes instances addresses policies status") == [
        "linode",
        "instance",
        "address",
        "policy",
        "status",
    ]
