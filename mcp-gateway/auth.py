"""Authentication and public-surface routing for the OpenBrain MCP gateway.

This module owns everything about what is reachable from the public MCP
listener and what it takes to reach it. It is deliberately separate from
server.py, which holds the 19 MCP tools and the REST API.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

ALL_SCOPES = frozenset({"openbrain:read", "openbrain:write"})

_METADATA_PATH = "/.well-known/oauth-protected-resource"


class AuthConfigError(Exception):
    """Configuration is missing or inconsistent. Raised at startup so the
    process refuses to run rather than serving unauthenticated."""


def _split_csv(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    issuer: str
    jwks_url: str
    resource_uri: str
    required_scopes: frozenset[str]
    static_tokens: frozenset[str]
    jwks_cache_ttl: int

    @property
    def metadata_url(self) -> str:
        parts = urlsplit(self.resource_uri)
        return urlunsplit((parts.scheme, parts.netloc, _METADATA_PATH, "", ""))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AuthConfig":
        env = os.environ if env is None else env
        enabled = env.get("MCP_AUTH_ENABLED", "true").strip().lower() not in (
            "false", "0", "no",
        )

        issuer = env.get("MCP_OAUTH_ISSUER", "").strip()
        jwks_url = env.get("MCP_OAUTH_JWKS_URL", "").strip()
        resource_uri = env.get("MCP_RESOURCE_URI", "").strip()

        if enabled:
            missing = [
                name for name, value in (
                    ("MCP_OAUTH_ISSUER", issuer),
                    ("MCP_OAUTH_JWKS_URL", jwks_url),
                    ("MCP_RESOURCE_URI", resource_uri),
                ) if not value
            ]
            if missing:
                raise AuthConfigError(
                    "MCP_AUTH_ENABLED is true but these are unset: "
                    + ", ".join(missing)
                    + ". Set them, or set MCP_AUTH_ENABLED=false to run "
                    "without authentication on a private network."
                )

        return cls(
            enabled=enabled,
            issuer=issuer,
            jwks_url=jwks_url,
            resource_uri=resource_uri,
            required_scopes=frozenset(
                env.get("MCP_REQUIRED_SCOPES", "openbrain:read").split()
            ),
            static_tokens=_split_csv(env.get("MCP_STATIC_TOKENS", "")),
            jwks_cache_ttl=int(env.get("MCP_JWKS_CACHE_TTL", "3600")),
        )


@dataclass(frozen=True)
class Principal:
    """Who is making this request, and what they may do."""
    subject: str
    scopes: frozenset[str]
    method: str  # "static" or "oauth"


class AuthError(Exception):
    status_code = 401

    def __init__(self, message: str, config: "AuthConfig"):
        super().__init__(message)
        self.config = config

    def www_authenticate(self) -> str:
        return (
            f'Bearer resource_metadata="{self.config.metadata_url}", '
            f'scope="{" ".join(sorted(self.config.required_scopes))}"'
        )


class Unauthorized(AuthError):
    status_code = 401


class InsufficientScope(AuthError):
    status_code = 403

    def __init__(self, needed: frozenset[str], config: "AuthConfig"):
        super().__init__("insufficient scope", config)
        self.needed = needed

    def www_authenticate(self) -> str:
        return (
            'Bearer error="insufficient_scope", '
            f'scope="{" ".join(sorted(self.needed))}", '
            f'resource_metadata="{self.config.metadata_url}"'
        )


class JWKSUnavailable(Exception):
    """The signing keys could not be fetched and nothing is cached. This is
    a 503, not a 401 — the client's token may be perfectly valid, and
    telling it otherwise sends it into a pointless reauthorization loop."""


def resource_metadata_document(config: AuthConfig) -> dict:
    return {
        "resource": config.resource_uri,
        "authorization_servers": [config.issuer],
        "scopes_supported": sorted(ALL_SCOPES),
        "bearer_methods_supported": ["header"],
    }


async def _send_json(send, status: int, payload: dict, extra_headers=()):
    body = json.dumps(payload).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def make_metadata_app(config: AuthConfig):
    """RFC 9728 protected resource metadata. Served without authentication —
    a client cannot authenticate until it has read this."""
    document = resource_metadata_document(config)

    async def metadata_app(scope, receive, send):
        if scope["type"] == "lifespan":
            return
        await _send_json(send, 200, document)

    return metadata_app


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def make_auth_middleware(app, config: AuthConfig, authenticate):
    """Wrap the MCP app. `authenticate` is an async callable taking
    (authorization_header, config) and returning a Principal or raising
    an AuthError."""

    async def middleware(scope, receive, send):
        if scope["type"] == "lifespan":
            await app(scope, receive, send)
            return
        if not config.enabled:
            await app(scope, receive, send)
            return

        try:
            principal = await authenticate(_header(scope, b"authorization"), config)
        except AuthError as exc:
            await _send_json(
                send,
                exc.status_code,
                {"error": str(exc)},
                [(b"www-authenticate", exc.www_authenticate().encode())],
            )
            return
        except JWKSUnavailable:
            await _send_json(
                send,
                503,
                {"error": "signing keys unavailable; retry shortly"},
            )
            return

        scope["state"] = dict(scope.get("state") or {})
        scope["state"]["principal"] = principal
        await app(scope, receive, send)

    return middleware


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
        if path.startswith(_METADATA_PATH):
            await metadata_app(scope, receive, send)
            return
        if path == "/mcp" or path.startswith("/mcp/"):
            await mcp_app(scope, receive, send)
            return
        await not_found(scope, receive, send)

    return listener
