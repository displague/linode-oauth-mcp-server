"""Linode OAuth MCP server package."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Set at build time from the git tag by setuptools-scm.
    __version__ = version("linode-oauth-mcp-server")
except PackageNotFoundError:
    __version__ = "0.0.0"
__description__ = "Linode API v4 OpenAPI MCP server with scoped OAuth sign-in"
