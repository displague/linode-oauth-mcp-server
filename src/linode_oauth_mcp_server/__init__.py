"""Linode OAuth MCP server package."""

import platform
from importlib.metadata import PackageNotFoundError, version
from typing import Optional

try:
    # Set at build time from the git tag by setuptools-scm.
    __version__ = version("linode-oauth-mcp-server")
except PackageNotFoundError:
    __version__ = "0.0.0"
__description__ = "Linode API v4 OpenAPI MCP server with scoped OAuth sign-in"


def user_agent(spec_version: Optional[str] = None) -> str:
    """User-Agent in linode-cli's style: client, spec version, Python."""
    parts = [f"linode-oauth-mcp-server/{__version__}"]
    if spec_version:
        parts.append(f"linode-api-openapi/{spec_version}")
    parts.append(f"python/{platform.python_version()}")
    return " ".join(parts)
