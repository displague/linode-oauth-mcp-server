"""Test the JSON spec cache and conditional upstream refresh."""

import json

import httpx2

from linode_oauth_mcp_server.config import Config
from linode_oauth_mcp_server.spec_manager import SpecManager

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Linode", "version": "4.229.1"},
    "paths": {"/v4/regions": {"get": {"operationId": "get-regions", "security": []}}},
}


def _manager(temp_dir, handler):
    manager = SpecManager(Config.load(None), cache_dir=str(temp_dir))
    manager.http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return manager


async def test_specs_cache_as_json_and_refresh_only_when_etag_changes(temp_dir):
    etag = {"value": '"v1"'}
    fetches = []

    def handler(request):
        if request.headers.get("If-None-Match") == etag["value"]:
            return httpx2.Response(304)
        fetches.append(request.url)
        return httpx2.Response(200, json=SPEC, headers={"ETag": etag["value"]})

    manager = _manager(temp_dir, handler)
    (temp_dir / "linode-merged.yaml").write_text("stale: true\n")

    await manager.update_specs()
    assert manager.has_all_cached_specs()
    assert manager.load_merged_spec("linode")["info"]["version"] == "4.229.1"
    assert not (temp_dir / "linode-merged.yaml").exists()
    assert json.loads((temp_dir / "linode_0.meta.json").read_text())["etag"] == '"v1"'
    assert len(fetches) == 1

    assert await manager.refresh_if_changed() is False
    assert len(fetches) == 1

    etag["value"] = '"v2"'
    assert await manager.refresh_if_changed() is True
    # The conditional check (body not read) plus the re-fetch.
    assert len(fetches) == 3
    assert json.loads((temp_dir / "linode_0.meta.json").read_text())["etag"] == '"v2"'


async def test_refresh_without_cached_validators_refetches(temp_dir):
    manager = _manager(temp_dir, lambda request: httpx2.Response(200, json=SPEC))
    assert await manager.refresh_if_changed() is True
    assert manager.has_all_cached_specs()
