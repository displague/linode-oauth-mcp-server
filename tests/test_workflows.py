"""Test Arazzo workflow parsing, expressions and execution through the server."""

import json

import httpx2
import pytest
import yaml
from fastmcp import Client

from linode_oauth_mcp_server import workflows
from linode_oauth_mcp_server.config import Config
from linode_oauth_mcp_server.main import OAUTH_SCOPES_META, LinodeMCPServer
from linode_oauth_mcp_server.workflows import (
    Workflow,
    WorkflowSpecError,
    _Condition,
    _Response,
    _Scope,
    evaluate,
    load_workflows,
    resolve,
)

PAT = {"personalAccessToken": []}
API_VERSION = {
    "name": "apiVersion",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
}


def _path_param(name):
    return {"name": name, "in": "path", "required": True, "schema": {"type": "integer"}}


def _op(operation_id, scope, params=(), body=None, page=False):
    op = {
        "operationId": operation_id,
        "summary": operation_id,
        "security": [PAT, {"oauth": [scope]}],
        "parameters": [_path_param(p) for p in params]
        + (
            [{"name": "page", "in": "query", "schema": {"type": "integer"}}]
            if page
            else []
        ),
        "responses": {"200": {"description": "ok"}},
    }
    if body:
        op["requestBody"] = {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {k: {"type": t} for k, t in body.items()},
                    }
                }
            }
        }
    return op


LINODE = "/{apiVersion}/linode/instances/{linodeId}"
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Linode", "version": "4.229.1"},
    "servers": [{"url": "https://api.linode.com"}],
    "paths": {
        "/{apiVersion}/linode/instances": {
            "parameters": [API_VERSION],
            "get": _op("get-linode-instances", "linodes:read_only", page=True),
        },
        LINODE: {
            "parameters": [API_VERSION],
            "get": _op("get-linode-instance", "linodes:read_only", ["linodeId"]),
        },
        LINODE + "/shutdown": {
            "parameters": [API_VERSION],
            "post": _op(
                "post-shutdown-linode-instance", "linodes:read_write", ["linodeId"]
            ),
        },
        LINODE + "/boot": {
            "parameters": [API_VERSION],
            "post": _op(
                "post-boot-linode-instance",
                "linodes:read_write",
                ["linodeId"],
                {"config_id": "integer"},
            ),
        },
        LINODE + "/disks/{diskId}": {
            "parameters": [API_VERSION],
            "get": _op("get-linode-disk", "linodes:read_only", ["linodeId", "diskId"]),
        },
        LINODE + "/disks/{diskId}/resize": {
            "parameters": [API_VERSION],
            "post": _op(
                "post-resize-disk",
                "linodes:read_write",
                ["linodeId", "diskId"],
                {"size": "integer"},
            ),
        },
        "/{apiVersion}/volumes/{volumeId}": {
            "parameters": [API_VERSION],
            "get": _op("get-volume", "volumes:read_only", ["volumeId"]),
        },
        "/{apiVersion}/volumes/{volumeId}/detach": {
            "parameters": [API_VERSION],
            "post": _op("post-detach-volume", "volumes:read_write", ["volumeId"]),
        },
        "/{apiVersion}/volumes/{volumeId}/attach": {
            "parameters": [API_VERSION],
            "post": _op(
                "post-attach-volume",
                "volumes:read_write",
                ["volumeId"],
                {"linode_id": "integer", "config_id": "integer"},
            ),
        },
    },
}


class FakeLinode:
    """A Linode whose state changes take a couple of polls to finish."""

    def __init__(self, status="running"):
        self.status = status
        self.disk = {"id": 5, "size": 1000, "status": "ready"}
        self.volume = {"id": 9, "linode_id": 1, "status": "active"}
        self.pending: list = []
        self.calls: list[str] = []
        self.bodies: dict[str, dict] = {}

    def later(self, polls, change):
        self.pending.append([polls, change])

    def tick(self):
        for item in list(self.pending):
            item[0] -= 1
            if item[0] <= 0:
                item[1]()
                self.pending.remove(item)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/v4")
        call = f"{request.method} {path}"
        self.calls.append(call)
        if request.content:
            self.bodies[call] = json.loads(request.content)
        if request.method == "GET":
            self.tick()
        if call == "GET /linode/instances/1":
            return httpx2.Response(200, json={"id": 1, "status": self.status})
        if call == "POST /linode/instances/1/shutdown":
            self.status = "shutting_down"
            self.later(3, lambda: setattr(self, "status", "offline"))
            return httpx2.Response(200, json={})
        if call == "POST /linode/instances/1/boot":
            self.status = "booting"
            self.later(1, lambda: setattr(self, "status", "running"))
            return httpx2.Response(200, json={})
        if call == "GET /linode/instances/1/disks/5":
            return httpx2.Response(200, json=self.disk)
        if call == "POST /linode/instances/1/disks/5/resize":
            if self.status != "offline":
                return httpx2.Response(
                    400, json={"errors": [{"reason": "Linode must be offline"}]}
                )
            size = self.bodies[call]["size"]
            self.disk["status"] = "resizing"
            self.later(2, lambda: self.disk.update(status="ready", size=size))
            return httpx2.Response(200, json={})
        if call == "GET /volumes/9":
            return httpx2.Response(200, json=self.volume)
        if call == "POST /volumes/9/detach":
            self.later(2, lambda: self.volume.update(linode_id=None))
            return httpx2.Response(200, json={})
        if call == "POST /volumes/9/attach":
            if self.volume["linode_id"] is not None:
                return httpx2.Response(400, json={"errors": [{"reason": "attached"}]})
            target = self.bodies[call]["linode_id"]
            self.later(1, lambda: self.volume.update(linode_id=target))
            return httpx2.Response(200, json=self.volume)
        return httpx2.Response(404, json={"errors": [{"reason": "Not found"}]})


@pytest.fixture
def no_wait(monkeypatch):
    async def sleep(_seconds):
        pass

    monkeypatch.setattr(workflows, "_sleep", sleep)


async def _client(monkeypatch, api, spec=SPEC, tool_catalog="full"):
    monkeypatch.setenv("LINODE_API_TOKEN", "pat")

    class TestServer(LinodeMCPServer):
        def _build_api_client(self, operation_scopes):
            client = super()._build_api_client(operation_scopes)
            client._transport = httpx2.MockTransport(api)
            return client

        async def _refresh_specs(self):
            pass

    server = TestServer(tool_catalog=tool_catalog)
    server.spec_manager.save_merged_spec("linode", spec)
    await server.initialize()
    return Client(server.mcp)


def _resize_disk_args(size=2000):
    return {"linodeId": 1, "diskId": 5, "size": size}


# --- Expressions and criteria ---------------------------------------------


def _scope(body=None, status=200, **inputs):
    scope = _Scope(inputs=inputs, steps={"first": {"id": 42}})
    scope.response = _Response(status=status, headers={"x-id": "abc"}, body=body)
    return scope


def test_expressions_resolve_inputs_steps_and_response():
    scope = _scope({"data": [{"id": 7, "a/b": 1}]}, size=3)
    assert evaluate("$statusCode", scope) == 200
    assert evaluate("$inputs.size", scope) == 3
    assert evaluate("$steps.first.outputs.id", scope) == 42
    assert evaluate("$response.body#/data/0/id", scope) == 7
    assert evaluate("$response.body#/data/0/a~1b", scope) == 1
    assert evaluate("$response.body#/data/5/id", scope) is None
    assert evaluate("$response.header.X-Id", scope) == "abc"
    with pytest.raises(WorkflowSpecError):
        evaluate("$url", scope)


def test_resolve_keeps_types_and_substitutes_embedded_expressions():
    scope = _scope(cluster=7)
    assert resolve("$inputs.cluster", scope) == 7
    assert resolve({"ids": ["$inputs.cluster"]}, scope) == {"ids": [7]}
    assert (
        resolve('{"lke_cluster_id": {$inputs.cluster}, "+order": "desc"}', scope)
        == '{"lke_cluster_id": 7, "+order": "desc"}'
    )


@pytest.mark.parametrize(
    "condition, expected",
    [
        ("$statusCode == 200", True),
        ("$statusCode != 200", False),
        ("$response.body#/status == 'OFFLINE'", True),  # case-insensitive
        ("$response.body#/size >= 2000 && $response.body#/size < 3000", True),
        ("$response.body#/status == 'running' || $response.body#/size == 2000", True),
        ("!($response.body#/status == 'offline')", False),
        ("$response.body#/linode_id == null", True),
        ("$response.body#/size == $inputs.size", True),
        ("$response.body#/size == '2000'", True),
        ("$response.body#/flag == true", True),
    ],
)
def test_simple_conditions(condition, expected):
    scope = _scope(
        {"status": "offline", "size": 2000, "linode_id": None, "flag": True},
        size=2000,
    )
    assert _Condition(condition).evaluate(scope) is expected


@pytest.mark.parametrize("condition", ["$statusCode ==", "($statusCode == 200", "a b"])
def test_malformed_conditions_are_rejected(condition):
    with pytest.raises(WorkflowSpecError):
        _Condition(condition).evaluate(_scope())


# --- Parsing -------------------------------------------------------------


def test_packaged_workflows_parse():
    paths = Config.load().workflow_paths()
    assert [p.name for p in paths] == ["workflows.arazzo.yaml"]
    ids = [w.workflow_id for w in load_workflows(paths[0])]
    assert ids == ["resize-linode-disk", "move-volume", "upgrade-lke-cluster"]


def _workflow(**step):
    return {
        "workflowId": "w",
        "steps": [{"stepId": "a", "operationId": "get-volume", **step}],
    }


@pytest.mark.parametrize(
    "workflow",
    [
        {"workflowId": "w", "steps": [{"stepId": "a", "workflowId": "other"}]},
        _workflow(successCriteria=[{"condition": "$", "type": "jsonpath"}]),
        _workflow(onSuccess=[{"type": "goto", "stepId": "missing"}]),
        _workflow(onSuccess=[{"type": "retry"}]),
        _workflow(requestBody={"payload": {}, "replacements": []}),
        {"workflowId": "w", "steps": []},
    ],
)
def test_unsupported_features_are_rejected_at_load(workflow):
    with pytest.raises(WorkflowSpecError):
        Workflow.parse(workflow)


# --- Execution -----------------------------------------------------------


async def test_resize_disk_powers_off_resizes_and_boots(monkeypatch, no_wait):
    api = FakeLinode()
    async with await _client(monkeypatch, api) as client:
        result = await client.call_tool(
            "workflow_resize_linode_disk", _resize_disk_args()
        )
    body = yaml.safe_load(result.content[0].text)
    assert body["outputs"] == {"initialStatus": "running", "size": 2000}
    assert [c for c in api.calls if c.startswith("POST")] == [
        "POST /linode/instances/1/shutdown",
        "POST /linode/instances/1/disks/5/resize",
        "POST /linode/instances/1/boot",
    ]
    assert api.status == "running"
    assert api.bodies["POST /linode/instances/1/disks/5/resize"] == {"size": 2000}
    # Repeated polls fold into one trace line
    assert any("wait_offline" in line and "×" in line for line in body["steps"])


async def test_resize_disk_leaves_an_offline_linode_offline(monkeypatch, no_wait):
    api = FakeLinode(status="offline")
    async with await _client(monkeypatch, api) as client:
        await client.call_tool("workflow_resize_linode_disk", _resize_disk_args())
    assert [c for c in api.calls if c.startswith("POST")] == [
        "POST /linode/instances/1/disks/5/resize"
    ]
    assert api.status == "offline"


async def test_exhausted_polling_reports_changes(monkeypatch, no_wait):
    api = FakeLinode()
    # The Linode never finishes shutting down
    api.later = lambda polls, change: None
    async with await _client(monkeypatch, api) as client:
        result = await client.call_tool(
            "workflow_resize_linode_disk", _resize_disk_args(), raise_on_error=False
        )
    text = result.content[0].text
    assert result.is_error
    assert "'wait_offline' did not succeed after 120 retries" in text
    assert "Earlier steps changed resources" in text
    assert api.calls.count("GET /linode/instances/1") == 122


async def test_failure_before_any_change_raises(monkeypatch, no_wait):
    api = FakeLinode()
    async with await _client(monkeypatch, api) as client:
        result = await client.call_tool(
            "workflow_resize_linode_disk",
            {"linodeId": 404, "diskId": 5, "size": 2000},
            raise_on_error=False,
        )
    text = result.content[0].text
    assert result.is_error
    assert "HTTP 404" in text
    assert "Earlier steps changed" not in text


async def test_move_volume_detaches_before_attaching(monkeypatch, no_wait):
    api = FakeLinode()
    async with await _client(monkeypatch, api) as client:
        result = await client.call_tool(
            "workflow_move_volume", {"volumeId": 9, "linodeId": 2}
        )
    outputs = yaml.safe_load(result.content[0].text)["outputs"]
    assert outputs["previousLinodeId"] == 1
    assert outputs["linodeId"] == 2
    # The optional configId was not given, so it is not sent
    assert api.bodies["POST /volumes/9/attach"] == {"linode_id": 2}


async def test_move_volume_already_attached_does_nothing(monkeypatch, no_wait):
    api = FakeLinode()
    async with await _client(monkeypatch, api) as client:
        await client.call_tool("workflow_move_volume", {"volumeId": 9, "linodeId": 1})
    assert api.calls == ["GET /volumes/9"]


async def test_workflow_tools_declare_step_scopes_and_annotations(monkeypatch):
    async with await _client(monkeypatch, FakeLinode()) as client:
        tools = {t.name: t for t in await client.list_tools()}
    tool = tools["workflow_resize_linode_disk"]
    assert tool.meta[OAUTH_SCOPES_META] == ["linodes:read_only", "linodes:read_write"]
    assert tool.annotations.destructive_hint is True
    assert tool.annotations.read_only_hint is False
    assert tool.title == "Resize a Linode disk"
    assert set(tool.input_schema["required"]) == {"linodeId", "diskId", "size"}
    # The spec has no LKE operations, so that workflow is skipped
    assert "workflow_upgrade_lke_cluster" not in tools


async def test_workflows_are_found_through_search(monkeypatch):
    async with await _client(
        monkeypatch, FakeLinode(), tool_catalog="search"
    ) as client:
        text = (
            (await client.call_tool("search_tools", {"query": "move volume to linode"}))
            .content[0]
            .text
        )
        schema = await client.call_tool(
            "get_tool_schema", {"name": "workflow_move_volume"}
        )
    assert "workflow_move_volume" in text
    assert schema.structured_content["inputSchema"]["required"] == [
        "volumeId",
        "linodeId",
    ]


async def test_lke_upgrade_filters_on_old_node_ids(monkeypatch, no_wait):
    spec = json.loads(json.dumps(SPEC))
    cluster = "/{apiVersion}/lke/clusters/{clusterId}"
    spec["paths"].update(
        {
            cluster: {
                "parameters": [API_VERSION],
                "get": _op("get-lke-cluster", "lke:read_only", ["clusterId"]),
                "put": _op(
                    "put-lke-cluster",
                    "lke:read_write",
                    ["clusterId"],
                    {"k8s_version": "string"},
                ),
            },
            cluster + "/recycle": {
                "parameters": [API_VERSION],
                "post": _op(
                    "post-lke-cluster-recycle", "lke:read_write", ["clusterId"]
                ),
            },
            cluster + "/pools": {
                "parameters": [API_VERSION],
                "get": _op("get-lke-cluster-pools", "lke:read_only", ["clusterId"]),
            },
        }
    )
    state = {"version": "1.36", "old_nodes": 2, "pool": "not_ready", "recycles": 0}
    filters = []

    def api(request):
        path = request.url.path.removeprefix("/v4")
        if path == "/linode/instances":
            query = json.loads(request.headers["X-Filter"])
            filters.append(query)
            if "id" in query:
                # Old nodes are replaced one per poll once the recycle starts
                if state["recycles"]:
                    state["old_nodes"] = max(state["old_nodes"] - 1, 0)
                return httpx2.Response(
                    200, json={"data": [], "results": state["old_nodes"]}
                )
            return httpx2.Response(
                200, json={"data": [{"id": 300}, {"id": 200}], "results": 2}
            )
        if path == "/lke/clusters/7/recycle":
            state["recycles"] += 1
            return httpx2.Response(200, json={})
        if path == "/lke/clusters/7" and request.method == "PUT":
            state["version"] = json.loads(request.content)["k8s_version"]
        if path == "/lke/clusters/7":
            return httpx2.Response(200, json={"id": 7, "k8s_version": state["version"]})
        if path == "/lke/clusters/7/pools":
            status, state["pool"] = state["pool"], "ready"
            return httpx2.Response(
                200,
                json={"data": [{"nodes": [{"status": "ready"}, {"status": status}]}]},
            )
        return httpx2.Response(200, json={})

    async with await _client(monkeypatch, api, spec) as client:
        result = await client.call_tool(
            "workflow_upgrade_lke_cluster", {"clusterId": 7, "k8sVersion": "1.37"}
        )
    outputs = yaml.safe_load(result.content[0].text)["outputs"]
    assert outputs == {
        "previousVersion": "1.36",
        "k8sVersion": "1.37",
        "recycledNodes": 2,
    }
    assert filters[0] == {"lke_cluster_id": 7, "+order_by": "id", "+order": "desc"}
    assert filters[1] == {"lke_cluster_id": 7, "id": {"+lte": 300}}
    assert state["recycles"] == 1


# --- Rate limits and deadlines ------------------------------------------


@pytest.fixture
def fake_clock(monkeypatch):
    """Sleeps return at once but move the workflow's clock forward."""
    now = [0.0]
    waits: list[float] = []

    async def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(workflows, "_sleep", sleep)
    monkeypatch.setattr(workflows, "_clock", lambda: now[0])
    return waits


class RateLimited(FakeLinode):
    """Rate limit the first ``count`` requests matching ``call``."""

    def __init__(self, call, count, retry_after="7", **kwargs):
        super().__init__(**kwargs)
        self.limited_call, self.remaining, self.retry_after = call, count, retry_after

    def __call__(self, request):
        call = f"{request.method} {request.url.path.removeprefix('/v4')}"
        if call == self.limited_call and self.remaining > 0:
            self.remaining -= 1
            self.calls.append(f"{call} (429)")
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return httpx2.Response(
                429, headers=headers, json={"errors": [{"reason": "Too many"}]}
            )
        return super().__call__(request)


async def test_rate_limited_poll_waits_and_polls_again(monkeypatch, fake_clock):
    api = RateLimited("GET /linode/instances/1/disks/5", 2)
    async with await _client(monkeypatch, api) as client:
        result = await client.call_tool(
            "workflow_resize_linode_disk", _resize_disk_args()
        )
    body = yaml.safe_load(result.content[0].text)
    assert body["outputs"] == {"initialStatus": "running", "size": 2000}
    assert fake_clock.count(7.0) == 2
    assert any("(rate limited) ×2" in line for line in body["steps"])


async def test_rate_limited_mutation_is_sent_again(monkeypatch, fake_clock):
    api = RateLimited("POST /linode/instances/1/shutdown", 1)
    async with await _client(monkeypatch, api) as client:
        await client.call_tool("workflow_resize_linode_disk", _resize_disk_args())
    assert api.calls.count("POST /linode/instances/1/shutdown (429)") == 1
    assert api.calls.count("POST /linode/instances/1/shutdown") == 1
    assert api.status == "running"


def test_rate_limit_wait_prefers_retry_after_then_the_poll_interval():
    resize = load_workflows(Config.load().workflow_paths()[0])[0]
    steps = {s.step_id: s for s in resize.steps}

    def wait(step, headers):
        response = _Response(status=429, headers=headers, body=None)
        return workflows._rate_limit_wait(response, steps[step], resize)

    assert wait("wait_disk_ready", {"retry-after": "3"}) == 3
    # wait_disk_ready polls every 10 seconds
    assert wait("wait_disk_ready", {}) == 10
    # shut_down doesn't poll
    assert wait("shut_down", {}) == workflows.RATE_LIMIT_DEFAULT_WAIT


async def test_rate_limit_before_any_change_fails_safely(monkeypatch, fake_clock):
    api = RateLimited("GET /linode/instances/1", 100, retry_after="60")
    async with await _client(monkeypatch, api) as client:
        result = await client.call_tool(
            "workflow_resize_linode_disk", _resize_disk_args(), raise_on_error=False
        )
    text = result.content[0].text
    assert result.is_error
    assert "'check_status' was still rate limited" in text
    assert "did not apply it" in text
    assert "Earlier steps changed" not in text


async def test_polling_step_stops_at_its_deadline(monkeypatch, fake_clock):
    # Rate limits use up wait_offline's time before its retries run out
    api = RateLimited("GET /linode/instances/1", 1000, retry_after="59")
    api.remaining = 0
    shutdown = api.__class__.__call__

    def limit_after_shutdown(request):
        if request.method == "POST" and request.url.path.endswith("/shutdown"):
            api.remaining = 1000
        return shutdown(api, request)

    async with await _client(monkeypatch, limit_after_shutdown) as client:
        result = await client.call_tool(
            "workflow_resize_linode_disk", _resize_disk_args(), raise_on_error=False
        )
    text = result.content[0].text
    assert "'wait_offline' was still rate limited" in text
    assert "Earlier steps changed resources" in text
    # wait_offline allows 5s × 120 polls plus the rate-limit allowance
    assert sum(fake_clock) <= 5 * 120 + workflows.RATE_LIMIT_ALLOWANCE


# --- Running again ---------------------------------------------------------


async def test_resize_skips_a_disk_already_at_the_new_size(monkeypatch, no_wait):
    api = FakeLinode(status="offline")
    api.disk["size"] = 2000
    async with await _client(monkeypatch, api) as client:
        result = await client.call_tool(
            "workflow_resize_linode_disk", _resize_disk_args()
        )
    assert not [c for c in api.calls if c.startswith("POST")]
    assert yaml.safe_load(result.content[0].text)["outputs"]["size"] == 2000


async def test_resize_doesnt_boot_a_linode_that_is_already_up(monkeypatch, no_wait):
    api = FakeLinode()
    resize = api.__class__.__call__

    def booted_by_someone_else(request):
        response = resize(api, request)
        if request.url.path.endswith("/resize"):
            # Someone boots the Linode while the disk resizes
            api.later(1, lambda: setattr(api, "status", "running"))
        return response

    async with await _client(monkeypatch, booted_by_someone_else) as client:
        await client.call_tool("workflow_resize_linode_disk", _resize_disk_args())
    assert "POST /linode/instances/1/boot" not in api.calls


UNGUARDED = """
arazzo: 1.0.1
info: {title: t, version: 1.0.0}
sourceDescriptions: [{name: linode, url: https://example.com, type: openapi}]
workflows:
  - workflowId: boot-twice
    steps:
      - stepId: boot
        operationId: post-boot-linode-instance
        parameters:
          - {name: apiVersion, in: path, value: v4}
          - {name: linodeId, in: path, value: 1}
"""


async def test_startup_records_unsafe_workflow_steps(monkeypatch, tmp_path):
    monkeypatch.setenv("LINODE_API_TOKEN", "pat")
    config = yaml.safe_load(Config.load().model_dump_json())
    config["workflows"] = ["unguarded.arazzo.yaml"]
    (tmp_path / "unguarded.arazzo.yaml").write_text(UNGUARDED)
    (tmp_path / "apis.yaml").write_text(yaml.dump(config))

    class TestServer(LinodeMCPServer):
        def _api_transport(self):
            return httpx2.MockTransport(FakeLinode())

        async def _refresh_specs(self):
            pass

    server = TestServer(config_path=str(tmp_path / "apis.yaml"))
    server.spec_manager.save_merged_spec("linode", SPEC)
    await server.initialize()
    assert [(f.rule, f.step_id) for f in server.workflow_findings] == [
        ("unguarded-write", "boot")
    ]
    # The workflow is still offered; the check only warns
    assert await server.mcp.get_tool("workflow_boot_twice") is not None


async def test_packaged_workflows_pass_the_startup_check(monkeypatch, caplog):
    async with await _client(monkeypatch, FakeLinode()) as client:
        await client.list_tools()
    assert "unguarded-write" not in caplog.text
