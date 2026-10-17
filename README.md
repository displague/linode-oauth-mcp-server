# Linode RFC 9727 MCP Server

An experimental MCP server that turns the Linode API v4 OpenAPI specification into tools. The API spec is fetched from Linode's official [OpenAPI distribution](https://github.com/linode/linode-api-openapi), parsed, cached on disk, and used to build the MCP tool catalog. Documentation search, Equinix service families, and Arazzo workflows from the upstream project have been removed.

This is a hard fork of [equinix-labs/equinix-docs-mcp-server](https://github.com/equinix-labs/equinix-docs-mcp-server). It retains the upstream MIT license; see [LICENSE](LICENSE).

## Scope and discovery

The server exposes Linode API operations as `linode_<operationId>` tools. By default, FastMCP's search transform keeps the full tool catalog out of the initial list; use `search_tools` to find operations and `call_tool` to invoke them. `--tool-catalog full` lists every generated tool directly. Every paginated list tool accepts an `X-Filter` argument, a JSON string sent as Linode's [filter header](https://techdocs.akamai.com/linode-api/reference/filtering-and-sorting) (for example, `{"mine": true}`). The upstream spec declares that header on only a few operations.

The server fetches the spec on first launch and stores it in the user's cache directory (`LINODE_MCP_CACHE_DIR` overrides the default). Later starts load the cached JSON copy, which takes about a second. After startup, the server checks the upstream spec in the background with a conditional GET using the cached `ETag`. An unchanged spec costs one small `304 Not Modified` request. If the spec has changed, the server fetches and caches it, and the new spec takes effect the next time the server starts. Run `linode-rfc9727-mcp-server --update-specs` to force a fresh fetch.

The Akamai TechDocs RFC 9727 catalog at <https://techdocs.akamai.com/.well-known/api-catalog> is a Linkset that links to service catalogs and documentation. It does not currently point directly to a Linode OpenAPI file. Its `cloud-computing` subcatalog URL currently returns 404, so this Linode-only server uses the canonical OpenAPI distribution from `linode/linode-api-openapi` instead. Other Akamai APIs are intentionally out of scope and have not been validated here.

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
  --label "Linode RFC 9727 MCP Server" \
  --redirect_uri http://localhost:8765/oauth/callback \
  --public true
```

The returned `id` is public and safe to commit; do not publish a private client's secret. Users can share this public client and do not need to register their own. The callback URL must exactly match the configured `http://localhost:8765/oauth/callback`. Public OAuth returns a short-lived access token in the redirect URL and does not issue a refresh token, so users sign in again when it expires. For personal use, a Linode CLI credential or personal access token is simpler. See [Linode authentication docs](https://techdocs.akamai.com/linode-api/reference/get-started) and [Create an OAuth client](https://techdocs.akamai.com/linode-api/reference/post-client).

## Install and run

Requires Python 3.13+.

After cloning this repository, install and start it:

```sh
pip install .
linode-rfc9727-mcp-server
```

This starts the default stdio server for MCP clients that launch local processes. For ChatGPT, which connects to remote MCP endpoints, run the server over local Streamable HTTP and connect through a Secure MCP Tunnel:

```sh
linode-rfc9727-mcp-server --transport http --host 127.0.0.1 --port 8000
```

The local MCP endpoint is `http://127.0.0.1:8000/mcp`. In ChatGPT, create a custom MCP connection and select an available Secure MCP Tunnel for this endpoint; use the resulting tunnel URL in the connection. Keep the local server running while testing. The HTTP endpoint has no separate MCP bearer authentication, so use it only locally behind a private tunnel; do not expose it directly to the public internet.

For local development:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
LINODE_API_TOKEN="your-token" linode-rfc9727-mcp-server
```

The initial cache fetch requires network access. Subsequent starts use the cached specification until it is refreshed.

## MCP client example

For a stdio client, either provide a token in the process environment, configure the shared public OAuth client, or let the server use an existing Linode CLI credential:

### Codex CLI

From the repository root, register the stdio server with Codex:

```sh
codex mcp add linode -- uv run --project "$(pwd)" linode-rfc9727-mcp-server
```

If your Codex sandbox restricts writes to your home cache directory or blocks network access, use repo-local caches and fetch the spec once from your normal shell before adding the server:

```sh
UV_CACHE_DIR="$(pwd)/.uv-cache" LINODE_MCP_CACHE_DIR="$(pwd)/.linode-mcp-cache" \
  uv run --project "$(pwd)" linode-rfc9727-mcp-server --update-specs
codex mcp add linode \
  --env UV_CACHE_DIR="$(pwd)/.uv-cache" \
  --env LINODE_MCP_CACHE_DIR="$(pwd)/.linode-mcp-cache" \
  -- uv run --project "$(pwd)" linode-rfc9727-mcp-server
```

`uv` installs/syncs the project dependencies on first launch. The server reads the authenticated Linode CLI token from `~/.config/linode-cli`, so no token needs to be copied into Codex configuration. After adding it, run `codex mcp list` to confirm the registration, then restart Codex so the server is loaded. The spec and uv caches in this example are gitignored.

### Other stdio clients

```json
{
  "mcpServers": {
    "linode": {
      "command": "linode-rfc9727-mcp-server",
      "env": { "LINODE_OAUTH_CLIENT_ID": "${LINODE_OAUTH_CLIENT_ID}" }
    }
  }
}
```

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

Then run `linode-rfc9727-mcp-server --config path/to/apis.yaml --update-specs`.

## Safety and limits

The upstream OpenAPI document includes read and write operations, including operations that create billable resources or delete resources. Review the operation description and parameters before invoking a write. Grant the token only the scopes required for the work. This project is experimental; API behavior and account-level permissions still need testing against a Linode account.

## Development

```sh
pip install -e .[dev]
```

## References

- [RFC 9727: API catalog well-known URI](https://www.rfc-editor.org/rfc/rfc9727)
- [Akamai TechDocs API catalog](https://techdocs.akamai.com/.well-known/api-catalog)
- [Linode API v4 reference](https://techdocs.akamai.com/linode-api/reference/api)
- [Linode API authentication](https://techdocs.akamai.com/linode-api/reference/get-started)
- [Linode OpenAPI spec distribution](https://github.com/linode/linode-api-openapi)
