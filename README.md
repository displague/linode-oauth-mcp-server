# Linode OAuth MCP Server

An experimental MCP server that turns the Linode API v4 OpenAPI specification into tools. Each tool requests only the OAuth scopes its operation needs, and the sign-in link appears in the MCP client's chat. The API spec is fetched from Linode's official [OpenAPI distribution](https://github.com/linode/linode-api-openapi), parsed, cached on disk, and used to build the MCP tool catalog.

## Scope and discovery

The server exposes Linode API operations as `linode_<operationId>` tools. By default, the server lists only three tools and keeps the rest of the catalog out of the initial list: `search_tools` finds operations, `call_tool` invokes them, and `get_tool_schema` returns one tool's full input schema. Search ranks tools by their name, title, and summary, and returns a compact entry for each of up to 10 matches: its title, first description paragraph, access level, required OAuth scopes, and a one-line summary per parameter. Results run about 5K characters, where full tool definitions would be up to 250K. Responses are rendered as YAML, including calls made through `call_tool`. `--tool-catalog full` lists every generated tool directly. Every paginated list tool accepts an `X-Filter` argument, a JSON string sent as Linode's [filter header](https://techdocs.akamai.com/linode-api/reference/filtering-and-sorting) (for example, `{"mine": true}`). The upstream spec declares that header on only a few operations.

The server fetches the spec on first launch and stores it in the user's cache directory (`LINODE_MCP_CACHE_DIR` overrides the default). Later starts load the cached JSON copy, which takes about a second. After startup, the server checks the upstream spec in the background with a conditional GET using the cached `ETag`. An unchanged spec costs one small `304 Not Modified` request. If the spec has changed, the server fetches and caches it, and the new spec takes effect the next time the server starts. Run `linode-oauth-mcp-server --update-specs` to force a fresh fetch.

## Authentication

Credentials are checked in this order:

1. `LINODE_API_TOKEN` or `LINODE_OAUTH_TOKEN`.
2. `LINODE_CLI_TOKEN`, then the `token` in `~/.config/linode-cli` (or `LINODE_CLI_CONFIG`).
3. A Linode public OAuth app, when `LINODE_OAUTH_CLIENT_ID` is configured.

When only the public client is configured, the first authenticated tool call opens Linode sign-in in a browser. The user grants access and the server receives the short-lived bearer token through a loopback callback. The server saves the token, its expiry, and its granted scopes to `oauth-token.json` in the cache directory. The file is readable only by the user (mode `0600`). A restarted server, for example after an MCP reconnect, reuses that token until it expires; Linode's public OAuth tokens last 2 hours. Expired tokens, and tokens saved for a different OAuth client ID, are discarded. Delete the file to sign out. Tokens from `LINODE_API_TOKEN` or linode-cli take precedence over a saved sign-in. A personal access token or an existing CLI credential is simpler for personal use. The Linode CLI can set up its own browser-based sign-in with `linode-cli configure`.

The first sign-in requests read-only access to Linodes and StackScripts, plus whatever scopes the first tool call needs. Each operation's required scopes come from its `security` entry in the OpenAPI spec. When a later call needs a scope the current token lacks (for example, `linodes:read_write` to create a Linode), the server asks for sign-in again. That request asks for the scopes already granted plus the new ones. MCP clients that support URL-mode elicitation show the sign-in link in the chat. On MCP 2026-07-28, the server returns an input-required result carrying the link, and the client retries the call after the user opens it. On earlier protocol versions, the server sends the elicitation request during the call. Clients without URL elicitation get the link opened in the system browser. Each tool's required scopes are listed in its `linode/oauthScopes` metadata. The call waits until the user approves, then continues with the new token. Operations the spec marks as public, such as listing regions or Linode types, never prompt for sign-in. Set `LINODE_OAUTH_SCOPES` (comma- or space-separated) to change the initial scope set, for example to `*` to grant everything up front. Tokens from `LINODE_API_TOKEN` or linode-cli are used as-is, without scope checks.

The repo includes a shared public OAuth client ID. To register a replacement client, use a Linode CLI identity permitted to create OAuth clients:

```sh
linode-cli account client-create \
  --label "Linode OAuth MCP Server" \
  --redirect_uri http://localhost:8765/oauth/callback \
  --public true
```

The returned `id` is public and safe to commit; do not publish a private client's secret. Users can share this public client and do not need to register their own. The callback URL must exactly match the configured `http://localhost:8765/oauth/callback`. Public OAuth returns a short-lived access token in the redirect URL and does not issue a refresh token, so users sign in again when it expires. For personal use, a Linode CLI credential or personal access token is simpler. See [Linode authentication docs](https://techdocs.akamai.com/linode-api/reference/get-started) and [Create an OAuth client](https://techdocs.akamai.com/linode-api/reference/post-client).

## Install and run

Requires Python 3.13+. The package is published on PyPI as [`linode-oauth-mcp-server`](https://pypi.org/p/linode-oauth-mcp-server). Run it without installing, using either tool:

```sh
uvx linode-oauth-mcp-server
pipx run linode-oauth-mcp-server
```

Both fetch the latest release on first use and cache it. To pick up a newer release, run `uvx linode-oauth-mcp-server@latest`, or pin a version with `uvx linode-oauth-mcp-server@0.2.0`. To install the command permanently, run `uv tool install linode-oauth-mcp-server` or `pipx install linode-oauth-mcp-server`.

This starts the default stdio server for MCP clients that launch local processes. For ChatGPT, which connects to remote MCP endpoints, run the server over local Streamable HTTP and connect through a Secure MCP Tunnel:

```sh
uvx linode-oauth-mcp-server --transport http --host 127.0.0.1 --port 8000
```

The local MCP endpoint is `http://127.0.0.1:8000/mcp`. In ChatGPT, create a custom MCP connection and select an available Secure MCP Tunnel for this endpoint; use the resulting tunnel URL in the connection. Keep the local server running while testing. The HTTP endpoint has no separate MCP bearer authentication, so use it only locally behind a private tunnel; do not expose it directly to the public internet.

The initial spec fetch requires network access. Later starts use the cached specification.

## MCP client examples

The server signs in through Linode's shared public OAuth client by default, so no token needs to be copied into client configuration. It also uses `LINODE_API_TOKEN` or an existing Linode CLI credential when one is present.

### Claude Code

```sh
claude mcp add linode -- uvx linode-oauth-mcp-server
```

Run `/mcp` in Claude Code to confirm that `linode` is connected.

### Codex CLI

```sh
codex mcp add linode -- uvx linode-oauth-mcp-server
```

If your Codex sandbox restricts writes to your home cache directory or blocks network access, use local caches and fetch the spec once from your normal shell before adding the server:

```sh
UV_CACHE_DIR="$PWD/.uv-cache" LINODE_MCP_CACHE_DIR="$PWD/.linode-mcp-cache" \
  uvx linode-oauth-mcp-server --update-specs
codex mcp add linode \
  --env UV_CACHE_DIR="$PWD/.uv-cache" \
  --env LINODE_MCP_CACHE_DIR="$PWD/.linode-mcp-cache" \
  -- uvx linode-oauth-mcp-server
```

Run `codex mcp list` to confirm the registration, then restart Codex so the server is loaded.

### Other stdio clients

```json
{
  "mcpServers": {
    "linode": {
      "command": "uvx",
      "args": ["linode-oauth-mcp-server"]
    }
  }
}
```

## Workflows

Some changes take several API calls in a set order, with waits in between. The server exposes these as `workflow_<workflowId>` tools, defined in an [Arazzo](https://spec.openapis.org/arazzo/latest.html) document (`data/config/workflows.arazzo.yaml`):

| Tool | What it does |
|---|---|
| `workflow_resize_linode_disk` | Shuts the Linode down if it is running, resizes the disk, waits for the new size, and boots the Linode again if it was running. |
| `workflow_move_volume` | Detaches a volume, waits for the detach to finish, attaches it to another Linode, and waits for the attachment. |
| `workflow_upgrade_lke_cluster` | Sets an LKE cluster's Kubernetes version, recycles its nodes, and waits until every old node is replaced and all nodes are ready. |

Search finds workflow tools alongside the API tools, and `get_tool_schema` describes them. Each workflow runs as one tool call that waits for its steps to finish, reports progress per step, and returns its outputs with a list of the steps it ran. Steps run through the server's own API tools, so a workflow uses the same credentials and requests the combined OAuth scopes of its steps in a single sign-in. If a step fails after an earlier step has changed something, the error says so, and the workflow is not retried automatically.

The executor supports a subset of Arazzo 1.0: `operationId` steps, runtime expressions in parameters and request payloads, `simple` and `regex` criteria, `end`/`goto`/`retry` actions (`retry` with `retryAfter` and `retryLimit` is how a step polls), and step and workflow outputs. It rejects documents that use other features when it loads them. A workflow whose operations aren't in the cached spec is skipped at startup. To add workflows, list more Arazzo documents under `workflows:` in a config file; relative paths resolve against the config file's directory.

## API spec updates

The default source is `https://raw.githubusercontent.com/linode/linode-api-openapi/main/openapi.json`. To change it, pass a YAML config:

```yaml
apis:
  linode:
    description: "Linode API v4"
    tags: [cloud, compute, networking]
    service_name: linode
    specs:
      - url: "https://raw.githubusercontent.com/linode/linode-api-openapi/main/openapi.json"
```

Then run `linode-oauth-mcp-server --config path/to/apis.yaml --update-specs`.

## Safety and limits

The upstream OpenAPI document includes read and write operations, including operations that create billable resources or delete resources. Review the operation description and parameters before invoking a write. Grant the token only the scopes required for the work. This project is experimental; API behavior and account-level permissions still need testing against a Linode account.

## Development

Run the server and tests from a checkout:

```sh
uv run linode-oauth-mcp-server
uv run --extra dev pytest
```

Lint, format, and type check the way CI does:

```sh
uv run --extra dev ruff check src tests
uv run --extra dev ruff format src tests
uv run --extra dev ty check src
```

To have an MCP client run your checkout instead of the PyPI release, register `uv run --project /path/to/checkout linode-oauth-mcp-server` as the command.

## Releasing

Versions come from git tags through `setuptools-scm`. Pushing a tag like `v0.2.0`, or publishing a GitHub release that creates one, runs [`.github/workflows/release.yml`](.github/workflows/release.yml). The workflow tests, builds, and publishes `0.2.0` to PyPI:

```sh
git tag v0.2.0
git push origin v0.2.0
```

Publishing uses [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/), which is based on GitHub's OpenID Connect tokens, so the repository stores no PyPI API token. One-time setup:

1. On PyPI, open **Your account → Publishing → Add a new pending publisher** and enter:
   - PyPI project name: `linode-oauth-mcp-server`
   - Owner: `displague`
   - Repository name: `linode-oauth-mcp-server`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. On GitHub, create an environment named `pypi` under **Settings → Environments**. Optionally, limit it to `v*` tags or require a reviewer.

The first tagged release creates the PyPI project.

## References

- [Linode API v4 reference](https://techdocs.akamai.com/linode-api/reference/api)
- [Linode API authentication](https://techdocs.akamai.com/linode-api/reference/get-started)
- [Linode OpenAPI spec distribution](https://github.com/linode/linode-api-openapi)
