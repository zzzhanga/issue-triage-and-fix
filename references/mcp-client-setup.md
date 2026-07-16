# Feishu Project MCP Client Setup

Use this reference only when live Feishu Project access is required and the current client has no compatible MCP tools, is unauthenticated, or the user explicitly asks to configure the connection. The issue workflow itself stays client-neutral.

## Contents

- [Shared Contract](#shared-contract)
- [Codex](#codex)
- [Cursor](#cursor)
- [Claude Code](#claude-code)
- [Other MCP Clients](#other-mcp-clients)
- [Verify And Fail Fast](#verify-and-fail-fast)

## Shared Contract

- Transport: Streamable HTTP / HTTP
- Endpoint: `https://project.feishu.cn/mcp_server/v1`
- Authentication header: `X-Mcp-Token`
- Recommended secret name: `FEISHU_PROJECT_MCP_TOKEN`
- Server name: prefer `feishu-project`, but do not require this exact name when matching tools.

Treat the endpoint as public connection metadata and the token as a per-user secret. Never put a real token in this skill, repository files, issue artifacts, logs, commands copied into reports, or committed client configuration. If a token was pasted into chat, source code, or an issue, revoke and replace it before continuing.

## Codex

`agents/openai.yaml` declares the non-secret dependency for Codex. Each user still needs a personal credential. Configure user-level `~/.codex/config.toml` or the Codex MCP settings UI:

```toml
[mcp_servers.feishu-project]
url = "https://project.feishu.cn/mcp_server/v1"
env_http_headers = { "X-Mcp-Token" = "FEISHU_PROJECT_MCP_TOKEN" }
```

Set `FEISHU_PROJECT_MCP_TOKEN` in an environment/secret store visible to the Codex host, restart Codex, and inspect the MCP server list. Do not replace `env_http_headers` with a committed plaintext `http_headers` token.

Official reference: <https://learn.chatgpt.com/docs/extend/mcp>

## Cursor

For local Cursor, use project `.cursor/mcp.json` or user `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "feishu-project": {
      "url": "https://project.feishu.cn/mcp_server/v1",
      "headers": {
        "X-Mcp-Token": "${env:FEISHU_PROJECT_MCP_TOKEN}"
      }
    }
  }
}
```

The environment variable must be visible to the Cursor process; a variable available only in an interactive shell may not reach a GUI-launched Cursor. Cursor cloud agents/automations can have a separate MCP integration and secret store, so do not assume local `.cursor/mcp.json` is available there and do not commit a plaintext fallback.

Official reference: <https://docs.cursor.com/context/model-context-protocol>

## Claude Code

Use a project `.mcp.json` for a shared non-secret definition or user scope for a personal definition. Claude Code requires `type: "http"` for a URL-based server:

```json
{
  "mcpServers": {
    "feishu-project": {
      "type": "http",
      "url": "https://project.feishu.cn/mcp_server/v1",
      "headers": {
        "X-Mcp-Token": "${FEISHU_PROJECT_MCP_TOKEN}"
      }
    }
  }
}
```

Set the environment variable before starting Claude Code. Project-scoped MCP servers may remain pending until the user trusts the workspace and approves the server. Verify with `claude mcp list`, `claude mcp get feishu-project`, or `/mcp`; do not let the skill wait indefinitely for approval.

Official reference: <https://code.claude.com/docs/en/mcp>

## Other MCP Clients

Configure a remote Streamable HTTP server with the shared endpoint and resolve `X-Mcp-Token` from that client's environment-variable support, encrypted secret store, or per-user connection UI. Configuration keys, environment interpolation, approval, and restart behavior are client-specific. Do not claim a Codex TOML file, Cursor JSON file, Claude Code `.mcp.json`, or locally configured credential is portable to another runtime.

If the client cannot keep the token outside committed plaintext, stop and ask the user to choose an approved secret-delivery method. Do not weaken the policy to make setup appear successful.

## Verify And Fail Fast

1. Confirm the server is enabled, authenticated, and not pending client/workspace approval.
2. Confirm a tool equivalent to `search_by_mql` is visible before a live Preview.
3. Check additional detail/comment/activity/download/write capabilities only when entering the stage that needs them.
4. On missing tools, missing environment variables, `401/403`, or startup timeout, report the client, server name, and exact visible state once, then stop.
5. Do not run `pip install`, broad field discovery, browser fallback, or repeated connection retries as a substitute for MCP setup.
