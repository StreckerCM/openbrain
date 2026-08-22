"""Authentication and public-surface routing for the OpenBrain MCP gateway.

This module owns everything about what is reachable from the public MCP
listener and what it takes to reach it. It is deliberately separate from
server.py, which holds the 19 MCP tools and the REST API.
"""

_METADATA_PREFIX = "/.well-known/oauth-protected-resource"


async def not_found(scope, receive, send):
    """Plain ASGI 404. Replaces the old catch-all that handed every
    unmatched path to the MCP application."""
    body = b'{"error":"not found"}'
    await send({
        "type": "http.response.start",
        "status": 404,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def make_mcp_listener(mcp_app, metadata_app):
    """Build the ASGI app served on the public MCP port.

    Only three things are reachable here: the MCP endpoint, the protected
    resource metadata document, and a 404.
    """

    async def listener(scope, receive, send):
        if scope["type"] == "lifespan":
            # uvicorn is configured with lifespan="off" for this app;
            # server.py owns the lifespan explicitly. Defensive only.
            return
        path = scope.get("path", "")
        if path.startswith(_METADATA_PREFIX):
            await metadata_app(scope, receive, send)
            return
        if path == "/mcp" or path.startswith("/mcp/"):
            await mcp_app(scope, receive, send)
            return
        await not_found(scope, receive, send)

    return listener
