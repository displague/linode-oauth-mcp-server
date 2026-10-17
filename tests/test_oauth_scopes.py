"""Test per-operation OAuth scope lookup and incremental consent."""

import asyncio

import pytest

from linode_oauth_mcp_server.auth import AuthManager, SignIn
from linode_oauth_mcp_server.oauth_scopes import (
    AuthRequirement,
    OperationScopes,
    merge_scopes,
    parse_scopes,
    scopes_satisfy,
)

PAT = {"personalAccessToken": []}
SPEC = {
    "paths": {
        "/{apiVersion}/linode/instances": {
            "get": {"security": [PAT, {"oauth": ["linodes:read_only"]}]},
            "post": {"security": [PAT, {"oauth": ["linodes:read_write"]}]},
        },
        "/{apiVersion}/linode/instances/{linodeId}": {
            "get": {"security": [PAT, {"oauth": ["linodes:read_only"]}]},
        },
        "/{apiVersion}/linode/types": {"get": {"security": []}},
        "/{apiVersion}/linode/{anything}": {
            "get": {"security": [PAT, {"oauth": ["account:read_write"]}]},
        },
        "/{apiVersion}/iam/role-permissions": {"get": {"security": [PAT]}},
        "/{apiVersion}/volumes/{volumeId}/attach": {
            "post": {
                "security": [
                    PAT,
                    {"oauth": ["volumes:read_write", "linodes:read_write"]},
                ]
            },
        },
    }
}


@pytest.mark.parametrize(
    "method, path, expected",
    [
        (
            "GET",
            "/v4/linode/instances",
            AuthRequirement(scopes=frozenset({"linodes:read_only"})),
        ),
        (
            "POST",
            "/v4/linode/instances",
            AuthRequirement(scopes=frozenset({"linodes:read_write"})),
        ),
        (
            "GET",
            "/v4beta/linode/instances/123/",
            AuthRequirement(scopes=frozenset({"linodes:read_only"})),
        ),
        ("GET", "/v4/linode/types", AuthRequirement(public=True)),
        ("GET", "/v4/iam/role-permissions", AuthRequirement()),
        (
            "POST",
            "/v4/volumes/9/attach",
            AuthRequirement(
                scopes=frozenset({"volumes:read_write", "linodes:read_write"})
            ),
        ),
        ("DELETE", "/v4/linode/instances", None),
        ("GET", "/v4/unknown", None),
    ],
)
def test_requirement_lookup(method, path, expected):
    assert OperationScopes(SPEC).requirement(method, path) == expected


def test_scope_algebra():
    assert parse_scopes("linodes:read_only, domains:*") == {
        "linodes:read_only",
        "domains:*",
    }
    assert scopes_satisfy({"linodes:read_write"}, {"linodes:read_only"})
    assert scopes_satisfy({"linodes:*"}, {"linodes:read_write"})
    assert scopes_satisfy({"*"}, {"account:read_write"})
    assert not scopes_satisfy({"linodes:read_only"}, {"linodes:read_write"})
    assert not scopes_satisfy({"linodes:read_write"}, {"domains:read_only"})
    assert merge_scopes(
        {"linodes:read_only", "domains:read_only"}, {"linodes:read_write"}
    ) == {
        "linodes:read_write",
        "domains:read_only",
    }


@pytest.fixture
def oauth_manager(monkeypatch):
    for var in (
        "LINODE_API_TOKEN",
        "LINODE_OAUTH_TOKEN",
        "LINODE_CLI_TOKEN",
        "LINODE_OAUTH_SCOPES",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LINODE_CLI_CONFIG", "/nonexistent")
    monkeypatch.setenv("LINODE_OAUTH_CLIENT_ID", "client")
    manager = AuthManager()
    requests = []

    async def fake_start(scopes):
        requests.append(scopes)
        manager.token = f"token{len(requests)}"
        manager._expires_at = float("inf")
        manager.granted_scopes = scopes
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return SignIn(
            scopes=scopes, url="https://login.linode.com/x", state="s", done=done
        )

    monkeypatch.setattr(manager, "_start_sign_in", fake_start)
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    return manager, requests


async def test_oauth_steps_up_only_when_scopes_are_missing(oauth_manager):
    manager, requests = oauth_manager

    header = await manager.get_auth_header(frozenset({"linodes:read_only"}))
    assert header == {"Authorization": "Bearer token1"}
    assert requests == [{"linodes:read_only", "stackscripts:read_only"}]

    await manager.get_auth_header(frozenset({"stackscripts:read_only"}))
    assert len(requests) == 1

    header = await manager.get_auth_header(
        frozenset({"linodes:read_write", "volumes:read_write"})
    )
    assert header == {"Authorization": "Bearer token2"}
    assert requests[1] == {
        "linodes:read_write",
        "volumes:read_write",
        "stackscripts:read_only",
    }


async def test_static_token_is_sent_without_scope_checks(monkeypatch):
    monkeypatch.setenv("LINODE_API_TOKEN", "pat")
    manager = AuthManager()
    assert await manager.get_auth_header(frozenset({"account:read_write"})) == {
        "Authorization": "Bearer pat"
    }


async def test_sign_in_url_falls_back_to_browser_outside_mcp_request():
    from linode_oauth_mcp_server.auth import _show_url_in_client

    assert await _show_url_in_client("msg", "https://login.linode.com/x", "id") is None


def _oauth_env(monkeypatch, client_id="client"):
    for var in ("LINODE_API_TOKEN", "LINODE_OAUTH_TOKEN", "LINODE_CLI_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LINODE_CLI_CONFIG", "/nonexistent")
    monkeypatch.setenv("LINODE_OAUTH_CLIENT_ID", client_id)


def test_oauth_token_is_saved_privately_and_resumed(monkeypatch):
    import os
    import stat
    import time

    from linode_oauth_mcp_server.auth import _token_path

    _oauth_env(monkeypatch)
    first = AuthManager()
    first.token = "saved-token"
    first._expires_at = time.time() + 3600
    first.granted_scopes = frozenset({"linodes:read_only", "account:read_only"})
    first._save_token()
    assert stat.S_IMODE(os.stat(_token_path()).st_mode) == 0o600

    resumed = AuthManager()
    assert resumed.token == "saved-token"
    assert resumed.granted_scopes == {"linodes:read_only", "account:read_only"}
    assert not resumed.needs_sign_in(frozenset({"account:read_only"}))
    assert resumed.needs_sign_in(frozenset({"account:read_write"}))

    resumed.clear_token_cache()
    assert not _token_path().exists()
    assert AuthManager().token is None


def test_expired_or_foreign_saved_token_is_discarded(monkeypatch):
    import time

    from linode_oauth_mcp_server.auth import _token_path

    _oauth_env(monkeypatch)
    manager = AuthManager()
    manager.token = "old"
    manager._expires_at = time.time() + 10  # inside the 30 second safety margin
    manager._save_token()
    assert AuthManager().token is None
    assert not _token_path().exists()

    manager._expires_at = time.time() + 3600
    manager._save_token()
    _oauth_env(monkeypatch, client_id="other-client")
    assert AuthManager().token is None


def test_static_token_ignores_saved_oauth_token(monkeypatch):
    import time

    _oauth_env(monkeypatch)
    manager = AuthManager()
    manager.token = "oauth"
    manager._expires_at = time.time() + 3600
    manager._save_token()
    monkeypatch.setenv("LINODE_API_TOKEN", "pat")
    assert AuthManager().token == "pat"
