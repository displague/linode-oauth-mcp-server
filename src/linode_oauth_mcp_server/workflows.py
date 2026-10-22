"""Arazzo workflows as MCP tools.

Each workflow in an Arazzo 1.0 document becomes a ``workflow_<workflowId>``
tool. Steps run the server's own Linode API tools, so they share its auth,
OAuth scopes, sign-in and response handling. A workflow tool declares the
union of its steps' scopes, so sign-in happens once, before the first step.

Supported: ``operationId`` steps, parameters and ``requestBody.payload`` with
runtime expressions, ``simple`` and ``regex`` criteria, step outputs,
``onSuccess``/``onFailure`` actions (``end``, ``goto`` and ``retry`` with
``retryAfter``/``retryLimit``, which is how a step polls), workflow-level
default actions and workflow outputs. A document that uses anything else is
rejected when it is loaded rather than misbehaving when it runs.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import yaml
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import Tool, ToolResult
from mcp_types import TextContent, ToolAnnotations
from pydantic import PrivateAttr

from .openapi_annotations import ANNOTATIONS_EXTENSION
from .progress import Progress, progress_reporter
from .tool_summaries import OAUTH_SCOPES_META

logger = logging.getLogger(__name__)
WORKFLOW_TOOL_PREFIX = "workflow_"
# Guards against goto cycles that never end; polling is bounded by retryLimit
MAX_STEP_RUNS = 1000
# Waits between retries; tests replace it
_sleep = asyncio.sleep

# Responses received while a workflow step runs, for its criteria and outputs
_responses: ContextVar[list[httpx2.Response] | None] = ContextVar(
    "workflow_responses", default=None
)


async def record_response(response: httpx2.Response) -> None:
    """httpx response hook: keep the API response for the running step."""
    recorded = _responses.get()
    if recorded is not None:
        recorded.append(response)


class WorkflowSpecError(ValueError):
    """An Arazzo document uses something this executor doesn't support."""


# --- Runtime expressions -------------------------------------------------

_EMBEDDED = re.compile(r"\{(\$[^{}]+)\}")


@dataclass
class _Response:
    status: int | None
    headers: dict[str, str]
    body: Any


@dataclass
class _Scope:
    inputs: dict[str, Any]
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    response: _Response | None = None


def _pointer(value: Any, pointer: str) -> Any:
    """Resolve a JSON Pointer; a missing member resolves to None."""
    if pointer in ("", "/"):
        return value
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value.get(token)
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            return None
    return value


def evaluate(expression: str, scope: _Scope) -> Any:
    """Evaluate one runtime expression such as ``$response.body#/status``."""
    base, _, pointer = expression.partition("#")
    if base == "$statusCode":
        return scope.response.status if scope.response else None
    if base == "$response.body":
        value = scope.response.body if scope.response else None
    elif base.startswith("$response.header."):
        name = base.removeprefix("$response.header.").lower()
        return scope.response.headers.get(name) if scope.response else None
    elif base.startswith("$inputs."):
        value = _dotted(scope.inputs, base.removeprefix("$inputs."))
    elif base.startswith("$steps."):
        step_id, _, rest = base.removeprefix("$steps.").partition(".outputs.")
        if not rest:
            raise WorkflowSpecError(f"Unsupported expression {expression!r}")
        value = _dotted(scope.steps.get(step_id, {}), rest)
    else:
        raise WorkflowSpecError(f"Unsupported expression {expression!r}")
    return _pointer(value, pointer) if pointer else value


def _dotted(value: Any, path: str) -> Any:
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def resolve(value: Any, scope: _Scope) -> Any:
    """Resolve expressions in a parameter value or request payload.

    A string that is a single expression keeps the value's type; expressions
    embedded in a longer string as ``{$...}`` are substituted as text.
    """
    if isinstance(value, dict):
        return {k: resolve(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, scope) for v in value]
    if not isinstance(value, str):
        return value
    if value.startswith("$") and not _EMBEDDED.search(value):
        return evaluate(value, scope)
    return _EMBEDDED.sub(lambda m: _text(evaluate(m.group(1), scope)), value)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _drop_none(value: Any) -> Any:
    """Leave out members whose expression resolved to nothing, such as an
    optional input the caller didn't give."""
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    return value


# --- Criteria ------------------------------------------------------------

_TOKEN = re.compile(
    r"\s*(?:(?P<op>&&|\|\||==|!=|<=|>=|<|>|!|\(|\))"
    r"|(?P<str>'(?:[^']|'')*')"
    r"|(?P<num>-?\d+(?:\.\d+)?)"
    r"|(?P<expr>\$[A-Za-z0-9_.\-#/~]+)"
    r"|(?P<word>true|false|null))"
)
_WORDS = {"true": True, "false": False, "null": None}


def _tokenize(condition: str) -> list[tuple[str, Any]]:
    tokens, pos = [], 0
    while pos < len(condition):
        match = _TOKEN.match(condition, pos)
        kind = match.lastgroup if match else None
        if match is None or kind is None:
            if condition[pos:].strip():
                raise WorkflowSpecError(
                    f"Can't parse condition {condition!r} at {condition[pos:]!r}"
                )
            break
        pos = match.end()
        text = match.group(kind)
        if kind == "str":
            tokens.append(("lit", text[1:-1].replace("''", "'")))
        elif kind == "num":
            tokens.append(("lit", float(text) if "." in text else int(text)))
        elif kind == "word":
            tokens.append(("lit", _WORDS[text]))
        else:
            tokens.append((kind, text))
    return tokens


def _compare(op: str, left: Any, right: Any) -> bool:
    # Arazzo compares strings case-insensitively
    if isinstance(left, str) and isinstance(right, str):
        left, right = left.lower(), right.lower()
    elif isinstance(left, str) and isinstance(right, (int, float)):
        left = _number(left)
    elif isinstance(right, str) and isinstance(left, (int, float)):
        right = _number(right)
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    try:
        return {
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
        }[op]
    except TypeError:
        return False


def _number(text: str) -> Any:
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return text


class _Condition:
    """Recursive-descent evaluator for Arazzo simple conditions."""

    def __init__(self, condition: str):
        self.condition = condition
        self.tokens = _tokenize(condition)

    def evaluate(self, scope: _Scope) -> bool:
        self.pos, self.scope = 0, scope
        result = self._or()
        if self.pos != len(self.tokens):
            raise WorkflowSpecError(f"Can't parse condition {self.condition!r}")
        return bool(result)

    def _peek(self) -> tuple[str, Any] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self, text: str) -> bool:
        if self._peek() == ("op", text):
            self.pos += 1
            return True
        return False

    def _or(self) -> Any:
        value = self._and()
        while self._take("||"):
            right = self._and()
            value = bool(value) or bool(right)
        return value

    def _and(self) -> Any:
        value = self._comparison()
        while self._take("&&"):
            right = self._comparison()
            value = bool(value) and bool(right)
        return value

    def _comparison(self) -> Any:
        value = self._unary()
        token = self._peek()
        if (
            token
            and token[0] == "op"
            and token[1] in ("==", "!=", "<", "<=", ">", ">=")
        ):
            self.pos += 1
            return _compare(token[1], value, self._unary())
        return value

    def _unary(self) -> Any:
        if self._take("!"):
            return not self._unary()
        if self._take("("):
            value = self._or()
            if not self._take(")"):
                raise WorkflowSpecError(f"Unbalanced parentheses in {self.condition!r}")
            return value
        token = self._peek()
        if token is None or token[0] == "op":
            raise WorkflowSpecError(f"Can't parse condition {self.condition!r}")
        self.pos += 1
        return token[1] if token[0] == "lit" else evaluate(token[1], self.scope)


@dataclass
class _Criterion:
    condition: str
    kind: str = "simple"
    context: str | None = None
    _parsed: Any = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "_Criterion":
        kind = data.get("type", "simple")
        if isinstance(kind, dict) or kind not in ("simple", "regex"):
            raise WorkflowSpecError(f"Unsupported criterion type {kind!r}")
        criterion = cls(data["condition"], kind, data.get("context"))
        if kind == "simple":
            criterion._parsed = _Condition(criterion.condition)
        else:
            if not criterion.context:
                raise WorkflowSpecError("A regex criterion needs a context")
            criterion._parsed = re.compile(criterion.condition)
        return criterion

    def check(self, scope: _Scope) -> bool:
        if self.kind == "simple":
            return self._parsed.evaluate(scope)
        value = evaluate(self.context or "", scope)
        text = value if isinstance(value, str) else json.dumps(value)
        return self._parsed.search(text) is not None


# --- Workflow model ------------------------------------------------------


@dataclass
class _Action:
    name: str
    kind: str
    step_id: str | None = None
    retry_after: float = 0
    retry_limit: int = 1
    criteria: list[_Criterion] = field(default_factory=list)

    @classmethod
    def parse(cls, data: dict[str, Any], retry: bool) -> "_Action":
        if "reference" in data:
            raise WorkflowSpecError("Reusable action references aren't supported")
        kind = data.get("type")
        allowed = ("end", "goto", "retry") if retry else ("end", "goto")
        if kind not in allowed:
            raise WorkflowSpecError(f"Unsupported action type {kind!r}")
        if "workflowId" in data:
            raise WorkflowSpecError(
                "Actions that run another workflow aren't supported"
            )
        if kind == "goto" and "stepId" not in data:
            raise WorkflowSpecError("A goto action needs a stepId")
        return cls(
            name=data.get("name", kind),
            kind=kind,
            step_id=data.get("stepId"),
            retry_after=float(data.get("retryAfter", 0)),
            retry_limit=int(data.get("retryLimit", 1)),
            criteria=[_Criterion.parse(c) for c in data.get("criteria") or []],
        )

    def matches(self, scope: _Scope) -> bool:
        return all(c.check(scope) for c in self.criteria)


@dataclass
class _Step:
    step_id: str
    operation_id: str
    description: str
    parameters: list[dict[str, Any]]
    payload: Any
    success: list[_Criterion]
    outputs: dict[str, str]
    on_success: list[_Action] | None
    on_failure: list[_Action] | None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "_Step":
        step_id = data.get("stepId")
        if not step_id:
            raise WorkflowSpecError("Every step needs a stepId")
        operation_id = data.get("operationId")
        if not operation_id:
            raise WorkflowSpecError(
                f"Step {step_id!r}: only operationId steps are supported"
            )
        # "$sourceDescriptions.<name>.<operationId>" names the operation too
        operation_id = operation_id.rsplit(".", 1)[-1]
        for parameter in data.get("parameters") or []:
            if "reference" in parameter or "name" not in parameter:
                raise WorkflowSpecError(
                    f"Step {step_id!r}: parameters need a name and value"
                )
        body = data.get("requestBody") or {}
        if "replacements" in body:
            raise WorkflowSpecError(
                f"Step {step_id!r}: requestBody replacements aren't supported"
            )
        payload = body.get("payload")
        if payload is not None and not isinstance(payload, dict):
            raise WorkflowSpecError(f"Step {step_id!r}: payload must be an object")
        on_success = data.get("onSuccess")
        on_failure = data.get("onFailure")
        return cls(
            step_id=step_id,
            operation_id=operation_id,
            description=data.get("description", ""),
            parameters=data.get("parameters") or [],
            payload=payload,
            success=[_Criterion.parse(c) for c in data.get("successCriteria") or []],
            outputs=data.get("outputs") or {},
            on_success=(
                None
                if on_success is None
                else [_Action.parse(a, retry=False) for a in on_success]
            ),
            on_failure=(
                None
                if on_failure is None
                else [_Action.parse(a, retry=True) for a in on_failure]
            ),
        )


@dataclass
class Workflow:
    workflow_id: str
    summary: str
    description: str
    inputs: dict[str, Any]
    steps: list[_Step]
    outputs: dict[str, str]
    success_actions: list[_Action]
    failure_actions: list[_Action]
    annotations: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Workflow":
        workflow_id = data.get("workflowId")
        if not workflow_id or not re.fullmatch(r"[A-Za-z0-9_\-]+", workflow_id):
            raise WorkflowSpecError(f"Invalid workflowId {workflow_id!r}")
        for unsupported in ("dependsOn", "parameters"):
            if data.get(unsupported):
                raise WorkflowSpecError(
                    f"Workflow {workflow_id!r}: {unsupported} isn't supported"
                )
        steps = [_Step.parse(s) for s in data.get("steps") or []]
        if not steps:
            raise WorkflowSpecError(f"Workflow {workflow_id!r} has no steps")
        workflow = cls(
            workflow_id=workflow_id,
            summary=data.get("summary", ""),
            description=data.get("description", ""),
            inputs=data.get("inputs") or {"type": "object", "properties": {}},
            steps=steps,
            outputs=data.get("outputs") or {},
            success_actions=[
                _Action.parse(a, retry=False) for a in data.get("successActions") or []
            ],
            failure_actions=[
                _Action.parse(a, retry=True) for a in data.get("failureActions") or []
            ],
            annotations=data.get(ANNOTATIONS_EXTENSION) or {},
        )
        ids = [s.step_id for s in steps]
        if len(set(ids)) != len(ids):
            raise WorkflowSpecError(f"Workflow {workflow_id!r} repeats a stepId")
        for step in steps:
            for action in (
                (step.on_success or [])
                + (step.on_failure or [])
                + workflow.success_actions
                + workflow.failure_actions
            ):
                if action.step_id and action.step_id not in ids:
                    raise WorkflowSpecError(
                        f"Workflow {workflow_id!r}: unknown step {action.step_id!r}"
                    )
        return workflow


def load_workflows(path: Path) -> list[Workflow]:
    """Parse the workflows of an Arazzo 1.0 document."""
    document = yaml.safe_load(path.read_text())
    if not isinstance(document, dict) or not str(document.get("arazzo", "")).startswith(
        "1."
    ):
        raise WorkflowSpecError(f"{path} is not an Arazzo 1.x document")
    return [Workflow.parse(w) for w in document.get("workflows") or []]


# --- Execution -----------------------------------------------------------

ToolLookup = Callable[[str], Awaitable[Tool | None]]


class WorkflowFailed(Exception):
    """A step failed with no action to recover; carries the run so far."""

    def __init__(self, message: str, trace: list[str], changed: bool):
        super().__init__(message)
        self.trace = list(trace)
        self.changed = changed


async def _call(tool: Tool, arguments: dict[str, Any]) -> _Response:
    """Run an API tool and return the HTTP response it got."""
    token = _responses.set([])
    try:
        try:
            await tool.run(arguments)
        except httpx2.HTTPStatusError:
            pass
        recorded = _responses.get() or []
    finally:
        _responses.reset(token)
    if not recorded:
        raise ToolError(f"{tool.name} made no API request")
    response = recorded[-1]
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return _Response(
        status=response.status_code,
        headers={k.lower(): v for k, v in response.headers.items()},
        body=body,
    )


async def run_workflow(
    workflow: Workflow,
    inputs: dict[str, Any],
    tools: dict[str, Tool],
    progress: Progress | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Run a workflow; return its outputs and a one-line-per-step trace."""
    scope = _Scope(inputs=inputs)
    index = {s.step_id: i for i, s in enumerate(workflow.steps)}
    trace = _Trace()
    retries: dict[str, int] = {}
    changed = False
    position = 0
    for _ in range(MAX_STEP_RUNS):
        if position >= len(workflow.steps):
            break
        step = workflow.steps[position]
        tool = tools[step.operation_id]
        arguments = _drop_none(
            {p["name"]: resolve(p.get("value"), scope) for p in step.parameters}
        )
        arguments.update(_drop_none(resolve(step.payload or {}, scope)))
        if not (tool.annotations and tool.annotations.read_only_hint):
            changed = True
        try:
            scope.response = await _call(tool, arguments)
        except ToolError:
            raise
        except Exception as exc:
            # The request couldn't be built or sent; criteria see no status
            logger.warning("Workflow step %s failed: %s", step.step_id, exc)
            scope.response = _Response(status=None, headers={}, body=str(exc))
        status = scope.response.status
        if step.success:
            succeeded = all(c.check(scope) for c in step.success)
        else:
            succeeded = status is not None and 200 <= status < 300
        trace.add(step, status, succeeded)

        if succeeded:
            retries.pop(step.step_id, None)
            scope.steps[step.step_id] = {
                name: evaluate(expr, scope) for name, expr in step.outputs.items()
            }
            actions = (
                workflow.success_actions if step.on_success is None else step.on_success
            )
            action = next((a for a in actions if a.matches(scope)), None)
            if action is None:
                position += 1
            elif action.kind == "end":
                break
            else:
                position = index[action.step_id or ""]
            if progress:
                await progress(f"{step.step_id}: done")
            continue

        actions = (
            workflow.failure_actions if step.on_failure is None else step.on_failure
        )
        action = next((a for a in actions if a.matches(scope)), None)
        if action is not None and action.kind == "retry":
            attempt = retries.get(step.step_id, 0) + 1
            if attempt <= action.retry_limit:
                retries[step.step_id] = attempt
                if progress:
                    await progress(
                        f"{step.step_id}: waiting "
                        f"(attempt {attempt} of {action.retry_limit})"
                    )
                await _sleep(action.retry_after)
                position = index[action.step_id] if action.step_id else position
                continue
            raise WorkflowFailed(
                f"Step {step.step_id!r} did not succeed after "
                f"{action.retry_limit} retries{_detail(scope.response)}",
                trace,
                changed,
            )
        if action is not None and action.kind == "goto":
            position = index[action.step_id or ""]
            continue
        raise WorkflowFailed(
            f"Step {step.step_id!r} ({step.operation_id}) failed"
            f"{_detail(scope.response)}",
            trace,
            changed,
        )
    else:
        raise WorkflowFailed(
            f"Workflow ran {MAX_STEP_RUNS} steps without finishing", trace, changed
        )
    outputs = {name: evaluate(expr, scope) for name, expr in workflow.outputs.items()}
    return outputs, list(trace)


class _Trace(list[str]):
    """One line per step run; repeated polls of a step fold into one line."""

    def add(self, step: _Step, status: int | None, ok: bool) -> None:
        line = f"{step.step_id}: {step.operation_id} -> {status or 'error'}"
        line += "" if ok else " (criteria not met)"
        if self and self._line == line:
            self._count += 1
            self[-1] = f"{line} ×{self._count}"
        else:
            self._line, self._count = line, 1
            self.append(line)


def _detail(response: _Response | None) -> str:
    if response is None:
        return ""
    body = (
        response.body if isinstance(response.body, str) else json.dumps(response.body)
    )
    if len(body) > 500:
        body = body[:500] + "…"
    return f": HTTP {response.status or 'error'} {body}"


# --- MCP tools ----------------------------------------------------------


class WorkflowTool(Tool):
    """An MCP tool that runs one Arazzo workflow."""

    _workflow: Workflow = PrivateAttr()
    _step_tools: dict[str, Tool] = PrivateAttr()

    @property
    def workflow(self) -> Workflow:
        return self._workflow

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        inputs = {
            name: schema["default"]
            for name, schema in (self._workflow.inputs.get("properties") or {}).items()
            if isinstance(schema, dict) and "default" in schema
        }
        inputs.update({k: v for k, v in arguments.items() if v is not None})
        progress = progress_reporter()
        try:
            outputs, trace = await run_workflow(
                self._workflow, inputs, self._step_tools, progress
            )
        except WorkflowFailed as exc:
            message = f"{exc}\n\nSteps run:\n" + "\n".join(
                f"- {line}" for line in exc.trace
            )
            if not exc.changed:
                # Nothing was changed, so the call can safely be retried
                # (for example after a new sign-in)
                raise ToolError(message) from exc
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=message
                        + "\n\nEarlier steps changed resources; check their "
                        "state before running the workflow again.",
                    )
                ],
                is_error=True,
            )
        text = yaml.dump(
            {"outputs": outputs, "steps": trace},
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
        return ToolResult(content=[TextContent(type="text", text=text)])


async def build_workflow_tool(workflow: Workflow, lookup: ToolLookup) -> WorkflowTool:
    """Resolve a workflow's operations to API tools and wrap it as a tool."""
    step_tools: dict[str, Tool] = {}
    for step in workflow.steps:
        tool = await lookup(step.operation_id)
        if tool is None:
            raise WorkflowSpecError(
                f"Workflow {workflow.workflow_id!r}: no API tool for "
                f"operation {step.operation_id!r}"
            )
        step_tools[step.operation_id] = tool
    scopes = sorted(
        {
            scope
            for tool in step_tools.values()
            for scope in (tool.meta or {}).get(OAUTH_SCOPES_META, ())
        }
    )
    hints = [t.annotations or ToolAnnotations() for t in step_tools.values()]
    hint_values: dict[str, Any] = {
        "title": workflow.summary or None,
        "read_only_hint": all(h.read_only_hint for h in hints),
        "destructive_hint": any(h.destructive_hint for h in hints),
        "idempotent_hint": False,
        "open_world_hint": True,
        # A workflow can say what its steps don't, such as downtime
        **ToolAnnotations.model_validate(workflow.annotations).model_dump(
            exclude_none=True
        ),
    }
    annotations = ToolAnnotations.model_validate(hint_values)
    steps = "\n".join(
        f"{i}. {s.step_id}: {s.operation_id}"
        + (f" — {s.description.strip()}" if s.description else "")
        for i, s in enumerate(workflow.steps, 1)
    )
    description = "\n\n".join(
        part
        for part in (
            workflow.summary,
            workflow.description.strip(),
            f"Steps:\n{steps}",
            "Runs as one call that waits for each step to finish; progress is "
            "reported per step.",
        )
        if part
    )
    parameters = {"type": "object", **workflow.inputs}
    tool = WorkflowTool(
        name=f"{WORKFLOW_TOOL_PREFIX}{workflow.workflow_id.replace('-', '_')}",
        title=workflow.summary or None,
        description=description,
        parameters=parameters,
        annotations=annotations,
        meta={OAUTH_SCOPES_META: scopes} if scopes else None,
        tags={"linode", "workflow"},
    )
    tool._workflow = workflow
    tool._step_tools = step_tools
    return tool
