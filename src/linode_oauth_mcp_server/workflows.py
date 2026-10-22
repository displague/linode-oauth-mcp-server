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
import math
import os
import re
import time
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

from . import resume
from .openapi_annotations import ANNOTATIONS_EXTENSION
from .progress import Progress, progress_reporter
from .rate_limits import NOT_APPLIED_STATUSES, parse_retry_after
from .tool_summaries import OAUTH_SCOPES_META

logger = logging.getLogger(__name__)
WORKFLOW_TOOL_PREFIX = "workflow_"
# Guards against goto cycles that never end; polling is bounded by retryLimit
# and the step deadline
MAX_STEP_RUNS = 1000
# Seconds a step may spend waiting out rate limits, beyond the polling time
# its retry action allows (retryAfter × retryLimit). Linode's rate-limit
# window is one minute.
RATE_LIMIT_ALLOWANCE = 120.0
# Wait for a rate-limited step when the response gives no Retry-After
RATE_LIMIT_DEFAULT_WAIT = 5.0
# Seconds a workflow call waits for its steps before it pauses with a resume
# token. Most MCP clients give up on a request after 60 seconds.
DEFAULT_MAX_WAIT = 45.0
# Workflow tool arguments that aren't workflow inputs
RESUME_ARGUMENT = "resume"
MAX_WAIT_ARGUMENT = "max_wait_s"
# Waits between retries and the clock deadlines are measured on; tests
# replace them
_sleep = asyncio.sleep
_clock = time.monotonic

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
    # Why the step is safe to run again, when nothing in the workflow shows it
    replay_safe: str | None = None
    # The step as written, for the revision resume tokens check
    source: dict[str, Any] = field(default_factory=dict, repr=False)

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
            replay_safe=data.get("x-replay-safe"),
            source=data,
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
    source: dict[str, Any] = field(default_factory=dict, repr=False)

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
            source=data,
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


@dataclass(frozen=True)
class ResumePoint:
    """A step a run can stop before and continue from with a resume token."""

    # The inputs and step outputs the rest of the run uses
    inputs: frozenset[str]
    outputs: frozenset[tuple[str, str]]
    # Hash of the steps the rest of the run can reach, so a token is
    # refused once any of them changes
    revision: str


@dataclass
class Checkpoint:
    """Where a run stopped, and what it needs to go on from there."""

    step_id: str
    inputs: dict[str, Any]
    steps: dict[str, dict[str, Any]]
    # Polling retries used so far, by stepId
    retries: dict[str, int] = field(default_factory=dict)
    # Whether an earlier step changed resources
    changed: bool = False
    # Wall-clock time the next step is due, for a call that resumes early
    due: float = 0.0


@dataclass
class RunResult:
    """A finished run's outputs, or where it paused."""

    trace: list[str]
    outputs: dict[str, Any] | None = None
    paused: Checkpoint | None = None
    # Seconds the run was about to wait when it paused
    retry_after: float = 0.0


async def run_workflow(
    workflow: Workflow,
    inputs: dict[str, Any],
    tools: dict[str, Tool],
    progress: Progress | None = None,
    *,
    start: Checkpoint | None = None,
    max_wait: float | None = None,
    pause_before: frozenset[str] = frozenset(),
) -> RunResult:
    """Run a workflow, or the rest of one from ``start``.

    With ``max_wait``, a run that would wait past that many seconds since it
    began pauses instead, before the next step it would run, if that step is
    in ``pause_before``; otherwise it keeps waiting.
    """
    began = _clock()
    index = {s.step_id: i for i, s in enumerate(workflow.steps)}
    if start is None:
        scope = _Scope(inputs=inputs)
        retries: dict[str, int] = {}
        changed = False
        position = 0
    else:
        scope = _Scope(inputs=start.inputs, steps=start.steps)
        retries = dict(start.retries)
        changed = start.changed
        position = index[start.step_id]
    trace = _Trace()
    # When each step first ran since it last succeeded, for its deadline
    started: dict[str, float] = {}

    def pause(at: int, wait: float) -> RunResult | None:
        step_id = workflow.steps[at].step_id
        if (
            max_wait is None
            or _clock() - began + wait <= max_wait
            or step_id not in pause_before
        ):
            return None
        checkpoint = Checkpoint(step_id, scope.inputs, scope.steps, retries, changed)
        return RunResult(list(trace), paused=checkpoint, retry_after=wait)

    for _ in range(MAX_STEP_RUNS):
        if position >= len(workflow.steps):
            break
        step = workflow.steps[position]
        tool = tools[step.operation_id]
        arguments = _drop_none(
            {p["name"]: resolve(p.get("value"), scope) for p in step.parameters}
        )
        arguments.update(_drop_none(resolve(step.payload or {}, scope)))
        started.setdefault(step.step_id, _clock())
        try:
            scope.response = await _call(tool, arguments)
        except ToolError:
            raise
        except Exception as exc:
            # The request couldn't be built or sent; criteria see no status
            logger.warning("Workflow step %s failed: %s", step.step_id, exc)
            scope.response = _Response(status=None, headers={}, body=str(exc))
        status = scope.response.status
        if status in NOT_APPLIED_STATUSES:
            # Rate limited or timed out: the API did nothing, so wait and
            # send the same request again, without using up the step's retries
            trace.add(step, status, False, note="rate limited")
            wait = _rate_limit_wait(scope.response, step, workflow)
            if paused := pause(position, wait):
                return paused
            elapsed = _clock() - started[step.step_id]
            if elapsed + wait > _step_deadline(step, workflow):
                raise WorkflowFailed(
                    f"Step {step.step_id!r} was still rate limited after "
                    f"{elapsed:.0f}s; the Linode API did not apply it and asked "
                    f"to wait {wait:.0f}s more{_detail(scope.response)}",
                    trace,
                    changed,
                )
            if progress:
                await progress(f"{step.step_id}: rate limited; waiting {wait:.0f}s")
            await _sleep(wait)
            continue
        if not (tool.annotations and tool.annotations.read_only_hint):
            changed = True
        if step.success:
            succeeded = all(c.check(scope) for c in step.success)
        else:
            succeeded = status is not None and 200 <= status < 300
        trace.add(step, status, succeeded)

        if succeeded:
            retries.pop(step.step_id, None)
            started.pop(step.step_id, None)
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

        actions = _failure_actions(step, workflow)
        action = next((a for a in actions if a.matches(scope)), None)
        if action is not None and action.kind == "retry":
            attempt = retries.get(step.step_id, 0) + 1
            elapsed = _clock() - started[step.step_id]
            if elapsed + action.retry_after > _step_deadline(step, workflow):
                raise WorkflowFailed(
                    f"Step {step.step_id!r} did not succeed within "
                    f"{elapsed:.0f}s{_detail(scope.response)}",
                    trace,
                    changed,
                )
            if attempt <= action.retry_limit:
                retries[step.step_id] = attempt
                position = index[action.step_id] if action.step_id else position
                if paused := pause(position, action.retry_after):
                    return paused
                if progress:
                    await progress(
                        f"{step.step_id}: waiting "
                        f"(attempt {attempt} of {action.retry_limit})"
                    )
                await _sleep(action.retry_after)
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
    return RunResult(list(trace), outputs=outputs)


class _Trace(list[str]):
    """One line per step run; repeated polls of a step fold into one line."""

    def add(
        self, step: _Step, status: int | None, ok: bool, note: str | None = None
    ) -> None:
        line = f"{step.step_id}: {step.operation_id} -> {status or 'error'}"
        if note:
            line += f" ({note})"
        elif not ok:
            line += " (criteria not met)"
        if self and self._line == line:
            self._count += 1
            self[-1] = f"{line} ×{self._count}"
        else:
            self._line, self._count = line, 1
            self.append(line)


def _failure_actions(step: _Step, workflow: Workflow) -> list[_Action]:
    return workflow.failure_actions if step.on_failure is None else step.on_failure


def _step_deadline(step: _Step, workflow: Workflow) -> float:
    """Seconds a step may take, from its first run until it succeeds."""
    polling = max(
        (
            a.retry_after * a.retry_limit
            for a in _failure_actions(step, workflow)
            if a.kind == "retry"
        ),
        default=0.0,
    )
    return polling + RATE_LIMIT_ALLOWANCE


def _rate_limit_wait(response: _Response, step: _Step, workflow: Workflow) -> float:
    """How long to wait before sending a rate-limited step again."""
    asked = parse_retry_after(response.headers.get("retry-after"))
    if asked is not None:
        return asked
    polls = [
        a.retry_after for a in _failure_actions(step, workflow) if a.kind == "retry"
    ]
    return max(polls, default=RATE_LIMIT_DEFAULT_WAIT) or RATE_LIMIT_DEFAULT_WAIT


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
    _resume_points: dict[str, ResumePoint] = PrivateAttr()

    @property
    def workflow(self) -> Workflow:
        return self._workflow

    @property
    def step_tools(self) -> dict[str, Tool]:
        return self._step_tools

    @property
    def resume_points(self) -> dict[str, ResumePoint]:
        return self._resume_points

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        arguments = {k: v for k, v in arguments.items() if v is not None}
        token = arguments.pop(RESUME_ARGUMENT, None)
        max_wait = arguments.pop(MAX_WAIT_ARGUMENT, None)
        try:
            max_wait = default_max_wait() if max_wait is None else float(max_wait)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"{MAX_WAIT_ARGUMENT} must be a number of seconds") from exc
        start = None
        if token is not None:
            if arguments:
                raise ToolError(
                    f"To continue a run, pass only {RESUME_ARGUMENT} (and "
                    f"optionally {MAX_WAIT_ARGUMENT}); its inputs are in the "
                    f"token. Also got: {', '.join(sorted(arguments))}"
                )
            start = self._checkpoint(token)
            inputs = start.inputs
        else:
            schema = self._workflow.inputs
            inputs = {
                name: prop["default"]
                for name, prop in (schema.get("properties") or {}).items()
                if isinstance(prop, dict) and "default" in prop
            }
            inputs.update(arguments)
            missing = [n for n in schema.get("required") or [] if n not in inputs]
            if missing:
                raise ToolError(f"Missing required inputs: {', '.join(missing)}")
        progress = progress_reporter()
        # A call that comes back before the next step is due waits for it, so
        # polling keeps to the step's interval
        wait = min(start.due - resume._now(), max_wait) if start else 0
        if start is not None and wait > 0:
            if progress:
                await progress(f"{start.step_id}: waiting {wait:.0f}s")
            await _sleep(wait)
            max_wait -= wait
        try:
            result = await run_workflow(
                self._workflow,
                inputs,
                self._step_tools,
                progress,
                start=start,
                max_wait=max_wait,
                pause_before=frozenset(self._resume_points),
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
        if result.paused is not None:
            return _text_result(self._paused(result.paused, result))
        return _text_result(
            {"status": "completed", "outputs": result.outputs, "steps": result.trace}
        )

    def _checkpoint(self, token: str) -> Checkpoint:
        """The checkpoint a resume token holds, if it's still good to use."""
        start_again = (
            "Run the workflow again with its inputs instead; its checks skip "
            "the work that is already done."
        )
        try:
            payload = resume.decode(token, resume.signing_key())
        except resume.ResumeTokenError as exc:
            raise ToolError(f"This resume token {exc}. {start_again}") from exc
        workflow_id = self._workflow.workflow_id
        if payload["wf"] != workflow_id:
            raise ToolError(
                f"This resume token is for the {payload['wf']} workflow, not "
                f"{workflow_id}."
            )
        point = self._resume_points.get(payload["at"])
        if point is None or point.revision != payload["rev"]:
            raise ToolError(
                f"The {workflow_id} workflow has changed since this resume token "
                f"was issued (in the steps from {payload['at']} on). {start_again}"
            )
        values = payload["vals"]
        return Checkpoint(
            step_id=payload["at"],
            inputs=values["inputs"],
            steps=values["steps"],
            retries=payload["retries"],
            changed=payload["changed"],
            due=payload["due"],
        )

    def _paused(self, checkpoint: Checkpoint, result: RunResult) -> dict[str, Any]:
        """The result of a run that paused: what's next and its resume token."""
        point = self._resume_points[checkpoint.step_id]
        # Carry only the values the rest of the run uses
        carried: dict[str, Any] = {
            "inputs": {
                name: checkpoint.inputs[name]
                for name in sorted(point.inputs)
                if name in checkpoint.inputs
            },
            "steps": {},
        }
        for step_id, name in sorted(point.outputs):
            outputs = checkpoint.steps.get(step_id) or {}
            if name in outputs:
                carried["steps"].setdefault(step_id, {})[name] = outputs[name]
        token = resume.encode(
            {
                "wf": self._workflow.workflow_id,
                "rev": point.revision,
                "at": checkpoint.step_id,
                "vals": carried,
                "retries": checkpoint.retries,
                "changed": checkpoint.changed,
                "due": round(resume._now() + result.retry_after, 1),
            },
            resume.signing_key(),
        )
        wait = math.ceil(result.retry_after)
        return {
            "status": "working",
            "next": checkpoint.step_id,
            "retry_after_s": wait,
            "message": (
                f"Still running. To continue, call {self.name} again in about "
                f"{wait} seconds with only {RESUME_ARGUMENT} set to the token "
                "below. The token holds the inputs and progress so far."
            ),
            "carried": carried,
            RESUME_ARGUMENT: token,
            "steps": result.trace,
        }


def _text_result(body: dict[str, Any]) -> ToolResult:
    text = yaml.dump(
        body, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    return ToolResult(content=[TextContent(type="text", text=text)])


def default_max_wait() -> float:
    """Seconds a workflow call waits before it pauses:
    ``LINODE_MCP_WORKFLOW_WAIT``, or 45."""
    try:
        return float(os.getenv("LINODE_MCP_WORKFLOW_WAIT", DEFAULT_MAX_WAIT))
    except ValueError:
        logger.warning("Ignoring invalid LINODE_MCP_WORKFLOW_WAIT")
        return DEFAULT_MAX_WAIT


async def build_workflow_tool(workflow: Workflow, lookup: ToolLookup) -> WorkflowTool:
    """Resolve a workflow's operations to API tools and wrap it as a tool."""
    # workflow_lint imports this module
    from .workflow_lint import operation_methods, resume_points

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
            "Waits for each step to finish and reports progress per step. A run "
            f"that would wait longer than {MAX_WAIT_ARGUMENT} seconds returns "
            f"status 'working' with a {RESUME_ARGUMENT} token instead; call "
            f"this tool again with only {RESUME_ARGUMENT} to continue.",
        )
        if part
    )
    tool = WorkflowTool(
        name=f"{WORKFLOW_TOOL_PREFIX}{workflow.workflow_id.replace('-', '_')}",
        title=workflow.summary or None,
        description=description,
        parameters=_parameters(workflow),
        annotations=annotations,
        meta={OAUTH_SCOPES_META: scopes} if scopes else None,
        tags={"linode", "workflow"},
    )
    tool._workflow = workflow
    tool._step_tools = step_tools
    tool._resume_points = resume_points(workflow, operation_methods(step_tools))
    return tool


def _parameters(workflow: Workflow) -> dict[str, Any]:
    """The tool's input schema: the workflow's inputs, plus how to continue.

    A call that continues a run passes only its resume token, so inputs
    aren't marked required in the schema; the tool checks them itself.
    """
    schema = dict(workflow.inputs)
    required = schema.pop("required", None) or []
    properties = dict(schema.get("properties") or {})
    for name in (RESUME_ARGUMENT, MAX_WAIT_ARGUMENT):
        if name in properties:
            raise WorkflowSpecError(
                f"Workflow {workflow.workflow_id!r}: input {name!r} is reserved"
            )
    for name in required:
        prop = properties.get(name)
        if isinstance(prop, dict):
            text = prop.get("description", "").strip()
            properties[name] = {
                **prop,
                "description": f"{text} Required unless resuming.".strip(),
            }
    properties[MAX_WAIT_ARGUMENT] = {
        "type": "number",
        "minimum": 0,
        "description": (
            "Seconds this call may wait for steps to finish before it returns "
            f"a {RESUME_ARGUMENT} token (default {default_max_wait():g})."
        ),
    }
    properties[RESUME_ARGUMENT] = {
        "type": "string",
        "description": (
            "Token from an earlier call's result that returned status "
            "'working'. Pass it alone to continue that run."
        ),
    }
    return {"type": "object", **schema, "properties": properties}
