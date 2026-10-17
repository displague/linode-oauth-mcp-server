"""Linode OAuth MCP server: Linode API v4 operations as MCP tools."""

import asyncio
import logging
from typing import Any, AsyncGenerator, List, Literal, Optional

import click
import httpx2
import yaml
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers.openapi import OpenAPIProvider, OpenAPITool
from fastmcp.tools.base import InputRequiredToolResult, ToolResult
from mcp_types import (
    ElicitRequest,
    ElicitRequestURLParams,
    InputRequiredResult,
    TextContent,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from . import user_agent
from .auth import AuthManager, client_supports_url_elicitation
from .config import APIConfig, Config
from .oauth_scopes import OperationScopes
from .openapi_annotations import annotate_openapi_component
from .response_formatter import ResponseFormatter
from .search import LinodeSearchTransform
from .spec_manager import SpecManager
from .tool_summaries import OAUTH_SCOPES_META, strip_schema, summarize_tools

logger = logging.getLogger(__name__)
CATALOG_CACHE_TTL_SECONDS = 3600
CATALOG_CACHE_SCOPE: Literal["public", "private"] = "public"
API_BASE_URL = "https://api.linode.com"
SIGN_IN_INPUT = "linode_sign_in"
CALL_TOOL_NAME = "call_tool"
TOOL_SCHEMA_TOOL_NAME = "get_tool_schema"


def _configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        force=True,
    )
    for name in ("urllib3", "requests", "httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)


class LinodeAuth(httpx2.Auth):
    """Inject a Linode bearer credential carrying the operation's OAuth scopes."""

    def __init__(self, auth_manager: AuthManager, operation_scopes: OperationScopes):
        self._auth_manager = auth_manager
        self._operation_scopes = operation_scopes

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        requirement = self._operation_scopes.requirement(
            request.method, request.url.path
        )
        if (
            requirement is None
            or not requirement.public
            or self._auth_manager.has_token()
        ):
            scopes = requirement.scopes if requirement else frozenset()
            request.headers.update(await self._auth_manager.get_auth_header(scopes))
        yield request


def _target_tool_name(context: MiddlewareContext) -> str:
    """The tool a call runs, looking through the search transform's call_tool."""
    name = getattr(context.message, "name", "") or ""
    arguments = getattr(context.message, "arguments", None) or {}
    if name == CALL_TOOL_NAME and isinstance(arguments.get("name"), str):
        return arguments["name"]
    return name


class SignInMiddleware(Middleware):
    """Ask the MCP client to show the Linode sign-in link before a tool runs.

    On MCP 2026-07-28 a server cannot push an elicitation mid-call. When a tool
    needs scopes the current token lacks, this returns an input-required result
    carrying a URL elicitation; the client shows the link and retries the call,
    which then waits for the sign-in callback. Older clients fall through to
    the auth flow, which pushes the elicitation or opens the system browser.
    """

    def __init__(self, auth_manager: AuthManager):
        self._auth_manager = auth_manager

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> ToolResult:
        ctx = context.fastmcp_context
        request_context = ctx.request_context if ctx else None
        if (
            ctx is None
            or request_context is None
            or request_context.protocol_version not in MODERN_PROTOCOL_VERSIONS
            or not client_supports_url_elicitation(ctx.session)
        ):
            return await call_next(context)
        scopes = await self._required_scopes(ctx, context)
        if not scopes or not self._auth_manager.needs_sign_in(scopes):
            return await call_next(context)

        sign_in = await self._auth_manager.begin_sign_in(scopes)
        answer = (ctx.input_responses or {}).get(SIGN_IN_INPUT)
        if answer is None:
            sign_in.presented = True
            return InputRequiredToolResult(
                InputRequiredResult(
                    result_type="input_required",
                    input_requests={
                        SIGN_IN_INPUT: ElicitRequest(
                            method="elicitation/create",
                            params=ElicitRequestURLParams(
                                mode="url",
                                message=sign_in.message,
                                url=sign_in.url,
                                elicitation_id=sign_in.state,
                            ),
                        )
                    },
                )
            )
        if getattr(answer, "action", None) != "accept":
            raise ToolError("Linode sign-in was declined; the call was not made")
        await self._auth_manager.wait_for_sign_in(sign_in)
        return await call_next(context)

    async def _required_scopes(
        self, ctx: Context, context: MiddlewareContext
    ) -> frozenset:
        try:
            tool = await ctx.fastmcp.get_tool(_target_tool_name(context))
        except Exception:
            tool = None
        return frozenset((getattr(tool, "meta", None) or {}).get(OAUTH_SCOPES_META, ()))


def _component_fn(operation_scopes: OperationScopes):
    """Annotate API tools and record each operation's required OAuth scopes."""

    def annotate(route, component) -> None:
        annotate_openapi_component(route, component)
        if not isinstance(component, OpenAPITool):
            return
        requirement = operation_scopes.requirement(route.method, route.path)
        if requirement is not None and requirement.scopes:
            component.meta = {
                **(component.meta or {}),
                OAUTH_SCOPES_META: sorted(requirement.scopes),
            }

    return annotate


class ResponseFormattingMiddleware(Middleware):
    """Render structured API responses as YAML for human readability."""

    def __init__(self, formatter: ResponseFormatter, api_names: List[str]):
        self._formatter = formatter
        self._api_prefixes = tuple(f"{name}_" for name in api_names)

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> ToolResult:
        result = await call_next(context)
        name = _target_tool_name(context)
        if not name.startswith(self._api_prefixes) or result.is_error:
            return result
        data: Any = result.structured_content
        if data is None:
            return result
        formatted = self._formatter.format_response(name, data)
        try:
            rendered = yaml.dump(
                data if formatted is None else formatted,
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
            )
        except Exception:
            logger.exception("Could not render Linode response for %s", name)
            return result
        # Clients such as Claude Code show structured content in place of
        # text. call_tool declares no output schema, so return only the YAML;
        # a directly listed API tool's output schema requires the data.
        via_call_tool = getattr(context.message, "name", None) == CALL_TOOL_NAME
        return ToolResult(
            content=[TextContent(type="text", text=rendered)],
            structured_content=None if via_call_tool else data,
        )


class LinodeMCPServer:
    """Expose the Linode v4 OpenAPI operations as MCP tools."""

    def __init__(self, config_path: Optional[str] = None, tool_catalog: str = "search"):
        self.config = Config.load(config_path)
        self.auth_manager = AuthManager(self.config)
        self.spec_manager = SpecManager(self.config)
        self.response_formatter = ResponseFormatter(self.config)
        self.tool_catalog = tool_catalog
        self.mcp: Optional[FastMCP] = None
        self._api_clients: List[httpx2.AsyncClient] = []
        self._spec_refresh: Optional[asyncio.Task] = None
        self._spec_version: Optional[str] = None

    async def initialize(self, force_update_specs: bool = False) -> None:
        if force_update_specs or not self.spec_manager.has_all_cached_specs():
            logger.info("Fetching the Linode OpenAPI specification")
            await self.spec_manager.update_specs()
        else:
            logger.info("Using cached Linode OpenAPI specification")
            self._spec_refresh = asyncio.create_task(self._refresh_specs())

        self.mcp = FastMCP(
            name="Linode API",
            instructions=(
                "This server exposes operations from the Linode API v4 OpenAPI "
                "specification as MCP tools. API tools are named linode_<operationId>. "
                "Use search_tools to find an operation, then call_tool to invoke it. "
                "Calls use the configured Linode bearer token and can change or "
                "delete billable resources; inspect the operation before calling it."
            ),
            cache_ttl=CATALOG_CACHE_TTL_SECONDS,
            cache_scope=CATALOG_CACHE_SCOPE,
        )
        self._register_api_providers()
        self.mcp.add_middleware(SignInMiddleware(self.auth_manager))
        self.mcp.add_middleware(
            ResponseFormattingMiddleware(
                self.response_formatter, list(self.config.apis)
            )
        )
        self._apply_catalog_transform()

    async def _refresh_specs(self) -> None:
        """Check upstream for a newer spec without delaying startup."""
        try:
            if await self.spec_manager.refresh_if_changed():
                logger.info(
                    "A newer Linode OpenAPI spec was cached; restart the server to use it"
                )
        except Exception:
            logger.exception("Background Linode spec refresh failed")

    def _register_api_providers(self) -> None:
        assert self.mcp is not None
        self.failed_providers: dict[str, str] = {}
        for api_name, api_config in self.config.apis.items():
            if not api_config.enabled:
                continue
            spec = self.spec_manager.get_provider_spec(api_name)
            if not spec:
                self.failed_providers[api_name] = "no cached spec"
                continue
            try:
                operation_scopes = OperationScopes(spec)
                self._spec_version = (spec.get("info") or {}).get("version")
                provider = OpenAPIProvider(
                    openapi_spec=spec,
                    client=self._build_api_client(operation_scopes),
                    tags={"linode", *api_config.tags},
                    mcp_component_fn=_component_fn(operation_scopes),
                )
                self.mcp.add_provider(provider, namespace=api_name)
            except Exception as exc:
                logger.exception("Failed to build Linode API provider")
                self.failed_providers[api_name] = str(exc)

    def _build_api_client(
        self, operation_scopes: OperationScopes
    ) -> httpx2.AsyncClient:
        client = httpx2.AsyncClient(
            base_url=API_BASE_URL,
            timeout=30.0,
            auth=LinodeAuth(self.auth_manager, operation_scopes),
            headers={"User-Agent": user_agent(self._spec_version)},
        )
        self._api_clients.append(client)
        return client

    def _apply_catalog_transform(self) -> None:
        assert self.mcp is not None
        if self.tool_catalog == "full":
            return
        if self.tool_catalog == "code-mode":
            try:
                from fastmcp.experimental.transforms.code_mode import (
                    CodeMode,
                    GetSchemas,
                    GetTags,
                    Search,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "Code mode requires the optional FastMCP code-mode dependency"
                ) from exc
            self.mcp.add_transform(
                CodeMode(discovery_tools=[GetTags(), Search(), GetSchemas()])
            )
            return
        self._add_tool_schema_tool()
        self.mcp.add_transform(
            LinodeSearchTransform(
                max_results=10,
                call_tool_name=CALL_TOOL_NAME,
                always_visible=[TOOL_SCHEMA_TOOL_NAME],
                search_result_serializer=summarize_tools,
            )
        )

    def _add_tool_schema_tool(self) -> None:
        assert self.mcp is not None
        mcp = self.mcp

        @mcp.tool(name=TOOL_SCHEMA_TOOL_NAME, annotations={"readOnlyHint": True})
        async def get_tool_schema(name: str) -> dict:
            """Return a Linode API tool's full description and input schema.

            Search results summarize parameters in one line each. Use this
            before calling a tool whose nested parameters need more detail.
            """
            tool = await mcp.get_tool(name)
            if tool is None or not name.startswith("linode_"):
                raise ToolError(f"Unknown Linode API tool: {name!r}")
            return {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": strip_schema(tool.parameters),
            }

    async def run(
        self,
        force_update_specs: bool = False,
        transport: str = "stdio",
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> None:
        await self.initialize(force_update_specs)
        assert self.mcp is not None
        if transport == "http":
            await self.mcp.run_async(transport="http", host=host, port=port)
        else:
            await self.mcp.run_stdio_async(show_banner=True)


@click.command()
@click.option(
    "--config", "-c", default=None, help="Path to the Linode API configuration"
)
@click.option(
    "--update-specs",
    is_flag=True,
    help="Fetch and cache a fresh Linode API spec, then exit",
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"], case_sensitive=False),
    default="stdio",
    help="MCP transport; use HTTP for remote clients and Secure MCP Tunnel",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind host for HTTP transport",
)
@click.option(
    "--port",
    type=int,
    default=8000,
    show_default=True,
    help="Bind port for HTTP transport",
)
@click.option(
    "--tool-catalog",
    type=click.Choice(["search", "code-mode", "full"], case_sensitive=False),
    default="search",
    help="Expose tools through search, experimental code mode, or the full list",
)
@click.option(
    "--log-level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False
    ),
    default="INFO",
    help="Logging level",
)
def main(
    config: Optional[str],
    update_specs: bool,
    transport: str,
    host: str,
    port: int,
    tool_catalog: str,
    log_level: str,
) -> None:
    """Start the Linode OAuth MCP server."""
    _configure_logging(log_level)

    async def _main() -> None:
        server = LinodeMCPServer(config, tool_catalog=tool_catalog.lower())
        if update_specs:
            await server.spec_manager.update_specs()
            click.echo("Linode API spec cached successfully")
        else:
            await server.run(transport=transport.lower(), host=host, port=port)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
