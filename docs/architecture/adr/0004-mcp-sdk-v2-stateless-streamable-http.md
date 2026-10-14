# ADR-0004: MCP with the official SDK 2.x, stateless streamable HTTP

- Status: Accepted
- Date: 2026-10-14

## Context

The portfolio standard requires the official MCP Python SDK with streamable
HTTP. SDK 2.x renamed `FastMCP` to `MCPServer` and removed the old module.
The service runs several replicas behind a load balancer without session
affinity.

## Decision

- Use `mcp>=2.3,<3` and `mcp.server.mcpserver.MCPServer`, mounted at
  `/mcp/` inside the FastAPI app, with `stateless_http=True` and
  `json_response=True`.
- Authenticate per request from the HTTP `Authorization` header (available
  through the tool `Context`) with the same authenticator and policy as
  REST, instead of the SDK's OAuth resource-server helpers, so fabric
  introspection and local policy apply identically on every interface.
- Restrict accepted Host headers (`LABQC_MCP_ALLOWED_HOSTS`) for DNS
  rebinding protection.

## Consequences

- Any replica serves any MCP request; no sticky sessions.
- Server-initiated notifications and resumable streams are not offered; the
  tools are request/response by nature.
- A later SDK major version is a contained change in `api/mcp_server.py`.
