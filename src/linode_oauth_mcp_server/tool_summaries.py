"""Compact tool descriptions for the search results the model reads.

Linode's generated tools carry very large schemas: response schemas reach
40K characters and create operations have dozens of documented fields. The
default search output includes all of it, so a 15-result search returned
about 250K characters. These helpers summarize each match in a few lines and
leave full input schemas to an explicit lookup.
"""

import re
from typing import Any, Dict, List, Sequence

from fastmcp.tools.base import Tool

OAUTH_SCOPES_META = "linode/oauthScopes"
_DESCRIPTION_LIMIT = 300
_PARAM_DESCRIPTION_LIMIT = 100
_ENUM_LIMIT = 6
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
# Linode's docs prefix descriptions with markers like __Filterable__.
_MARKER = re.compile(r"__([A-Za-z -]+)__\s*")


def _plain(text: str) -> str:
    text = _LINK.sub(r"\1", text or "")
    text = _MARKER.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "…"


def _first_paragraph(text: str) -> str:
    return _plain((text or "").strip().split("\n\n", 1)[0])


def _first_sentence(text: str) -> str:
    plain = _plain(text)
    match = re.match(r"(.+?[.!?])(\s|$)", plain)
    return match.group(1) if match else plain


def _type_of(schema: Dict[str, Any]) -> str:
    if "enum" in schema:
        values = [v for v in schema["enum"] if v is not None]
        shown = " | ".join(
            repr(v) if not isinstance(v, str) else v for v in values[:_ENUM_LIMIT]
        )
        if len(values) > _ENUM_LIMIT:
            shown += " | …"
        return f"enum: {shown}"
    for key in ("anyOf", "oneOf"):
        if key in schema:
            branches = list(dict.fromkeys(_type_of(b) for b in schema[key]))
            return " | ".join(branches)
    kind = schema.get("type", "any")
    if isinstance(kind, list):
        kind = " | ".join(k for k in kind if k != "null") or "null"
    if kind == "array":
        return f"array of {_type_of(schema.get('items') or {})}"
    if kind == "object" and schema.get("properties"):
        return "object {" + ", ".join(schema["properties"]) + "}"
    return str(kind)


def _parameter_lines(schema: Dict[str, Any]) -> List[str]:
    required = set(schema.get("required") or [])
    lines = []
    for name, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        flag = ", required" if name in required else ""
        line = f"- `{name}` ({_type_of(prop)}{flag})"
        description = _truncate(
            _first_sentence(prop.get("description", "")), _PARAM_DESCRIPTION_LIMIT
        )
        if description:
            line += f": {description}"
        lines.append(line)
    return lines


def _access(tool: Tool) -> str:
    annotations = tool.annotations
    if annotations is None:
        return ""
    if annotations.read_only_hint:
        return "read-only"
    if annotations.destructive_hint:
        return "destructive write"
    return "write"


def summarize_tools(tools: Sequence[Tool]) -> str:
    """Render search matches as compact markdown."""
    if not tools:
        return "No tools matched the query."
    blocks = [
        "Call a tool with call_tool. For a parameter's full schema, call "
        "get_tool_schema with the tool name."
    ]
    for tool in tools:
        title = tool.title or (tool.annotations.title if tool.annotations else None)
        lines = [f"### {tool.name}" + (f" — {title}" if title else "")]
        description = _truncate(
            _first_paragraph(tool.description or ""), _DESCRIPTION_LIMIT
        )
        if description:
            lines.append(description)
        facts = [_access(tool)]
        scopes = (tool.meta or {}).get(OAUTH_SCOPES_META)
        if scopes:
            facts.append("scopes: " + ", ".join(scopes))
        facts = [f for f in facts if f]
        if facts:
            lines.append("; ".join(facts).capitalize())
        lines.extend(_parameter_lines(tool.parameters or {}))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def strip_schema(node: Any) -> Any:
    """Drop vendor extensions and examples, which the model does not need."""
    if isinstance(node, dict):
        return {
            key: strip_schema(value)
            for key, value in node.items()
            if not key.startswith("x-") and key not in ("example", "examples")
        }
    if isinstance(node, list):
        return [strip_schema(item) for item in node]
    return node
