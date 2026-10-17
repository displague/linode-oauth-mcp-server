"""Configuration for the Linode API MCP server."""

import os
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import platformdirs
import yaml
from pydantic import BaseModel, Field


def default_config_path() -> Path:
    """Path to the configuration bundled with the installed package."""
    return Path(str(resources.files("linode_oauth_mcp_server"))) / (
        "data/config/apis.yaml"
    )


def cache_root() -> Path:
    """Directory for the cached Linode OpenAPI specification.

    Defaults to the per-user cache directory. Override with
    ``LINODE_MCP_CACHE_DIR``.
    """
    env = os.getenv("LINODE_MCP_CACHE_DIR")
    if env:
        return Path(env)
    return Path(platformdirs.user_cache_dir("linode-oauth-mcp-server", appauthor=False))


class SpecSource(BaseModel):
    """Single upstream OpenAPI spec source."""

    url: str = Field(..., description="URL to the OpenAPI spec")


class APIConfig(BaseModel):
    """Configuration for an API group (namespace)."""

    # Not part of the YAML; set by loader for convenience/back-compat
    name: Optional[str] = Field(default=None, description="API key/name in config")

    specs: List[SpecSource] = Field(
        default_factory=list,
        description="List of spec sources to merge under this API namespace",
    )
    auth_type: Optional[str] = Field(
        default=None, description="Authentication type for this API group"
    )
    service_name: Optional[str] = Field(
        default=None, description="Service name (used for path normalization)"
    )
    enabled: bool = Field(default=True, description="Enable/disable this API")
    description: Optional[str] = Field(
        default=None,
        description="Human-readable summary of this API family; appended to "
        "every generated tool description so catalog search can match tools "
        "by product name. Overrides the spec's info title/description.",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Extra tags applied to this API's tools.",
    )
    include: List[str] = Field(
        default_factory=list,
        description="Regex patterns of operationIds to include (after prefix)",
    )
    exclude: List[str] = Field(
        default_factory=list,
        description="Regex patterns of operationIds to exclude (after prefix)",
    )
    format: Dict[str, Union[str, List[str], Dict[str, str]]] = Field(
        default_factory=dict,
        description="JQ format strings per operationId for response transformation. "
        "Can be a single string, list of strings (applied in sequence), "
        "or dict with named formats (e.g., {'summary': 'jq_filter', 'detailed': 'jq_filter2'})",
    )

    # Backward-compat convenience properties (legacy single-spec shape)
    @property
    def url(self) -> str:
        return self.specs[0].url if self.specs else ""


class Config(BaseModel):
    """Main configuration class."""

    config_path: Optional[str] = Field(
        default=None, description="Path to the loaded config file"
    )
    apis: Dict[str, APIConfig] = Field(default_factory=dict)
    oauth: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        """Load configuration from a YAML file.

        When config_path is omitted, the configuration bundled with the
        package is used.
        """
        if config_path is None:
            config_path = str(default_config_path())
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")

        with open(path, "r") as f:
            config_data = yaml.safe_load(f)

        # Convert API configs to APIConfig objects
        if "apis" in config_data:
            apis = {}
            for name, api_data in config_data["apis"].items():
                if not isinstance(api_data, dict):
                    raise ValueError(f"Invalid API config for {name}: {api_data}")

                # Allow a compact single-spec config as well as a spec list.
                specs_list: List[SpecSource] = []
                if "specs" in api_data and isinstance(api_data["specs"], list):
                    for item in api_data["specs"]:
                        if not isinstance(item, dict) or "url" not in item:
                            raise ValueError(f"Invalid spec entry for {name}: {item}")
                        specs_list.append(SpecSource(**item))
                else:
                    # Compact single-spec form
                    if "url" in api_data:
                        specs_list.append(SpecSource(url=api_data["url"]))

                # Build model dict
                model_data: Dict[str, Any] = {
                    "name": name,
                    "specs": specs_list,
                    "auth_type": api_data.get("auth_type"),
                    "service_name": api_data.get("service_name"),
                    "enabled": api_data.get("enabled", True),
                    "description": api_data.get("description"),
                    "tags": api_data.get("tags", []) or [],
                    "include": api_data.get("include", []) or [],
                    "exclude": api_data.get("exclude", []) or [],
                    "format": api_data.get("format", {}) or {},
                }

                apis[name] = APIConfig(**model_data)
            config_data["apis"] = apis

        config = cls(**config_data)
        config.config_path = config_path
        return config

    def save(self, config_path: str) -> None:
        """Save configuration to a YAML file."""
        path = Path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Convert to dict for YAML serialization
        config_dict = self.model_dump()

        with open(path, "w") as f:
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)

    def get_api_names(self) -> List[str]:
        """Get list of configured API names."""
        return list(self.apis.keys())

    def get_api_config(self, name: str) -> Optional[APIConfig]:
        """Get configuration for a specific API."""
        return self.apis.get(name)
