"""Test pausing workflows and continuing them from resume tokens."""

import base64
import json
import os
import time

import httpx2
import pytest
import yaml
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_workflows import SPEC, FakeLinode, _client, _resize_disk_args

from linode_oauth_mcp_server import resume, workflows
from linode_oauth_mcp_server.config import Config
from linode_oauth_mcp_server.main import LinodeMCPServer
from linode_oauth_mcp_server.workflow_lint import resume_points
from linode_oauth_mcp_server.workflows import Workflow, load_workflows

RESIZE = "workflow_resize_linode_disk"


@pytest.fixture
def no_wait(monkeypatch):
    async def sleep(_seconds):
        pass

    monkeypatch.setattr(workflows, "_sleep", sleep)


def _body(result):
    return yaml.safe_load(result.content[0].text)


async def _run_to_end(client, name, arguments, max_calls=500):
    """Call a workflow, then resume it with the same max_wait_s until it
    finishes; return each result."""
    wait = {"max_wait_s": arguments["max_wait_s"]} if "max_wait_s" in arguments else {}
    results = [_body(await client.call_tool(name, arguments))]
    while results[-1]["status"] == "working":
        assert len(results) < max_calls
        token = results[-1]["resume"]
        results.append(_body(await client.call_tool(name, {"resume": token, **wait})))
    return results


def _payload(token):
    data = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))


def _packaged():
    return {w.workflow_id: w for w in load_workflows(Config.load().workflow_paths()[0])}


def _methods(workflow):
    # Linode operationIds start with their HTTP method
    return {s.operation_id: s.operation_id.split("-")[0] for s in workflow.steps}


# --- Pausing and continuing ----------------------------------------------


async def test_resize_pauses_at_each_wait_and_resumes_to_the_end(monkeypatch, no_wait):
    api = FakeLinode()
    async with await _client(monkeypatch, api) as client:
        results = await _run_to_end(client, RESIZE, _resize_disk_args(max_wait_s=0))
    assert len(results) > 3
    first = results[0]
    assert first["next"] == "wait_offline"
    assert first["retry_after_s"] == 5
    # Only the values the rest of the run uses are carried
    assert first["carried"] == {
        "inputs": {"diskId": 5, "linodeId": 1, "size": 2000},
        "steps": {"check_status": {"initialStatus": "running"}},
    }
    assert results[-1]["outputs"] == {"initialStatus": "running", "size": 2000}
    # Each write ran once across all the calls
    assert [c for c in api.calls if c.startswith("POST")] == [
        "POST /linode/instances/1/shutdown",
        "POST /linode/instances/1/disks/5/resize",
        "POST /linode/instances/1/boot",
    ]
    assert api.status == "running"


async def test_runs_that_finish_within_max_wait_dont_pause(monkeypatch, no_wait):
    async with await _client(monkeypatch, FakeLinode()) as client:
        body = _body(await client.call_tool(RESIZE, _resize_disk_args()))
    assert body["status"] == "completed"
    assert "resume" not in body


async def test_a_token_used_twice_doesnt_repeat_writes(monkeypatch, no_wait):
    api = FakeLinode()
    async with await _client(monkeypatch, api) as client:
        results = await _run_to_end(client, RESIZE, _resize_disk_args(max_wait_s=0))
        token = next(r for r in results if r.get("next") == "wait_disk_ready")
        # The client re-sends a call whose response it lost
        again = _body(
            await client.call_tool(
                RESIZE, {"resume": token["resume"], "max_wait_s": 1000}
            )
        )
    assert again["status"] == "completed"
    assert api.calls.count("POST /linode/instances/1/disks/5/resize") == 1
    assert api.calls.count("POST /linode/instances/1/boot") == 1


async def test_polling_retries_add_up_across_calls(monkeypatch, no_wait):
    api = FakeLinode()
    # The Linode never finishes shutting down
    api.later = lambda polls, change: None
    async with await _client(monkeypatch, api) as client:
        result = await client.call_tool(RESIZE, _resize_disk_args(max_wait_s=0))
        for _ in range(200):
            if result.is_error:
                break
            result = await client.call_tool(
                RESIZE,
                {"resume": _body(result)["resume"], "max_wait_s": 0},
                raise_on_error=False,
            )
    text = result.content[0].text
    assert result.is_error
    assert "'wait_offline' did not succeed after 120 retries" in text
    # Carried from the call that shut the Linode down
    assert "Earlier steps changed resources" in text


async def test_resuming_early_waits_until_the_next_poll_is_due(monkeypatch):
    waits: list[float] = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(workflows, "_sleep", sleep)
    async with await _client(monkeypatch, FakeLinode()) as client:
        body = _body(await client.call_tool(RESIZE, _resize_disk_args(max_wait_s=0)))
        # Called again at once, not after retry_after_s
        await client.call_tool(RESIZE, {"resume": body["resume"], "max_wait_s": 3})
    # wait_offline polls every 5 seconds; the wait is capped at max_wait_s
    assert waits == [3]
    waits.clear()
    async with await _client(monkeypatch, FakeLinode()) as client:
        body = _body(await client.call_tool(RESIZE, _resize_disk_args(max_wait_s=0)))
        await client.call_tool(RESIZE, {"resume": body["resume"], "max_wait_s": 0})
    assert waits == []


async def test_resuming_with_other_arguments_is_refused(monkeypatch, no_wait):
    async with await _client(monkeypatch, FakeLinode()) as client:
        body = _body(await client.call_tool(RESIZE, _resize_disk_args(max_wait_s=0)))
        with pytest.raises(ToolError, match="pass only resume"):
            await client.call_tool(RESIZE, {"resume": body["resume"], "size": 9})
        with pytest.raises(ToolError, match="Missing required inputs: size"):
            await client.call_tool(RESIZE, {"linodeId": 1, "diskId": 5})


# --- Refused tokens ------------------------------------------------------


@pytest.fixture
async def paused(monkeypatch, no_wait):
    """A client and the result of a resize that paused."""
    async with await _client(monkeypatch, FakeLinode()) as client:
        body = _body(await client.call_tool(RESIZE, _resize_disk_args(max_wait_s=0)))
        yield client, body["resume"]


async def test_edited_token_is_refused(paused):
    client, token = paused
    payload = _payload(token)
    payload["vals"]["inputs"]["size"] = 99999
    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    edited = data.decode() + "." + token.split(".")[1]
    with pytest.raises(ToolError, match="was changed"):
        await client.call_tool(RESIZE, {"resume": edited})
    with pytest.raises(ToolError, match="not a resume token"):
        await client.call_tool(RESIZE, {"resume": "nonsense"})


async def test_token_from_another_key_is_refused(paused, monkeypatch):
    client, token = paused
    monkeypatch.setenv("LINODE_MCP_RESUME_KEY", "another installation")
    with pytest.raises(ToolError, match="issued by another installation"):
        await client.call_tool(RESIZE, {"resume": token})


async def test_expired_token_is_refused(paused, monkeypatch):
    client, token = paused
    later = time.time() + resume.DEFAULT_TTL + 60
    monkeypatch.setattr(resume, "_now", lambda: later)
    with pytest.raises(ToolError, match="expired at"):
        await client.call_tool(RESIZE, {"resume": token})


async def test_token_for_another_workflow_is_refused(paused):
    client, token = paused
    with pytest.raises(ToolError, match="is for the resize-linode-disk workflow"):
        await client.call_tool("workflow_move_volume", {"resume": token})


def test_tokens_hold_no_credentials(monkeypatch):
    monkeypatch.setenv("LINODE_API_TOKEN", "secret-pat")
    token = resume.encode({"wf": "w"}, resume.signing_key())
    assert set(_payload(token)) == {"v", "wf", "exp"}


# --- Restarts and changed workflows ------------------------------------


def _packaged_text():
    return Config.load().workflow_paths()[0].read_text()


async def _client_with_workflows(monkeypatch, tmp_path, text, api):
    """A new server process whose only workflows are ``text``."""
    tmp_path.mkdir()
    monkeypatch.setenv("LINODE_API_TOKEN", "pat")
    config = yaml.safe_load(Config.load().model_dump_json())
    config["workflows"] = ["w.arazzo.yaml"]
    (tmp_path / "w.arazzo.yaml").write_text(text)
    (tmp_path / "apis.yaml").write_text(yaml.dump(config))

    class TestServer(LinodeMCPServer):
        def _api_transport(self):
            return httpx2.MockTransport(api)

        async def _refresh_specs(self):
            pass

    server = TestServer(config_path=str(tmp_path / "apis.yaml"))
    server.spec_manager.save_merged_spec("linode", SPEC)
    await server.initialize()
    return Client(server.mcp)


def _edited(change):
    document = yaml.safe_load(_packaged_text())
    steps = {s["stepId"]: s for s in document["workflows"][0]["steps"]}
    change(steps)
    return yaml.dump(document)


@pytest.mark.parametrize(
    "change, accepted",
    [
        (lambda steps: None, True),
        # A step the run already finished
        (lambda steps: steps["check_status"].update(outputs={}), True),
        # Documentation only
        (lambda steps: steps["wait_disk_ready"].update(description="New"), True),
        # A step still to run
        (
            lambda steps: steps["wait_running"]["onFailure"][0].update(retryLimit=9),
            False,
        ),
    ],
)
async def test_tokens_survive_restarts_until_remaining_steps_change(
    monkeypatch, tmp_path, no_wait, change, accepted
):
    api = FakeLinode()
    async with await _client_with_workflows(
        monkeypatch, tmp_path / "before", _packaged_text(), api
    ) as client:
        results = await _run_to_end(client, RESIZE, _resize_disk_args(max_wait_s=0))
    token = next(r for r in results if r.get("next") == "wait_disk_ready")["resume"]

    async with await _client_with_workflows(
        monkeypatch, tmp_path / "after", _edited(change), api
    ) as client:
        if accepted:
            body = _body(
                await client.call_tool(RESIZE, {"resume": token, "max_wait_s": 1000})
            )
            assert body["status"] == "completed"
        else:
            with pytest.raises(ToolError, match="has changed since"):
                await client.call_tool(RESIZE, {"resume": token})


def test_signing_key_is_created_once_and_private():
    key = resume.signing_key()
    assert resume.signing_key() == key
    assert os.stat(resume._key_path()).st_mode & 0o777 == 0o600


# --- Resume points -------------------------------------------------------


def test_packaged_workflows_can_pause_before_every_read():
    for workflow in _packaged().values():
        methods = _methods(workflow)
        reads = {s.step_id for s in workflow.steps if methods[s.operation_id] == "get"}
        assert set(resume_points(workflow, methods)) == reads


def test_lke_upgrade_carries_the_newest_node_it_started_with():
    workflow = _packaged()["upgrade-lke-cluster"]
    point = resume_points(workflow, _methods(workflow))["wait_old_nodes_gone"]
    assert ("newest_node", "newestNodeId") in point.outputs
    assert point.inputs == {"clusterId"}


def test_no_pause_before_a_read_that_doesnt_guard_the_write_after_it():
    workflow = Workflow.parse(
        {
            "workflowId": "w",
            "steps": [
                {"stepId": "look", "operationId": "get-linode-instance"},
                {"stepId": "boot", "operationId": "post-boot-linode-instance"},
                {"stepId": "poll", "operationId": "get-linode-instance"},
            ],
        }
    )
    assert set(resume_points(workflow, _methods(workflow))) == {"poll"}
