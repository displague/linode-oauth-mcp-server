"""Manages the lifecycle of OpenAPI specifications.

This module fetches, caches, normalizes, and prepares the Linode OpenAPI
specification for the FastMCP provider.
"""

import asyncio
import collections.abc
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx2
import yaml

from . import user_agent
from .config import APIConfig, Config, cache_root

logger = logging.getLogger(__name__)

# The cache is JSON: a 10 MB spec loads in well under a second as JSON but
# takes close to a minute through PyYAML.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
# Bump when spec processing changes so caches written by older versions are
# rebuilt instead of reused.
CACHE_VERSION = 2


def _sanitize_unicode(value: Any) -> Any:
    """Remove unpaired UTF-16 surrogate code points from decoded spec data.

    Some upstream OpenAPI documents contain escaped surrogate code points in
    descriptions. They are legal to carry in a Python ``str`` but cannot be
    encoded as UTF-8 by MCP transports, causing tool discovery to fail.
    """
    if isinstance(value, str):
        return "".join(char for char in value if not 0xD800 <= ord(char) <= 0xDFFF)
    if isinstance(value, dict):
        return {
            _sanitize_unicode(key): _sanitize_unicode(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_unicode(item) for item in value]
    return value


X_FILTER_PARAMETER = {
    "name": "X-Filter",
    "in": "header",
    "required": False,
    "description": (
        "JSON object that filters and sorts the results, sent as the X-Filter "
        'header. Example: {"mine": true} or {"region": "us-east", '
        '"+order_by": "label"}. Supports +and, +or, +contains, +gt, +gte, '
        "+lt, +lte, +neq, +order_by and +order. See "
        "https://techdocs.akamai.com/linode-api/reference/filtering-and-sorting"
    ),
    "schema": {"type": "string"},
}


def _add_filter_header(spec: Dict[str, Any]) -> None:
    """Give every paginated list operation an ``X-Filter`` string parameter.

    Linode honors ``X-Filter`` on all paginated GETs, but the spec declares it
    on only a few, and those as a JSON-object ``content`` parameter. Generated
    tools drop undeclared headers, so filters like ``{"mine": true}`` would be
    silently ignored.
    """
    shared = (spec.get("components") or {}).get("parameters") or {}

    def name(param: Dict[str, Any]) -> Optional[str]:
        ref = param.get("$ref")
        if isinstance(ref, str):
            param = shared.get(ref.rsplit("/", 1)[-1], {})
        return param.get("name")

    for path_item in (spec.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        operation = path_item.get("get")
        if not isinstance(operation, dict):
            continue
        inherited = [p for p in path_item.get("parameters", []) if isinstance(p, dict)]
        own = [p for p in operation.get("parameters", []) if isinstance(p, dict)]
        if not {"page", "page_size"} & {name(p) for p in inherited + own}:
            continue
        operation["parameters"] = [p for p in own if name(p) != "X-Filter"] + [
            dict(X_FILTER_PARAMETER)
        ]
        if any(name(p) == "X-Filter" for p in inherited):
            path_item["parameters"] = [p for p in inherited if name(p) != "X-Filter"]


def deep_merge(d: Dict[str, Any], u: collections.abc.Mapping) -> Dict[str, Any]:
    """
    Merge two dictionaries recursively.

    :param d: The dictionary to merge into.
    :param u: The dictionary to merge from.
    :return: The merged dictionary.
    """
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping):
            d[k] = deep_merge(d.get(k, {}), v)
        else:
            d[k] = v
    return d


def _update_references(obj: Any) -> Any:
    """Recursively update '$ref' values in the spec."""
    # This function is currently not used, but is kept for future reference.
    if isinstance(obj, dict):
        for key, value in obj.items():
            if (
                key == "$ref"
                and isinstance(value, str)
                and value.startswith("#/components/schemas/")
            ):
                new_ref = f"#/$defs/{value.split('/')[-1]}"
                obj[key] = new_ref
            else:
                _update_references(value)
    elif isinstance(obj, list):
        for item in obj:
            _update_references(item)
    return obj


class SpecManager:
    """Manages fetching, caching, and merging of OpenAPI specifications."""

    def __init__(self, config: Config, cache_dir: Optional[str] = None):
        self.config = config
        self.cache_dir = Path(cache_dir) if cache_dir else cache_root() / "specs"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Upstream specification locations may redirect, so follow them.
        self.http_client = httpx2.AsyncClient(
            follow_redirects=True, headers={"User-Agent": user_agent()}
        )

    async def update_specs(self) -> None:
        """Update all API specifications based on the configuration."""
        tasks = []
        for api_name, api_config in self.config.apis.items():
            if api_config.enabled:
                tasks.append(self._process_api_group(api_name, api_config))
        await asyncio.gather(*tasks)

    async def refresh_if_changed(self) -> bool:
        """Re-fetch specs only when an upstream source has changed.

        Each source is checked with a conditional GET against its cached ETag
        or Last-Modified value, so an unchanged spec costs one small request.
        Returns True when the cache was updated.
        """
        changed = False
        for api_name, api_config in self.config.apis.items():
            if not api_config.enabled:
                continue
            for i, spec_source in enumerate(api_config.specs):
                if await self._source_changed(f"{api_name}_{i}", spec_source.url):
                    changed = True
        if changed:
            await self.update_specs()
        return changed

    async def _source_changed(self, spec_key: str, url: str) -> bool:
        validators = self._load_meta(spec_key)
        headers = {}
        if validators.get("etag"):
            headers["If-None-Match"] = validators["etag"]
        if validators.get("last_modified"):
            headers["If-Modified-Since"] = validators["last_modified"]
        if not headers or validators.get("url") != url:
            return True
        try:
            # Stream so a changed spec's body is not downloaded twice.
            async with self.http_client.stream("GET", url, headers=headers) as response:
                return response.status_code != 304
        except httpx2.HTTPError as e:
            logger.info(f"Could not check {url} for spec updates: {e}")
            return False

    async def _process_api_group(self, api_name: str, api_config: APIConfig) -> None:
        """Process a single group of API specifications."""
        logger.info(f"Processing API group: {api_name}")
        base_spec: Optional[Dict[str, Any]] = None
        for i, spec_source in enumerate(api_config.specs):
            spec = await self._fetch_and_cache_spec(f"{api_name}_{i}", spec_source.url)
            if not spec:
                continue

            # Extract base path from server URL
            base_path = ""
            if "servers" in spec and spec["servers"]:
                server_url = spec["servers"][0].get("url", "")
                if server_url:
                    from urllib.parse import urlparse

                    parsed_url = urlparse(server_url)
                    base_path = parsed_url.path
                    # remove trailing slash if present
                    if base_path.endswith("/"):
                        base_path = base_path[:-1]

            # Prepend base_path to all paths
            if base_path and "paths" in spec:
                new_paths = {}
                for path, path_item in spec["paths"].items():
                    new_paths[f"{base_path}{path}"] = path_item
                spec["paths"] = new_paths

            if base_spec is None:
                base_spec = spec
            else:
                # Merge subsequent specs into the base spec
                base_spec.setdefault("paths", {}).update(spec.get("paths", {}))
                if "components" in spec:
                    logger.debug(
                        f"Merging components for {api_name}: {spec.get('components')}"
                    )
                    deep_merge(
                        base_spec.setdefault("components", {}), spec["components"]
                    )
                if "$defs" in spec:
                    logger.debug(f"Merging $defs for {api_name}: {spec.get('$defs')}")
                    deep_merge(base_spec.setdefault("$defs", {}), spec["$defs"])

        if base_spec is None:
            return

        merged_spec = base_spec

        # Some specs omit operationIds; synthesize them so
        # every operation can become a tool.
        try:
            self._apply_autogen_operation_ids(merged_spec)
        except Exception as e:
            logger.debug(f"Could not apply autogen operationIds: {e}")

        # Repair recurring authoring bugs that make otherwise-valid specs
        # unparseable.
        self._sanitize_schema_quirks(merged_spec)

        # Normalize auth: promote Authorization header parameters to security requirements
        try:
            self._promote_auth_header_to_security(merged_spec)
        except Exception as e:
            logger.warning(
                f"Auth normalization skipped for {api_name} due to error: {e}"
            )

        self.save_merged_spec(api_name, merged_spec)

    _HTTP_METHODS = (
        "get",
        "put",
        "post",
        "delete",
        "options",
        "head",
        "patch",
        "trace",
    )

    async def _fetch_and_cache_spec(
        self, spec_key: str, url: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch a spec from a URL, validate, and cache it."""
        try:
            response = await self.http_client.get(url)
            response.raise_for_status()
            spec_content = response.text

            try:
                spec = json.loads(spec_content)
            except ValueError:
                spec = yaml.load(spec_content, Loader=_YAML_LOADER)

            if not str(spec.get("openapi", "")).startswith("3."):
                logger.error(f"{url} is not an OpenAPI 3 document")
                return None

            self.save_cached_spec(spec_key, spec)
            self._save_meta(
                spec_key,
                {
                    "url": url,
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "version": (spec.get("info") or {}).get("version"),
                },
            )
            logger.info("Fetched and cached OpenAPI spec from %s", url)
            return spec
        except httpx2.HTTPStatusError as e:
            logger.error(f"HTTP error fetching spec from {url}: {e}")
        except yaml.YAMLError as e:
            logger.error(f"YAML parsing error for spec from {url}: {e}")
            return None  # Return None to prevent malformed data from proceeding
        except Exception as e:
            logger.error(f"Failed to fetch or parse spec from {url}: {e}")
            return None

        # If all fails, try to load from cache as a last resort.
        return self.load_cached_spec(spec_key)

    def _promote_auth_header_to_security(self, spec: Dict[str, Any]) -> None:
        """Normalize auth using OAS3 security with a root default.

        Strategy:
        - Remove legacy Authorization header parameters from operations.
        - Keep explicit upstream security requirements.
        - Mark operations without auth requirements as explicitly unauthenticated.
        - Add a bearer scheme only when a removed header parameter needs one.
        """
        if not isinstance(spec, dict):
            return

        components = spec.get("components", {}) or {}
        comp_params: Dict[str, Any] = components.get("parameters", {}) or {}

        # Identify candidate global Authorization header parameters
        auth_param_keys: set[str] = set()
        for key, param in comp_params.items():
            if not isinstance(param, dict):
                continue
            loc = param.get("in")
            name = str(param.get("name", ""))
            x_prefix = param.get("x-prefix") or param.get("x_prefix")
            if (
                loc == "header"
                and name.lower() == "authorization"
                and (
                    x_prefix is None
                    or str(x_prefix).strip().lower().startswith("bearer")
                )
            ):
                auth_param_keys.add(key)

        # Ensure at least one bearer scheme exists to validate references if needed.
        sec_schemes: Dict[str, Any] = components.get("securitySchemes", {}) or {}
        if not any(
            isinstance(v, dict)
            and v.get("type") == "http"
            and v.get("scheme") == "bearer"
            for v in sec_schemes.values()
        ):
            sec_schemes.setdefault(
                "BearerAuth",
                {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
            )
            components.setdefault("securitySchemes", sec_schemes)
            spec.setdefault("components", components)

        paths: Dict[str, Any] = spec.get("paths", {}) or {}
        if not paths:
            return

        # Helper to test if a parameter dict is an Authorization header
        def _is_inline_auth_param(p: Dict[str, Any]) -> bool:
            if not isinstance(p, dict):
                return False
            if p.get("in") != "header":
                return False
            name = str(p.get("name", ""))
            if name.lower() != "authorization":
                return False
            xp = p.get("x-prefix") or p.get("x_prefix")
            # Accept either explicit bearer prefix or any Authorization header
            return xp is None or str(xp).strip().lower().startswith("bearer")

        # Iterate operations
        methods = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
        for path_item in paths.values():
            if not isinstance(path_item, dict):
                continue
            for method, op in list(path_item.items()):
                if method.lower() not in methods or not isinstance(op, dict):
                    continue

                params = op.get("parameters", []) or []
                new_params = []
                found_auth = False
                for p in params:
                    if (
                        isinstance(p, dict)
                        and "$ref" in p
                        and isinstance(p["$ref"], str)
                    ):
                        ref = p["$ref"]
                        # Normalize possible refs to components.parameters
                        if ref.startswith("#/components/parameters/"):
                            key = ref.split("/")[-1]
                            if key in auth_param_keys:
                                found_auth = True
                                continue  # drop this param
                    # Check inline param shape
                    if isinstance(p, dict) and _is_inline_auth_param(p):
                        found_auth = True
                        continue  # drop inline auth param
                    new_params.append(p)

                if found_auth:
                    # Remove the auth param; allow operation to inherit root-level default
                    if new_params:
                        op["parameters"] = new_params
                    else:
                        op.pop("parameters", None)
                else:
                    # No auth header existed originally; explicitly opt-out of default
                    # unless operation already specifies security
                    if "security" not in op:
                        op["security"] = []

        # If we removed all uses of a global auth parameter, we can optionally keep it for docs
        # but no need to delete it here.

    def get_merged_spec(self) -> Dict[str, Any]:
        """Merge all individual API specs into a single spec."""
        all_specs = self.get_all_merged_specs()

        # Start with a base structure for the merged spec
        merged_spec: Dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {
                "title": "Linode API",
                "version": "1.0.0",
                "description": "The Linode API v4 OpenAPI specification.",
            },
            "servers": [{"url": "https://api.linode.com"}],
            "paths": {},
            "components": {},
            "security": [],
            "$defs": {},  # for schema components
        }

        all_security_schemes = []

        if not all_specs:
            # Clean up empty keys before returning
            if not merged_spec["components"]:
                del merged_spec["components"]
            if not merged_spec["$defs"]:
                del merged_spec["$defs"]
            return merged_spec

        # Merge all specs
        for api_name, spec in all_specs.items():
            if not spec:
                continue

            # Collect security requirements
            if "security" in spec:
                all_security_schemes.extend(spec["security"])

            # Prefix all operationIds with the API name and filter paths
            if "paths" in spec:
                # First, apply include/exclude filtering
                filtered_paths = self._filter_paths_by_operation_id(
                    api_name, spec["paths"]
                )

                # Then prefix operationIds
                for path, path_item in filtered_paths.items():
                    for method, operation in path_item.items():
                        if isinstance(operation, dict) and "operationId" in operation:
                            operation["operationId"] = (
                                f"{api_name}_{operation['operationId']}"
                            )

                merged_spec.setdefault("paths", {}).update(filtered_paths)

            if "components" in spec:
                # Schemas go into $defs
                if "schemas" in spec["components"]:
                    deep_merge(
                        merged_spec.setdefault("$defs", {}),
                        spec["components"]["schemas"],
                    )

                # Other components go into components
                for component_type, components in spec["components"].items():
                    if component_type != "schemas":
                        deep_merge(
                            merged_spec.setdefault("components", {}).setdefault(
                                component_type, {}
                            ),
                            components,
                        )

            if "$defs" in spec:
                deep_merge(merged_spec.setdefault("$defs", {}), spec["$defs"])

        # Add unique security schemes to the merged spec
        if all_security_schemes:
            unique_schemes = []
            seen = set()
            for scheme in all_security_schemes:
                # Convert dict to a hashable tuple of tuples
                scheme_tuple = tuple(sorted((k, tuple(v)) for k, v in scheme.items()))
                if scheme_tuple not in seen:
                    unique_schemes.append(scheme)
                    seen.add(scheme_tuple)
            merged_spec["security"] = unique_schemes

        # Update all schema references to point to $defs
        merged_spec = _update_references(merged_spec)

        # Clean up empty keys before returning
        if not merged_spec.get("components"):
            merged_spec.pop("components", None)
        if not merged_spec.get("$defs"):
            merged_spec.pop("$defs", None)

        return merged_spec

    def _apply_autogen_operation_ids(self, spec: Dict[str, Any]) -> None:
        """Generate operationId values for operations that lack them.

        Rules:
        - GET on a path ending in a parameter -> get{CamelCase(basename)},
          otherwise list{CamelCase(basename)}.
        - Other methods -> {verb}{CamelCase(basename)}.
        - Basename is the last non-parameter path segment
          (e.g. /a/b/{id} -> b).
        - Uniqueness is ensured by appending numeric suffixes.
        """
        paths = spec.get("paths")
        if not isinstance(paths, dict):
            return

        def camel(s: str) -> str:
            parts = re.split(r"[^A-Za-z0-9]+", s)
            return "".join(p.capitalize() for p in parts if p)

        def basename_and_param_flag(p: str) -> tuple[str, bool]:
            segs = [seg for seg in p.split("/") if seg]
            if not segs:
                return ("Root", False)
            last = segs[-1]
            is_param = bool(re.match(r"^{.+}$", last))
            if is_param:
                base = segs[-2] if len(segs) >= 2 else "Root"
            else:
                base = last
            return (base, is_param)

        methods_allowed = {"get", "put", "post", "delete", "patch", "options", "head"}

        existing = set()
        for methods in paths.values():
            if not isinstance(methods, dict):
                continue
            for op in methods.values():
                if isinstance(op, dict) and op.get("operationId"):
                    existing.add(op["operationId"])

        for p, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, op in methods.items():
                if method.lower() not in methods_allowed or not isinstance(op, dict):
                    continue
                if op.get("operationId"):
                    continue

                base, ends_with_param = basename_and_param_flag(p)
                if method.lower() == "get":
                    candidate = (
                        f"get{camel(base)}" if ends_with_param else f"list{camel(base)}"
                    )
                else:
                    candidate = f"{method.lower()}{camel(base)}"

                if candidate in existing:
                    i = 2
                    while f"{candidate}{i}" in existing:
                        i += 1
                    candidate = f"{candidate}{i}"

                op["operationId"] = candidate
                existing.add(candidate)

    # ECMAScript named group syntax "(?<name>" that Python's regex validator
    # rejects; must not touch lookbehinds "(?<=" / "(?<!".
    _JS_NAMED_GROUP = re.compile(r"\(\?<(?![=!])")

    def _sanitize_schema_quirks(self, node: Any) -> None:
        """Fix common spec authoring bugs in place.

        - `type: file` is Swagger 2.0 syntax that some specs declaring
          openapi 3.0 still use. OpenAPI 3 expresses binary payloads as
          string/binary.
        - `pattern` regexes with ECMAScript named groups `(?<name>...)`:
          JSON Schema validators built on Python's re require
          `(?P<name>...)`.
        - Boolean `exclusiveMaximum`/`exclusiveMinimum` are
          JSON Schema draft-4 syntax; tool schemas are validated as 2020-12
          where they must be numeric. Converting to the numeric form would
          break the OpenAPI 3.0 document parse (a catch-22 between the two
          dialects), so drop the boolean and keep the inclusive bound.
        - Swagger 2.0 `collectionFormat` left inside OpenAPI 3 array schemas
          is not a JSON Schema keyword and would leak into tool
          input schemas; OpenAPI 3 expresses it with parameter `style`.
        """
        if isinstance(node, dict):
            if node.get("type") == "file":
                node["type"] = "string"
                node["format"] = "binary"

            pattern = node.get("pattern")
            if isinstance(pattern, str) and "(?<" in pattern:
                node["pattern"] = self._JS_NAMED_GROUP.sub("(?P<", pattern)

            for exclusive in ("exclusiveMaximum", "exclusiveMinimum"):
                if isinstance(node.get(exclusive), bool):
                    node.pop(exclusive)

            if isinstance(node.get("collectionFormat"), str) and "items" in node:
                node.pop("collectionFormat")

            for value in node.values():
                self._sanitize_schema_quirks(value)
        elif isinstance(node, list):
            for item in node:
                self._sanitize_schema_quirks(item)

    def get_provider_spec(self, api_name: str) -> Optional[Dict[str, Any]]:
        """Load one API family's spec, ready for provider registration.

        Applies the configured include/exclude operationId filters but leaves
        operationIds and component schemas untouched: per-family namespacing
        is handled at provider registration, not by rewriting the spec.
        """
        spec = self.load_merged_spec(api_name)
        if not spec:
            return None
        spec = _sanitize_unicode(spec)
        if "paths" in spec:
            spec["paths"] = self._filter_paths_by_operation_id(api_name, spec["paths"])
            _add_filter_header(spec)
        return spec

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        # Write then rename so a concurrent reader never sees a partial file.
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        tmp.replace(path)
        # Drop the YAML cache written by earlier versions.
        path.with_suffix(".yaml").unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path) -> Optional[Any]:
        try:
            with open(path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def get_merged_spec_path(self, api_name: str) -> Path:
        """Get the path to the merged spec file."""
        return self.cache_dir / f"{api_name}-merged.json"

    def save_merged_spec(self, api_name: str, spec: Dict[str, Any]) -> None:
        """Save a merged specification to the cache."""
        path = self.get_merged_spec_path(api_name)
        self._write_json(path, spec)
        self._save_meta(f"{api_name}-merged", {"cache_version": CACHE_VERSION})
        logger.info(f"Saved merged spec for {api_name} to {path}")

    def load_merged_spec(self, api_name: str) -> Optional[Dict[str, Any]]:
        """Load a merged specification from the cache."""
        return self._read_json(self.get_merged_spec_path(api_name))

    def get_cached_spec_path(self, spec_key: str) -> Path:
        """Get the path to a cached specification file."""
        return self.cache_dir / f"{spec_key}.json"

    def save_cached_spec(self, spec_key: str, spec: Dict[str, Any]) -> None:
        """Save a fetched specification to the cache."""
        path = self.get_cached_spec_path(spec_key)
        self._write_json(path, spec)
        logger.info(f"Cached spec {spec_key} to {path}")

    def load_cached_spec(self, spec_key: str) -> Optional[Dict[str, Any]]:
        """Load a cached specification."""
        path = self.get_cached_spec_path(spec_key)
        spec = self._read_json(path)
        if spec is not None:
            logger.warning(f"Using cached spec for {spec_key} from {path}")
            return spec
        logger.error(f"No cached spec found for {spec_key}")
        return None

    def _meta_path(self, spec_key: str) -> Path:
        return self.cache_dir / f"{spec_key}.meta.json"

    def _load_meta(self, spec_key: str) -> Dict[str, Any]:
        return self._read_json(self._meta_path(spec_key)) or {}

    def _save_meta(self, spec_key: str, meta: Dict[str, Any]) -> None:
        with open(self._meta_path(spec_key), "w") as f:
            json.dump(meta, f, indent=2)

    def get_all_merged_specs(self) -> Dict[str, Dict[str, Any]]:
        """Load all merged specifications from the cache."""
        specs = {}
        for api_name in self.config.apis.keys():
            spec = self.load_merged_spec(api_name)
            if spec:
                specs[api_name] = spec
        return specs

    def _filter_paths_by_operation_id(
        self, api_name: str, paths: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Filter paths based on include/exclude patterns for operation IDs."""
        api_config = self.config.get_api_config(api_name)
        if not api_config or (not api_config.include and not api_config.exclude):
            return paths

        import re

        filtered_paths = {}

        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue

            filtered_path_item = {}
            for method, operation in path_item.items():
                if not isinstance(operation, dict) or "operationId" not in operation:
                    filtered_path_item[method] = operation
                    continue

                operation_id = operation["operationId"]

                # Check include patterns (if any)
                if api_config.include:
                    included = False
                    for pattern in api_config.include:
                        if re.search(pattern, operation_id, re.IGNORECASE):
                            included = True
                            break
                    if not included:
                        continue

                # Check exclude patterns (if any)
                if api_config.exclude:
                    excluded = False
                    for pattern in api_config.exclude:
                        if re.search(pattern, operation_id, re.IGNORECASE):
                            excluded = True
                            break
                    if excluded:
                        continue

                # Operation passed all filters
                filtered_path_item[method] = operation

            # Only include path if it has at least one operations
            if filtered_path_item:
                filtered_paths[path] = filtered_path_item

        return filtered_paths

    def has_all_cached_specs(self) -> bool:
        """Check if all enabled APIs have cached merged specifications."""
        for api_name, api_config in self.config.apis.items():
            if api_config.enabled:
                merged_spec_path = self.get_merged_spec_path(api_name)
                if not merged_spec_path.exists():
                    return False
                meta = self._load_meta(f"{api_name}-merged")
                if meta.get("cache_version") != CACHE_VERSION:
                    logger.info(f"Cached {api_name} spec is from an older version")
                    return False
        return True
