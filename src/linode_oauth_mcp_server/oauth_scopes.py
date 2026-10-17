"""Map Linode API requests to the OAuth scopes their operations require.

Linode scopes are ``<resource>:<access>`` where access is ``read_only``,
``read_write`` or ``*``; a bare ``*`` grants everything. Linode's authorize
endpoint takes them comma-separated in the ``scopes`` query parameter.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
_ACCESS_RANK = {"read_only": 0, "read_write": 1, "*": 2}


def parse_scopes(value: str | None) -> frozenset[str]:
    """Parse a comma- or space-separated scope string."""
    return frozenset(s for s in re.split(r"[,\s]+", value or "") if s)


def serialize_scopes(scopes: Iterable[str]) -> str:
    return ",".join(sorted(scopes))


def _split(scope: str) -> tuple[str, str]:
    resource, _, access = scope.partition(":")
    return resource, access


def scopes_satisfy(granted: Iterable[str], required: Iterable[str]) -> bool:
    """Whether ``granted`` covers every scope in ``required``."""
    granted = set(granted)
    if "*" in granted:
        return True
    best: dict[str, int] = {}
    for scope in granted:
        resource, access = _split(scope)
        best[resource] = max(best.get(resource, -1), _ACCESS_RANK.get(access, -1))
    for scope in required:
        resource, access = _split(scope)
        if best.get(resource, -1) < _ACCESS_RANK.get(access, 99):
            return False
    return True


def merge_scopes(*groups: Iterable[str]) -> frozenset[str]:
    """Union scope sets, keeping only the strongest access per resource."""
    best: dict[str, str] = {}
    for scope in (s for group in groups for s in group):
        if scope == "*":
            return frozenset({"*"})
        resource, access = _split(scope)
        current = best.get(resource)
        if current is None or _ACCESS_RANK.get(access, -1) > _ACCESS_RANK.get(
            current, -1
        ):
            best[resource] = access
    return frozenset(f"{resource}:{access}" for resource, access in best.items())


@dataclass(frozen=True)
class AuthRequirement:
    """Credentials an operation needs.

    ``public`` operations need no token. Otherwise ``scopes`` lists the OAuth
    scopes to request; it is empty when the operation only names personal
    access tokens, in which case any available token is sent as-is.
    """

    public: bool = False
    scopes: frozenset[str] = frozenset()


class OperationScopes:
    """Look up an operation's auth requirement by HTTP method and request path."""

    def __init__(self, spec: dict[str, Any]):
        root_security = spec.get("security")
        routes: list[tuple[int, int, str, re.Pattern, AuthRequirement]] = []
        for template, path_item in (spec.get("paths") or {}).items():
            if not isinstance(path_item, dict):
                continue
            pattern = re.compile(
                "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "/?$"
            )
            params = template.count("{")
            for method, op in path_item.items():
                if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                    continue
                security = op.get("security", root_security)
                # Prefer literal segments over templated ones, e.g. /linode/types
                # over /linode/{linodeId}.
                routes.append(
                    (
                        params,
                        -len(template),
                        method.upper(),
                        pattern,
                        self._requirement(security),
                    )
                )
        routes.sort(key=lambda r: (r[0], r[1]))
        self._routes = routes

    @staticmethod
    def _requirement(security: list[dict[str, list[str]]] | None) -> AuthRequirement:
        if security is None:
            return AuthRequirement()
        if not security or any(not req for req in security):
            return AuthRequirement(public=True)
        for req in security:
            if "oauth" in req:
                return AuthRequirement(scopes=frozenset(req["oauth"]))
        return AuthRequirement()

    def requirement(self, method: str, path: str) -> AuthRequirement | None:
        method = method.upper()
        for _, _, route_method, pattern, requirement in self._routes:
            if route_method == method and pattern.match(path):
                return requirement
        return None
