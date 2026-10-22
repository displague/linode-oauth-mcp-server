"""Test the checks that workflows are safe to run again."""

import pytest

from linode_oauth_mcp_server.config import Config
from linode_oauth_mcp_server.workflow_lint import lint_workflow
from linode_oauth_mcp_server.workflows import Workflow, load_workflows


def _methods(workflow):
    # Linode operationIds start with their method
    return {s.operation_id: s.operation_id.split("-")[0] for s in workflow.steps}


def _lint(*steps):
    workflow = Workflow.parse({"workflowId": "w", "steps": list(steps)})
    return [(f.rule, f.step_id) for f in lint_workflow(workflow, _methods(workflow))]


def _step(step_id, operation, **extra):
    return {"stepId": step_id, "operationId": operation, **extra}


def _skip_to(target, condition="$response.body#/done == true", kind="goto"):
    action = {"name": "skip", "type": kind, "criteria": [{"condition": condition}]}
    if kind == "goto":
        action["stepId"] = target
    return action


def test_packaged_workflows_are_safe_to_run_again():
    for workflow in load_workflows(Config.load().workflow_paths()[0]):
        assert lint_workflow(workflow, _methods(workflow)) == []


def test_write_reached_from_the_start_is_flagged():
    assert _lint(_step("create", "post-thing")) == [("unguarded-write", "create")]


def test_write_after_an_unguarded_read_is_flagged():
    assert _lint(_step("read", "get-thing"), _step("create", "post-thing")) == [
        ("unguarded-write", "create")
    ]


def test_read_that_skips_the_write_guards_it():
    findings = _lint(
        _step("read", "get-thing", onSuccess=[_skip_to("after")]),
        _step("create", "post-thing"),
        _step("after", "get-thing"),
    )
    assert findings == []


def test_ending_the_run_from_the_response_guards_a_write():
    assert (
        _lint(
            _step("read", "get-thing", onSuccess=[_skip_to(None, kind="end")]),
            _step("create", "post-thing"),
        )
        == []
    )


def test_skip_that_doesnt_depend_on_the_response_is_no_guard():
    findings = _lint(
        _step("read", "get-thing", onSuccess=[_skip_to("after", "$inputs.x == 1")]),
        _step("create", "post-thing"),
        _step("after", "get-thing"),
    )
    assert findings == [("unguarded-write", "create")]


def test_skip_that_leads_back_to_the_write_is_no_guard():
    findings = _lint(
        _step("read", "get-thing", onSuccess=[_skip_to("create")]),
        _step("other", "put-thing"),
        _step("create", "post-thing"),
    )
    assert findings == [("unguarded-write", "create")]


def test_every_path_to_a_write_needs_a_guard():
    # The second read is reached by a goto from the start and doesn't guard
    findings = _lint(
        _step(
            "first",
            "get-thing",
            onSuccess=[_skip_to("after"), _skip_to("second", "$inputs.x == 1")],
        ),
        _step("second", "get-thing"),
        _step("create", "post-thing"),
        _step("after", "get-thing"),
    )
    assert findings == [("unguarded-write", "create")]


def test_idempotent_writes_and_marked_steps_are_not_flagged():
    assert _lint(_step("set", "put-thing")) == []
    assert _lint(_step("boot", "post-boot", **{"x-replay-safe": "no-op if on"})) == []


def test_delete_needs_404_handling():
    done = {"name": "gone", "type": "goto", "stepId": "after"}
    assert _lint(_step("delete", "delete-thing"), _step("after", "get-thing")) == [
        ("delete-without-404", "delete")
    ]
    handled = _step(
        "delete",
        "delete-thing",
        onFailure=[{**done, "criteria": [{"condition": "$statusCode == 404"}]}],
    )
    assert _lint(handled, _step("after", "get-thing")) == []


def test_using_values_read_before_a_delete_is_flagged():
    not_found = [
        {
            "name": "gone",
            "type": "end",
            "criteria": [{"condition": "$statusCode == 404"}],
        }
    ]
    findings = _lint(
        _step("read", "get-thing", outputs={"size": "$response.body#/size"}),
        _step("delete", "delete-thing", onFailure=not_found),
        _step(
            "create",
            "put-thing",
            requestBody={"payload": {"size": "$steps.read.outputs.size"}},
        ),
    )
    assert findings == [("break-before-make", "create")]


@pytest.mark.parametrize("rule_text", ["x-replay-safe", "Add a read step"])
def test_findings_explain_the_fix(rule_text):
    workflow = Workflow.parse(
        {"workflowId": "w", "steps": [_step("create", "post-thing")]}
    )
    [finding] = lint_workflow(workflow, _methods(workflow))
    assert rule_text in str(finding)
    assert str(finding).startswith("w/create: POST post-thing runs again")
