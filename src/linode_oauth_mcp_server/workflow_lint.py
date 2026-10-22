"""Check that a workflow is safe to run again from any of its read steps.

A run can be repeated from the start (a client re-sends a call whose
response stream broke) or, with resume tokens, from a read step part way
through. Either way, the writes after that point run again, so each write
must be safe to repeat. The checks are structural: they look at the shape of
the workflow, not what its conditions mean.

* ``unguarded-write``: a write that isn't idempotent (a POST, such as a
  create or a power action) can be reached from a read step, or from the
  start, without passing a read step whose action skips the write based on
  the response, such as "already offline: go to the resize". A step that is
  safe to repeat anyway can say why with ``x-replay-safe: <reason>``.
* ``delete-without-404``: a delete has no failure action for a 404, so
  running it again fails instead of finding the work done.
* ``break-before-make``: a step uses values read before an earlier delete.
  If the run stops in between, the deleted resource's details exist only in
  the run's state; create the replacement before deleting the original.

The same analysis says where a run can stop and continue later from a
resume token (``resume_points``): before a read step that is safe to run
again, carrying only the values the rest of the run uses.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from fastmcp.server.providers.openapi import OpenAPITool
from fastmcp.tools.base import Tool

from .workflows import ResumePoint, Workflow, _Action, _Step

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
IDEMPOTENT_METHODS = READ_METHODS | {"PUT", "DELETE"}
_STEP_OUTPUT = re.compile(r"\$steps\.([A-Za-z0-9_\-]+)\.outputs\.")
_NOT_FOUND = re.compile(r"\$statusCode\s*==\s*404\b")
_INPUT = re.compile(r"\$inputs\.([A-Za-z0-9_\-]+)")
_OUTPUT = re.compile(r"\$steps\.([A-Za-z0-9_\-]+)\.outputs\.([A-Za-z0-9_\-]+)")
# Step fields that don't change what a step does
_DOCUMENTATION = ("description", "summary")


@dataclass(frozen=True)
class Finding:
    rule: str
    workflow_id: str
    step_id: str
    message: str

    def __str__(self) -> str:
        return f"{self.workflow_id}/{self.step_id}: {self.message} [{self.rule}]"


def operation_methods(step_tools: dict[str, Tool]) -> dict[str, str]:
    """Each operation's HTTP method, from the API tools a workflow runs.

    A tool that isn't an OpenAPI tool counts as a POST, which is checked the
    most strictly.
    """
    return {
        operation_id: tool._route.method if isinstance(tool, OpenAPITool) else "POST"
        for operation_id, tool in step_tools.items()
    }


def lint_workflow(workflow: Workflow, methods: dict[str, str]) -> list[Finding]:
    """Find steps that aren't safe to run again.

    ``methods`` maps each step's operationId to its HTTP method.
    """
    graph = _Graph(workflow)
    method = _methods(workflow, methods)
    reads = _reads(workflow, method)
    findings: list[Finding] = []

    def finding(rule: str, step: _Step, message: str) -> None:
        findings.append(Finding(rule, workflow.workflow_id, step.step_id, message))

    for i, step in enumerate(workflow.steps):
        verb = method[step.step_id]
        unguarded = _unguarded(graph, i, reads, verb)
        if unguarded:
            names = ", ".join(
                "the start" if r is None else workflow.steps[r].step_id
                for r in unguarded
            )
            finding(
                "unguarded-write",
                step,
                f"{verb} {step.operation_id} runs again when the workflow is "
                f"repeated from {names}. Add a read step before it whose "
                "action skips it once it's done, or mark it x-replay-safe "
                "with the reason.",
            )
        if verb == "DELETE" and not any(
            _NOT_FOUND.search(c.condition)
            for a in graph.failure_actions(step)
            if a.kind in ("goto", "end")
            for c in a.criteria
        ):
            finding(
                "delete-without-404",
                step,
                "Running this delete again gets a 404. Add an onFailure goto or "
                "end action with $statusCode == 404 so that counts as done.",
            )
        if verb == "DELETE":
            read_before = {
                workflow.steps[j].step_id
                for j in graph.ancestors(i)
                if j in reads and workflow.steps[j].outputs
            }
            for later in sorted(graph.descendants(i)):
                used = _referenced_steps(workflow.steps[later]) & read_before
                if used:
                    finding(
                        "break-before-make",
                        workflow.steps[later],
                        f"Uses outputs of {', '.join(sorted(used))}, read before "
                        f"{step.step_id} deletes. Create the replacement before "
                        "deleting the original.",
                    )
    return findings


def resume_points(
    workflow: Workflow, methods: dict[str, str]
) -> dict[str, ResumePoint]:
    """The steps a run can safely stop before, by stepId.

    Each is a read step from which every write still to come is either safe
    to repeat or can be skipped by a read: running the rest again from there
    after a token is used twice, or after a long gap, does no harm.
    """
    graph = _Graph(workflow)
    method = _methods(workflow, methods)
    reads = _reads(workflow, method)
    unsafe = {
        r
        for i, step in enumerate(workflow.steps)
        for r in _unguarded(graph, i, reads, method[step.step_id])
    }
    points: dict[str, ResumePoint] = {}
    for i in sorted(reads - unsafe):
        remaining = sorted({i} | graph.descendants(i))
        text = repr(workflow.outputs) + "".join(
            _step_text(workflow.steps[j], graph) for j in remaining
        )
        points[workflow.steps[i].step_id] = ResumePoint(
            inputs=frozenset(_INPUT.findall(text)),
            outputs=frozenset(_OUTPUT.findall(text)),
            revision=_revision(workflow, remaining),
        )
    return points


def _methods(workflow: Workflow, methods: dict[str, str]) -> dict[str, str]:
    return {s.step_id: methods[s.operation_id].upper() for s in workflow.steps}


def _reads(workflow: Workflow, method: dict[str, str]) -> set[int]:
    return {
        i for i, s in enumerate(workflow.steps) if method[s.step_id] in READ_METHODS
    }


def _unguarded(graph: "_Graph", i: int, reads: set[int], verb: str) -> list[int | None]:
    """Where a run can be repeated from and run step ``i`` again unchecked."""
    if verb in IDEMPOTENT_METHODS or graph.workflow.steps[i].replay_safe:
        return []
    return [
        r
        for r in graph.resume_points_before(i, reads)
        if r is None or not graph.bypasses(r, i)
    ]


def _step_text(step: _Step, graph: "_Graph") -> str:
    """Everything in a step that can hold a runtime expression."""
    criteria = list(step.success) + [
        c
        for a in graph.success_actions(step) + graph.failure_actions(step)
        for c in a.criteria
    ]
    return (
        repr(step.parameters)
        + repr(step.payload)
        + repr(step.outputs)
        + "".join(f"{c.condition} {c.context or ''}" for c in criteria)
    )


def _revision(workflow: Workflow, remaining: list[int]) -> str:
    steps = workflow.steps
    material = {
        "steps": [
            {k: v for k, v in steps[j].source.items() if k not in _DOCUMENTATION}
            # The step a run falls through to matters as much as the step
            | {"next": steps[j + 1].step_id if j + 1 < len(steps) else None}
            for j in remaining
        ],
        "outputs": workflow.outputs,
        "successActions": workflow.source.get("successActions"),
        "failureActions": workflow.source.get("failureActions"),
    }
    text = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _reads_response(criterion) -> bool:
    text = (criterion.context or "") + criterion.condition
    return "$response" in text or "$statusCode" in text


def _referenced_steps(step: _Step) -> set[str]:
    text = repr(step.parameters) + repr(step.payload)
    return set(_STEP_OUTPUT.findall(text))


class _Graph:
    """Which step can run after which."""

    def __init__(self, workflow: Workflow):
        self.workflow = workflow
        self.index = {s.step_id: i for i, s in enumerate(workflow.steps)}
        count = len(workflow.steps)
        self.next: list[set[int]] = [set() for _ in range(count)]
        for i, step in enumerate(workflow.steps):
            success = self.success_actions(step)
            if not any(not a.criteria for a in success):
                # No action always applies, so the run can go on to the next step
                if i + 1 < count:
                    self.next[i].add(i + 1)
            for action in success + self.failure_actions(step):
                if action.kind == "goto":
                    self.next[i].add(self.index[action.step_id or ""])
                elif action.kind == "retry":
                    self.next[i].add(self.index.get(action.step_id or "", i))
        self.previous: list[set[int]] = [set() for _ in range(count)]
        for i, targets in enumerate(self.next):
            for j in targets:
                self.previous[j].add(i)

    def success_actions(self, step: _Step) -> list[_Action]:
        if step.on_success is None:
            return self.workflow.success_actions
        return step.on_success

    def failure_actions(self, step: _Step) -> list[_Action]:
        if step.on_failure is None:
            return self.workflow.failure_actions
        return step.on_failure

    def _walk(self, start: int, edges: list[set[int]]) -> set[int]:
        seen: set[int] = set()
        todo = list(edges[start])
        while todo:
            j = todo.pop()
            if j not in seen:
                seen.add(j)
                todo.extend(edges[j])
        return seen

    def descendants(self, i: int) -> set[int]:
        return self._walk(i, self.next)

    def ancestors(self, i: int) -> set[int]:
        return self._walk(i, self.previous)

    def resume_points_before(self, i: int, reads: set[int]) -> set[int | None]:
        """The nearest read steps (or the start, as None) a run reaches ``i`` from."""
        found: set[int | None] = set()
        seen = {i}
        todo = [i]
        while todo:
            j = todo.pop()
            if j == 0:
                # A run can start here and reach i without passing a read
                found.add(None)
            for k in self.previous[j]:
                if k in seen:
                    continue
                seen.add(k)
                if k in reads:
                    found.add(k)
                else:
                    todo.append(k)
        return found

    def bypasses(self, read: int, write: int) -> bool:
        """Whether a read step has an action that, judging by the response,
        sends the run somewhere the write can't be reached from."""
        step = self.workflow.steps[read]
        for action in self.success_actions(step) + self.failure_actions(step):
            if action.kind not in ("goto", "end"):
                continue
            if not any(_reads_response(c) for c in action.criteria):
                continue
            if action.kind == "end":
                return True
            target = self.index[action.step_id or ""]
            if target != write and write not in self.descendants(target):
                return True
        return False
