# Remote MCP OAuth Resource Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OpenBrain's `/mcp` endpoint reachable from the public internet with OAuth 2.1 bearer-token authentication, while confining the web UI, PostgREST, Adminer, and the unauthenticated write API to the LAN and tailnet.

**Architecture:** The gateway becomes an OAuth 2.0 Resource Server. Authentik at `auth.streckercm.com` is the Authorization Server; the gateway validates RS256 JWTs against Authentik's JWKS and never handles credentials. The MCP endpoint and the write REST API move onto separate uvicorn listeners in the same process; the public ingress is configured to reach only the MCP listener, whose own routing 404s anything outside `/mcp` and the metadata prefix — see Task 9 for what this guarantee does and does not cover. A `cloudflared` sidecar provides the public path; NPMplus continues to serve LAN and tailnet clients, which is safe because enforcement lives in the application rather than at the edge.

**Tech Stack:** Python 3.12, FastMCP (`mcp[http]==1.27.0`), Starlette, uvicorn, asyncpg, httpx, PyJWT with the `crypto` extra, pytest with pytest-asyncio, Docker Compose, cloudflared.

**Spec:** `docs/superpowers/specs/2026-08-22-remote-mcp-oauth-design.md`

## Global Constraints

- **Never rebuild `mcp-gateway` with unpinned dependencies.** An unpinned rebuild pulled `mcp` 2.0.0, which removed `mcp.server.fastmcp`, and the container crash-looped. `requirements.txt` states what the service depends on; `constraints.txt` states which versions the build resolved to. Bumping either means regenerating the other.
- **Regenerate `constraints.txt` from `pip freeze` inside the `python:3.12-slim` build image**, never from a development machine — a different platform or Python version resolves a different set.
- **Extras are illegal in constraints files.** `mcp[http]==1.27.0` errors there. Bare names only, which is what `pip freeze` emits.
- **Test locally in Docker before deploying.** Nothing in this plan reaches CT 115 until the local matrix passes.
- **Fail closed.** `MCP_AUTH_ENABLED` defaults to `true`. If auth is enabled and required configuration is missing, the process must refuse to start rather than serve unauthenticated.
- **Issuer comparison is exact.** No scheme or host case folding, no default-port elision, no trailing-slash or percent-encoding normalization before comparing `iss`.
- **Canonical resource URI is `https://openbrain-mcp.streckercm.com/mcp`** — used as the `aud` value, the RFC 9728 `resource` field, and the `resource` parameter clients send.
- **Scope names are `openbrain:read` and `openbrain:write`.**
- **Do not add `0.0.0.0` back as a bind fallback** for any service.

## File Structure

| File | Responsibility |
|---|---|
| `mcp-gateway/auth.py` | **New.** Everything about the public MCP surface and its protection: config loading, JWKS caching, token validation, the ASGI auth middleware, the RFC 9728 metadata document, and the listener routing factory. Kept out of `server.py`, which is already 2,100 lines. |
| `mcp-gateway/server.py` | **Modify.** Compose the two listeners, own the lifespan explicitly, replace the catch-all router. |
| `mcp-gateway/tests/conftest.py` | **New.** RSA keypair, JWKS document, token minting, and a fake JWKS server over `httpx.MockTransport`. |
| `mcp-gateway/tests/test_auth.py` | **New.** The local test matrix. |
| `mcp-gateway/pytest.ini` | **New.** `pythonpath` and asyncio mode. |
| `mcp-gateway/requirements-dev.txt` | **New.** Test-only dependencies, never installed into the runtime image. |
| `mcp-gateway/requirements.txt` | **Modify.** Add `pyjwt[crypto]`. |
| `mcp-gateway/constraints.txt` | **Modify.** Regenerate. |
| `docker-compose.yml` | **Modify.** Bind addresses, second gateway port, `cloudflared` service. |
| `cloudflared/config.yml` | **New.** Tunnel ingress restricted to `/mcp` and the metadata paths. |
| `web-ui/nginx.conf` | **Modify.** Point write upstreams at the API listener. |
| `.env.example` | **Modify.** New variables. |
| `.gitignore` | **Modify.** Exclude tunnel credentials. |
| `README.md` | **Modify.** Remote access setup. |

---

## Task 1: Bind private services off `0.0.0.0`

Independently shippable and valuable on its own. An unauthenticated full-database read (PostgREST) and a database admin console (Adminer) should not listen on every interface of a container that is about to gain a public ingress. No application code changes.

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`

**Interfaces:**
- Consumes: nothing
- Produces: `PRIVATE_BIND` environment variable, defaulting to `127.0.0.1`, consumed by every later compose change

- [ ] **Step 1: Add `PRIVATE_BIND` to `.env.example`**

Append to `.env.example`:

```bash
# Host interface for services that must not be publicly reachable.
# On CT 115 this is the tailnet address so NPMplus on CT 127 can reach it.
# Never set this to 0.0.0.0.
PRIVATE_BIND=127.0.0.1
```

- [ ] **Step 2: Bind every published port in `docker-compose.yml`**

Replace each `ports:` entry. The `db` service:

```yaml
    ports:
      - "${PRIVATE_BIND:-127.0.0.1}:5433:5432"
```

`postgrest`:

```yaml
    ports:
      - "${PRIVATE_BIND:-127.0.0.1}:3006:3000"
```

`adminer`:

```yaml
    ports:
      - "${PRIVATE_BIND:-127.0.0.1}:3008:8080"
```

`mcp-gateway`:

```yaml
    ports:
      - "${PRIVATE_BIND:-127.0.0.1}:3007:3001"
```

`web-ui`:

```yaml
    ports:
      - "${PRIVATE_BIND:-127.0.0.1}:3010:80"
```

`docs`:

```yaml
    ports:
      - "${PRIVATE_BIND:-127.0.0.1}:3009:80"
```

- [ ] **Step 3: Verify the rendered configuration**

Run: `docker compose config | grep -A2 "published"`

Expected: every `published` port shows `host_ip: 127.0.0.1`. No entry shows `0.0.0.0`.

- [ ] **Step 4: Bring the stack up and confirm services still work**

```bash
docker compose up -d
curl -sf http://127.0.0.1:3006/knowledge?limit=1 >/dev/null && echo "postgrest OK"
curl -sf http://127.0.0.1:3010/ >/dev/null && echo "web-ui OK"
```

Expected: both print OK.

- [ ] **Step 5: Confirm the ports are no longer on all interfaces**

Run: `docker compose ps --format json | grep -o '0.0.0.0:[0-9]*' || echo "no 0.0.0.0 bindings"`

Expected: `no 0.0.0.0 bindings`

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "fix: bind private services to PRIVATE_BIND instead of all interfaces

PostgREST serves an unauthenticated full read of every table and Adminer
is a database admin console; neither should listen on every interface.
Parameterized so CT 115 can pin them to its tailnet address."
```

---

## Task 2: Split MCP and REST onto separate listeners

The gateway currently serves `/mcp` and the unauthenticated write API on one port, and routes every non-`/api` path to the MCP app via a catch-all `else`. This task separates them into two uvicorn listeners in one process and replaces the catch-all with a 404. No authentication yet — the deliverable is that the two surfaces are separable, verified by the web UI still working.

**Files:**
- Create: `mcp-gateway/auth.py`
- Create: `mcp-gateway/tests/conftest.py`
- Create: `mcp-gateway/tests/test_auth.py`
- Create: `mcp-gateway/pytest.ini`
- Create: `mcp-gateway/requirements-dev.txt`
- Modify: `mcp-gateway/server.py:2074-2100`
- Modify: `web-ui/nginx.conf`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `PRIVATE_BIND` from Task 1
- Produces:
  - `auth.not_found(scope, receive, send)` — plain ASGI 404 responder
  - `auth.make_mcp_listener(mcp_app, metadata_app) -> ASGI app` — routes `/mcp` and `/mcp/*` to `mcp_app`, `/.well-known/oauth-protected-resource*` to `metadata_app`, everything else to `not_found`
  - MCP listener on container port **3001**, API listener on container port **3002**

> **Note (2026-08-22):** `requirements-dev.txt`, `pytest.ini`, `tests/`, and the
> `__pycache__`/`.venv` gitignore entries already exist on branch
> `fix/add-project-duplicate-name`. If that branch has merged, Steps 1-3 below are
> already satisfied — verify the files match and skip to Step 4. If it has not merged,
> create them here and expect a trivial conflict at merge time.

- [ ] **Step 1: Create the test dependency file**

Create `mcp-gateway/requirements-dev.txt`:

```
# Test-only dependencies. NOT installed into the runtime image — the
# Dockerfile installs requirements.txt alone. Run tests with:
#   pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt
pytest==8.4.2
pytest-asyncio==1.3.0
```

- [ ] **Step 2: Create the pytest configuration**

Create `mcp-gateway/pytest.ini`:

```ini
[pytest]
pythonpath = .
testpaths = tests
asyncio_mode = auto
```

- [ ] **Step 3: Create an empty conftest so imports resolve**

Create `mcp-gateway/tests/conftest.py`:

```python
"""Shared fixtures for mcp-gateway tests.

Populated with signing-key and JWKS fixtures in Task 3.
"""
```

- [ ] **Step 4: Write the failing routing tests**

Create `mcp-gateway/tests/test_auth.py`:

```python
import json

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import auth


def _stub(name):
    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": json.dumps({"app": name}).encode(),
        })
    return app


@pytest.fixture
def listener():
    return auth.make_mcp_listener(_stub("mcp"), _stub("metadata"))


def test_mcp_path_routes_to_mcp_app(listener):
    resp = TestClient(listener).post("/mcp")
    assert resp.status_code == 200
    assert resp.json() == {"app": "mcp"}


def test_metadata_path_routes_to_metadata_app(listener):
    resp = TestClient(listener).get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    assert resp.json() == {"app": "metadata"}


def test_metadata_path_with_resource_suffix_routes_to_metadata_app(listener):
    resp = TestClient(listener).get("/.well-known/oauth-protected-resource/mcp")
    assert resp.status_code == 200
    assert resp.json() == {"app": "metadata"}


@pytest.mark.parametrize("path", [
    "/api/bulk-delete",
    "/api/knowledge",
    "/",
    "/anything-else",
])
def test_everything_else_is_404(listener, path):
    resp = TestClient(listener).post(path)
    assert resp.status_code == 404
```

- [ ] **Step 5: Run the tests to verify they fail**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'auth'`

- [ ] **Step 6: Create `auth.py` with the routing primitives**

Create `mcp-gateway/auth.py`:

```python
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
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -v
```

Expected: PASS — 6 passed

- [ ] **Step 8: Replace the combined app in `server.py`**

In `mcp-gateway/server.py`, replace everything from `async def _combined_app` to the end of the file with:

```python
import auth


async def _placeholder_metadata(scope, receive, send):
    """Replaced with the real RFC 9728 document in Task 4."""
    await auth.not_found(scope, receive, send)


mcp_listener_app = auth.make_mcp_listener(mcp_asgi, _placeholder_metadata)

# The API listener keeps the existing Starlette app. It is private-only —
# see the deployment notes; nothing authenticates these routes.
api_listener_app = rest_app

# Kept so `server:app` still resolves for anything referencing it.
app = mcp_listener_app


async def _serve() -> None:
    import uvicorn

    # We own the lifespan rather than letting either uvicorn drive it.
    # Both servers are started with lifespan="off", so the DB pool and the
    # MCP session manager are guaranteed to be up before either listener
    # accepts a connection. Letting uvicorn run it on one app while the
    # other served traffic would be a startup race.
    async with _rest_and_mcp_lifespan(None):
        mcp_config = uvicorn.Config(
            mcp_listener_app,
            host="0.0.0.0",
            port=3001,
            log_level="info",
            lifespan="off",
        )
        api_config = uvicorn.Config(
            api_listener_app,
            host="0.0.0.0",
            port=3002,
            log_level="info",
            lifespan="off",
        )
        await asyncio.gather(
            uvicorn.Server(mcp_config).serve(),
            uvicorn.Server(api_config).serve(),
        )


if __name__ == "__main__":
    print("[startup] Applying database schema...", flush=True)
    asyncio.run(_apply_schema())
    print("[startup] MCP listener on :3001, private API listener on :3002", flush=True)
    asyncio.run(_serve())
```

Binding `0.0.0.0` inside the container is correct — the container's network namespace is not the host's, and the host-side restriction is the `PRIVATE_BIND` publication from Task 1.

- [ ] **Step 9: Expose the API port in `docker-compose.yml`**

Change the `mcp-gateway` service `ports:` to:

```yaml
    ports:
      - "${PRIVATE_BIND:-127.0.0.1}:3007:3001"
      - "${PRIVATE_BIND:-127.0.0.1}:3011:3002"
```

- [ ] **Step 10: Point the web UI at the API listener**

In `web-ui/nginx.conf`, change the two write upstreams from port 3001 to 3002. The `/api/read/` block proxying to `postgrest:3000` is unchanged.

```nginx
    location /api/write/ {
        rewrite ^/api/write/(.*)$ /api/$1 break;
        proxy_pass http://mcp-gateway:3002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Content-Type $http_content_type;
    }

    location /api/search {
        proxy_pass http://mcp-gateway:3002/api/search;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Content-Type $http_content_type;
    }
```

- [ ] **Step 11: Rebuild and verify both listeners**

```bash
docker compose up -d --build mcp-gateway web-ui
sleep 5
echo "--- write API on the API port (expect 200) ---"
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:3011/api/search \
  -H "Content-Type: application/json" -d '{"query":"test"}'
echo "--- write API on the MCP port (expect 404) ---"
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE http://127.0.0.1:3007/api/bulk-delete
echo "--- unmatched path on the MCP port (expect 404) ---"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3007/
```

Expected: `200`, then `404`, then `404`.

- [ ] **Step 12: Verify the web UI end to end**

Open `http://127.0.0.1:3010/` and confirm: the dashboard loads, a knowledge entry can be created, edited, and archived, and search returns results. These exercise `/api/read/`, `/api/write/`, and `/api/search` respectively.

- [ ] **Step 13: Commit**

```bash
git add mcp-gateway/auth.py mcp-gateway/server.py mcp-gateway/pytest.ini \
        mcp-gateway/requirements-dev.txt mcp-gateway/tests \
        web-ui/nginx.conf docker-compose.yml
git commit -m "refactor: split MCP endpoint and write API onto separate listeners

The gateway served /mcp and the unauthenticated write API on one port,
with a catch-all routing every non-/api path into the MCP app. Splitting
them means a public ingress pointed at the MCP port has no route to
/api/* regardless of how it is configured, rather than relying on a path
allowlist being correct.

Replaces the catch-all with a 404 and moves lifespan ownership out of
uvicorn so both listeners start after the DB pool and MCP session
manager are up."
```

---

## Task 3: Load and validate auth configuration, fail closed

**Files:**
- Modify: `mcp-gateway/auth.py`
- Modify: `mcp-gateway/tests/test_auth.py`
- Modify: `mcp-gateway/requirements.txt`
- Modify: `mcp-gateway/constraints.txt`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `auth.make_mcp_listener` from Task 2
- Produces:
  - `auth.AuthConfigError` — exception raised for invalid configuration
  - `auth.ALL_SCOPES: frozenset[str]` — `{"openbrain:read", "openbrain:write"}`
  - `auth.AuthConfig` dataclass with fields `enabled: bool`, `issuer: str`, `jwks_url: str`, `resource_uri: str`, `required_scopes: frozenset[str]`, `static_tokens: frozenset[str]`, `jwks_cache_ttl: int`, `metadata_url: str`
  - `auth.AuthConfig.from_env(env: Mapping[str, str]) -> AuthConfig`

- [ ] **Step 1: Add PyJWT to the runtime requirements**

In `mcp-gateway/requirements.txt`, add below `asyncpg==0.31.0`:

```
pyjwt[crypto]==2.12.1
```

- [ ] **Step 2: Capture a rollback reference before rebuilding**

```bash
docker compose exec mcp-gateway pip freeze > mcp-gateway-pins-known-good.txt
```

Expected: a file listing the currently running, verified dependency set. This is the artifact to restore from if the rebuild goes wrong.

- [ ] **Step 3: Rebuild and regenerate `constraints.txt`**

The freeze must come from the build image, not a development machine.

The existing `constraints.txt` pins the old set, so a normal build fails on the conflict
until it is regenerated. Resolve the new set with requirements alone first:

```bash
docker build -t openbrain-gateway-resolve -f - mcp-gateway <<'DOCKERFILE'
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
DOCKERFILE

docker run --rm openbrain-gateway-resolve pip freeze > /tmp/freeze.txt
grep -icE "^(pyjwt|cryptography)" /tmp/freeze.txt
```

Expected: `2` — both `PyJWT` and `cryptography` resolved.

- [ ] **Step 4: Rebuild `constraints.txt` preserving its header**

The header is the explanatory comment block; everything after it is pins. Detect the
boundary rather than hardcoding a line count, so a header edit cannot silently truncate
a comment or duplicate a pin.

```bash
cd mcp-gateway
first_pin=$(grep -n '^[a-zA-Z]' constraints.txt | head -1 | cut -d: -f1)
head -n "$((first_pin - 1))" constraints.txt > constraints.txt.new
cat /tmp/freeze.txt >> constraints.txt.new
mv constraints.txt.new constraints.txt
grep -icE "^(pyjwt|cryptography)" constraints.txt
head -3 constraints.txt
```

Expected: `2`, then the first three lines are still the comment block. Confirm no line
in the file contains an extra (`[`), which is illegal in a constraints file.

- [ ] **Step 5: Verify the real image builds with constraints applied**

```bash
docker compose build mcp-gateway
```

Expected: build succeeds. `cryptography` is a compiled wheel — if it builds from source or fails, the manylinux wheel is unavailable for this architecture and that must be resolved before continuing.

- [ ] **Step 6: Write the failing configuration tests**

Append to `mcp-gateway/tests/test_auth.py`:

```python
BASE_ENV = {
    "MCP_AUTH_ENABLED": "true",
    "MCP_OAUTH_ISSUER": "https://auth.example.com/application/o/openbrain-mcp/",
    "MCP_OAUTH_JWKS_URL": "https://auth.example.com/application/o/openbrain-mcp/jwks/",
    "MCP_RESOURCE_URI": "https://openbrain-mcp.example.com/mcp",
}


def test_config_loads_from_env():
    cfg = auth.AuthConfig.from_env(BASE_ENV)
    assert cfg.enabled is True
    assert cfg.issuer == BASE_ENV["MCP_OAUTH_ISSUER"]
    assert cfg.resource_uri == "https://openbrain-mcp.example.com/mcp"
    assert cfg.required_scopes == frozenset({"openbrain:read"})
    assert cfg.static_tokens == frozenset()
    assert cfg.jwks_cache_ttl == 3600


def test_config_defaults_to_enabled():
    env = {k: v for k, v in BASE_ENV.items() if k != "MCP_AUTH_ENABLED"}
    assert auth.AuthConfig.from_env(env).enabled is True


@pytest.mark.parametrize("missing", [
    "MCP_OAUTH_ISSUER",
    "MCP_OAUTH_JWKS_URL",
    "MCP_RESOURCE_URI",
])
def test_enabled_with_missing_setting_raises(missing):
    env = {k: v for k, v in BASE_ENV.items() if k != missing}
    with pytest.raises(auth.AuthConfigError) as exc:
        auth.AuthConfig.from_env(env)
    assert missing in str(exc.value)


def test_disabled_does_not_require_settings():
    cfg = auth.AuthConfig.from_env({"MCP_AUTH_ENABLED": "false"})
    assert cfg.enabled is False


def test_static_tokens_are_split_and_stripped():
    env = dict(BASE_ENV, MCP_STATIC_TOKENS=" tok-a , tok-b ,, ")
    assert auth.AuthConfig.from_env(env).static_tokens == frozenset({"tok-a", "tok-b"})


def test_required_scopes_are_split():
    env = dict(BASE_ENV, MCP_REQUIRED_SCOPES="openbrain:read openbrain:write")
    assert auth.AuthConfig.from_env(env).required_scopes == auth.ALL_SCOPES


def test_metadata_url_is_derived_from_resource_uri():
    cfg = auth.AuthConfig.from_env(BASE_ENV)
    assert cfg.metadata_url == (
        "https://openbrain-mcp.example.com/.well-known/oauth-protected-resource"
    )
```

- [ ] **Step 7: Run the tests to verify they fail**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -k config -v
```

Expected: FAIL — `AttributeError: module 'auth' has no attribute 'AuthConfig'`

- [ ] **Step 8: Implement the configuration**

Add to the top of `mcp-gateway/auth.py`, below the docstring:

```python
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
```

- [ ] **Step 9: Run the tests to verify they pass**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -v
```

Expected: PASS — all tests, including the Task 2 routing tests.

- [ ] **Step 10: Load the config at startup in `server.py`**

In `mcp-gateway/server.py`, immediately after `import auth`, add:

```python
AUTH_CONFIG = auth.AuthConfig.from_env()
```

An `AuthConfigError` here propagates out of module import and the container exits non-zero — which is the fail-closed behavior required.

- [ ] **Step 11: Add the new variables to `.env.example`**

Append to `.env.example`:

```bash
# Remote MCP authentication. Auth is ON by default; the gateway refuses to
# start if it is enabled and the settings below are unset.
MCP_AUTH_ENABLED=true
MCP_OAUTH_ISSUER=https://auth.streckercm.com/application/o/openbrain-mcp/
MCP_OAUTH_JWKS_URL=https://auth.streckercm.com/application/o/openbrain-mcp/jwks/
MCP_RESOURCE_URI=https://openbrain-mcp.streckercm.com/mcp
MCP_REQUIRED_SCOPES=openbrain:read
MCP_JWKS_CACHE_TTL=3600

# Comma-separated tokens for headless agents that cannot complete an OAuth
# flow. Empty disables the static path entirely.
MCP_STATIC_TOKENS=
```

- [ ] **Step 12: Verify fail-closed behavior in Docker**

```bash
docker compose run --rm --no-deps \
  -e MCP_AUTH_ENABLED=true -e MCP_OAUTH_ISSUER= -e MCP_OAUTH_JWKS_URL= \
  -e MCP_RESOURCE_URI= mcp-gateway python -c "import server" ; echo "exit=$?"
```

Expected: an `AuthConfigError` naming all three variables, and a non-zero exit code.

- [ ] **Step 13: Commit**

```bash
git add mcp-gateway/auth.py mcp-gateway/server.py mcp-gateway/tests/test_auth.py \
        mcp-gateway/requirements.txt mcp-gateway/constraints.txt .env.example
git commit -m "feat: load MCP auth configuration and fail closed on startup

Auth defaults to enabled. If it is on and the issuer, JWKS URL, or
resource URI is unset, module import raises and the container exits
rather than serving unauthenticated.

Adds pyjwt[crypto] and regenerates constraints.txt from a pip freeze in
the python:3.12-slim build image."
```

---

## Task 4: Serve protected resource metadata and challenge unauthenticated requests

The MCP spec requires the server to implement RFC 9728 and to point clients at it from a `401`. This task delivers discovery with validation still stubbed to always-deny, which confirms the handshake works before any signature verification exists.

**Files:**
- Modify: `mcp-gateway/auth.py`
- Modify: `mcp-gateway/tests/test_auth.py`
- Modify: `mcp-gateway/server.py`

**Interfaces:**
- Consumes: `auth.AuthConfig` from Task 3
- Produces:
  - `auth.resource_metadata_document(config) -> dict`
  - `auth.make_metadata_app(config) -> ASGI app`
  - `auth.Unauthorized(message, config)` — exception carrying `status_code = 401` and a `www_authenticate` string
  - `auth.InsufficientScope(scopes, config)` — `status_code = 403`
  - `auth.make_auth_middleware(app, config, authenticate) -> ASGI app`

- [ ] **Step 1: Write the failing metadata and challenge tests**

Append to `mcp-gateway/tests/test_auth.py`:

```python
@pytest.fixture
def config():
    return auth.AuthConfig.from_env(BASE_ENV)


def test_metadata_document_shape(config):
    doc = auth.resource_metadata_document(config)
    assert doc["resource"] == "https://openbrain-mcp.example.com/mcp"
    assert doc["authorization_servers"] == [BASE_ENV["MCP_OAUTH_ISSUER"]]
    assert set(doc["scopes_supported"]) == auth.ALL_SCOPES
    assert doc["bearer_methods_supported"] == ["header"]


def test_metadata_app_serves_the_document_without_auth(config):
    client = TestClient(auth.make_metadata_app(config))
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    assert resp.json()["resource"] == "https://openbrain-mcp.example.com/mcp"


def test_missing_authorization_returns_401_with_challenge(config):
    async def deny(authorization, config):
        raise auth.Unauthorized("no credentials", config)

    guarded = auth.make_auth_middleware(_stub("mcp"), config, deny)
    resp = TestClient(guarded).post("/mcp")

    assert resp.status_code == 401
    challenge = resp.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'resource_metadata="https://openbrain-mcp.example.com/.well-known/oauth-protected-resource"' in challenge
    assert 'scope="openbrain:read"' in challenge


def test_insufficient_scope_returns_403_with_error(config):
    async def deny(authorization, config):
        raise auth.InsufficientScope(frozenset({"openbrain:write"}), config)

    guarded = auth.make_auth_middleware(_stub("mcp"), config, deny)
    resp = TestClient(guarded).post("/mcp")

    assert resp.status_code == 403
    challenge = resp.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="openbrain:write"' in challenge


def test_successful_authentication_passes_through(config):
    async def allow(authorization, config):
        return auth.Principal(subject="u1", scopes=auth.ALL_SCOPES, method="test")

    guarded = auth.make_auth_middleware(_stub("mcp"), config, allow)
    resp = TestClient(guarded).post("/mcp")

    assert resp.status_code == 200
    assert resp.json() == {"app": "mcp"}


def test_auth_disabled_passes_everything_through():
    cfg = auth.AuthConfig.from_env({"MCP_AUTH_ENABLED": "false"})

    async def deny(authorization, config):
        raise auth.Unauthorized("should not be called", config)

    guarded = auth.make_auth_middleware(_stub("mcp"), cfg, deny)
    assert TestClient(guarded).post("/mcp").status_code == 200
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -k "metadata or challenge or scope or pass" -v
```

Expected: FAIL — `AttributeError: module 'auth' has no attribute 'resource_metadata_document'`

- [ ] **Step 3: Implement metadata, errors, and middleware**

Add to `mcp-gateway/auth.py`. Add `import json` to the imports at the top.

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -v
```

Expected: PASS

- [ ] **Step 5: Wire the real metadata app and a deny-all validator into `server.py`**

Replace the `_placeholder_metadata` block added in Task 2 with:

```python
async def _deny_all(authorization, config):
    """Replaced with real validation in Tasks 5-8."""
    raise auth.Unauthorized("authentication not yet implemented", config)


mcp_listener_app = auth.make_mcp_listener(
    auth.make_auth_middleware(mcp_asgi, AUTH_CONFIG, _deny_all),
    auth.make_metadata_app(AUTH_CONFIG),
)
```

- [ ] **Step 6: Verify the handshake end to end**

```bash
docker compose up -d --build mcp-gateway
sleep 5
echo "--- metadata, no auth required ---"
curl -s http://127.0.0.1:3007/.well-known/oauth-protected-resource | python -m json.tool
echo "--- challenge ---"
curl -s -i -X POST http://127.0.0.1:3007/mcp | grep -i "^HTTP/\|^www-authenticate"
```

Expected: a JSON document whose `resource` matches `MCP_RESOURCE_URI`, then `HTTP/1.1 401` with a `www-authenticate` header containing both `resource_metadata` and `scope`.

- [ ] **Step 7: Commit**

```bash
git add mcp-gateway/auth.py mcp-gateway/server.py mcp-gateway/tests/test_auth.py
git commit -m "feat: serve RFC 9728 metadata and challenge unauthenticated MCP requests

Discovery and the 401 challenge land before any signature verification,
so the client handshake can be confirmed on its own. Validation is
deny-all until the next tasks.

JWKS failures are modelled as 503 rather than 401 from the start: a
valid token told it is invalid sends the client into a reauthorization
loop that will not succeed."
```

---

## Task 5: Accept static tokens for headless agents

Agents on the VPS and homelab have no browser to complete an OAuth flow. This is also the migration path that keeps existing Claude Code clients working across the cutover.

**Files:**
- Modify: `mcp-gateway/auth.py`
- Modify: `mcp-gateway/tests/test_auth.py`
- Modify: `mcp-gateway/server.py`

**Interfaces:**
- Consumes: `auth.Principal`, `auth.Unauthorized`, `auth.AuthConfig`
- Produces: `auth.bearer_token(authorization: str | None, config) -> str` and `auth.match_static_token(token: str, config) -> Principal | None`

- [ ] **Step 1: Write the failing static token tests**

Append to `mcp-gateway/tests/test_auth.py`:

```python
STATIC_ENV = dict(BASE_ENV, MCP_STATIC_TOKENS="tok-alpha,tok-beta")


@pytest.fixture
def static_config():
    return auth.AuthConfig.from_env(STATIC_ENV)


def test_bearer_token_extracted(static_config):
    assert auth.bearer_token("Bearer tok-alpha", static_config) == "tok-alpha"


def test_bearer_token_scheme_is_case_insensitive(static_config):
    assert auth.bearer_token("bearer tok-alpha", static_config) == "tok-alpha"


@pytest.mark.parametrize("header", [None, "", "Basic abc", "tok-alpha", "Bearer"])
def test_bad_authorization_header_raises_unauthorized(static_config, header):
    with pytest.raises(auth.Unauthorized):
        auth.bearer_token(header, static_config)


def test_configured_static_token_matches(static_config):
    principal = auth.match_static_token("tok-beta", static_config)
    assert principal is not None
    assert principal.method == "static"
    assert principal.scopes == auth.ALL_SCOPES


def test_unknown_token_does_not_match(static_config):
    assert auth.match_static_token("tok-unknown", static_config) is None


def test_static_tokens_disabled_when_unset(config):
    assert config.static_tokens == frozenset()
    assert auth.match_static_token("anything", config) is None


def test_empty_token_never_matches_when_disabled(config):
    assert auth.match_static_token("", config) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -k "bearer or static" -v
```

Expected: FAIL — `AttributeError: module 'auth' has no attribute 'bearer_token'`

- [ ] **Step 3: Implement token extraction and static matching**

Add `import hmac` to the imports in `mcp-gateway/auth.py`, then add:

```python
def bearer_token(authorization: str | None, config: AuthConfig) -> str:
    """Pull the credential out of an Authorization header, or raise."""
    if not authorization:
        raise Unauthorized("missing Authorization header", config)
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise Unauthorized("expected an Authorization: Bearer credential", config)
    return value.strip()


def match_static_token(token: str, config: AuthConfig) -> Principal | None:
    """Compare against configured static tokens in constant time.

    Every candidate is checked with no early exit: returning as soon as one
    matches would leak, through response timing, how far down the list a
    guess got. A plain `==` would leak the shared prefix length outright.
    """
    matched = False
    for candidate in config.static_tokens:
        if hmac.compare_digest(token, candidate):
            matched = True
    if not matched:
        return None
    return Principal(subject="static-token", scopes=ALL_SCOPES, method="static")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -v
```

Expected: PASS

- [ ] **Step 5: Replace the deny-all validator in `server.py`**

```python
async def _authenticate(authorization, config):
    token = auth.bearer_token(authorization, config)
    principal = auth.match_static_token(token, config)
    if principal is not None:
        return principal
    # JWT validation arrives in Tasks 6-8.
    raise auth.Unauthorized("credential not recognized", config)
```

And update the listener construction to use `_authenticate` instead of `_deny_all`. Delete `_deny_all`.

- [ ] **Step 6: Verify against the running container**

The gateway reads configuration through `env_file: .env`, so a shell variable on the
`docker compose` command line will not reach the container. Set it in `.env`:

```bash
grep -q '^MCP_STATIC_TOKENS=' .env \
  && sed -i 's/^MCP_STATIC_TOKENS=.*/MCP_STATIC_TOKENS=local-test-token/' .env \
  || echo 'MCP_STATIC_TOKENS=local-test-token' >> .env
docker compose up -d --build mcp-gateway
sleep 5
echo "--- valid static token (expect 200) ---"
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:3007/mcp \
  -H "Authorization: Bearer local-test-token" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
echo "--- wrong token (expect 401) ---"
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:3007/mcp \
  -H "Authorization: Bearer wrong" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expected: `200` then `401`.

What is being verified is the *difference*: the second call must be `401` and the first
must not be. If the first returns `400` with a JSON-RPC error body, authentication passed
and the MCP handshake is the problem — a different bug, in a different layer.

- [ ] **Step 7: Confirm the tool list is complete**

```bash
curl -s -X POST http://127.0.0.1:3007/mcp \
  -H "Authorization: Bearer local-test-token" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | grep -o '"name"' | wc -l
```

Expected: `19`

- [ ] **Step 8: Commit**

```bash
git add mcp-gateway/auth.py mcp-gateway/server.py mcp-gateway/tests/test_auth.py
git commit -m "feat: accept static bearer tokens for headless agents

Agents on the VPS and homelab have no browser to complete an OAuth flow.
This is also the migration path that keeps existing Claude Code clients
working across the cutover to authenticated MCP.

Comparison is constant-time with no early exit, so response timing does
not reveal how far into the candidate list a guess matched."
```

---

## Task 6: Cache Authentik's signing keys

**Files:**
- Modify: `mcp-gateway/auth.py`
- Modify: `mcp-gateway/tests/conftest.py`
- Modify: `mcp-gateway/tests/test_auth.py`

**Interfaces:**
- Consumes: `auth.JWKSUnavailable`, `auth.AuthConfig`
- Produces: `auth.JWKSCache(jwks_url, http_getter, ttl=3600, min_refetch_interval=30, clock=time.monotonic)` with `async def get_key(self, kid: str) -> jwt.PyJWK`

- [ ] **Step 1: Add signing fixtures to `conftest.py`**

Replace `mcp-gateway/tests/conftest.py` with:

```python
"""Shared fixtures for mcp-gateway tests."""
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://auth.example.com/application/o/openbrain-mcp/"
JWKS_URL = "https://auth.example.com/application/o/openbrain-mcp/jwks/"
RESOURCE = "https://openbrain-mcp.example.com/mcp"
KID = "test-key-1"
OTHER_KID = "rotated-key-2"


def _keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def signing_key():
    return _keypair()


@pytest.fixture(scope="session")
def foreign_key():
    """A key the JWKS never advertises — stands in for a forged token."""
    return _keypair()


def _jwk_for(key, kid):
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return jwk


@pytest.fixture(scope="session")
def jwks_document(signing_key):
    return {"keys": [_jwk_for(signing_key, KID)]}


class FakeJWKSServer:
    """Serves a JWKS document over httpx.MockTransport, with knobs for
    counting fetches and simulating outages."""

    def __init__(self, document):
        self.document = document
        self.calls = 0
        self.status = 200
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request):
        self.calls += 1
        if self.status != 200:
            return httpx.Response(self.status, text="unavailable")
        return httpx.Response(200, json=self.document)

    def rotate(self, key, kid):
        self.document = {"keys": [_jwk_for(key, kid)]}


@pytest.fixture
def jwks_server(jwks_document):
    return FakeJWKSServer(dict(jwks_document))


@pytest.fixture
def mint_token(signing_key):
    def _mint(key=None, kid=KID, **overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": RESOURCE,
            "sub": "user-1",
            "iat": now,
            "exp": now + 300,
            "scope": "openbrain:read openbrain:write",
        }
        claims.update(overrides)
        return jwt.encode(
            claims, key or signing_key, algorithm="RS256", headers={"kid": kid}
        )
    return _mint
```

- [ ] **Step 2: Write the failing JWKS cache tests**

Append to `mcp-gateway/tests/test_auth.py`:

```python
from conftest import JWKS_URL, KID, OTHER_KID


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


async def test_jwks_fetches_and_returns_key(jwks_server):
    cache = auth.JWKSCache(JWKS_URL, lambda: jwks_server.client)
    key = await cache.get_key(KID)
    assert key.key_id == KID
    assert jwks_server.calls == 1


async def test_jwks_is_cached_between_calls(jwks_server):
    cache = auth.JWKSCache(JWKS_URL, lambda: jwks_server.client)
    await cache.get_key(KID)
    await cache.get_key(KID)
    assert jwks_server.calls == 1


async def test_jwks_refetches_after_ttl(jwks_server):
    clock = FakeClock()
    cache = auth.JWKSCache(JWKS_URL, lambda: jwks_server.client, ttl=100, clock=clock)
    await cache.get_key(KID)
    clock.advance(101)
    await cache.get_key(KID)
    assert jwks_server.calls == 2


async def test_unknown_kid_triggers_one_refetch(jwks_server, foreign_key):
    clock = FakeClock()
    cache = auth.JWKSCache(
        JWKS_URL, lambda: jwks_server.client, min_refetch_interval=30, clock=clock
    )
    await cache.get_key(KID)
    jwks_server.rotate(foreign_key, OTHER_KID)
    clock.advance(31)
    key = await cache.get_key(OTHER_KID)
    assert key.key_id == OTHER_KID
    assert jwks_server.calls == 2


async def test_unknown_kid_is_rate_limited(jwks_server):
    clock = FakeClock()
    cache = auth.JWKSCache(
        JWKS_URL, lambda: jwks_server.client, min_refetch_interval=30, clock=clock
    )
    await cache.get_key(KID)
    clock.advance(31)  # past the floor, so the first garbage kid may refetch
    for _ in range(20):
        with pytest.raises(auth.JWKSUnavailable):
            await cache.get_key("garbage-kid")
    # One refetch attempt, not twenty: junk kids must not become an
    # outbound request amplifier.
    assert jwks_server.calls == 2


async def test_serves_stale_cache_when_fetch_fails(jwks_server):
    clock = FakeClock()
    cache = auth.JWKSCache(JWKS_URL, lambda: jwks_server.client, ttl=100, clock=clock)
    await cache.get_key(KID)
    jwks_server.status = 500
    clock.advance(101)
    key = await cache.get_key(KID)
    assert key.key_id == KID


async def test_raises_when_fetch_fails_with_cold_cache(jwks_server):
    jwks_server.status = 500
    cache = auth.JWKSCache(JWKS_URL, lambda: jwks_server.client)
    with pytest.raises(auth.JWKSUnavailable):
        await cache.get_key(KID)


async def test_ttl_expiry_refetch_is_rate_limited_during_outage(jwks_server):
    """A sustained outage past ttl must not turn every get_key call for an
    already-cached kid into a fresh outbound fetch attempt: the stale key
    should keep being served, and fetch attempts should stay bounded by
    min_refetch_interval, not scale with call count."""
    clock = FakeClock()
    cache = auth.JWKSCache(
        JWKS_URL, lambda: jwks_server.client, ttl=100, min_refetch_interval=30, clock=clock
    )
    await cache.get_key(KID)
    jwks_server.status = 500
    clock.advance(101)  # past ttl; server is down
    for _ in range(5):
        key = await cache.get_key(KID)
        assert key.key_id == KID
    # One fetch attempt during the outage window despite 5 calls for a
    # cached kid -- the TTL path must be rate-limited like the unknown-kid
    # path, not fire on every request.
    assert jwks_server.calls == 2

    jwks_server.status = 200
    clock.advance(31)  # past min_refetch_interval: throttle reopens
    key = await cache.get_key(KID)
    assert key.key_id == KID
    assert jwks_server.calls == 3
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -k jwks -v
```

Expected: FAIL — `AttributeError: module 'auth' has no attribute 'JWKSCache'`

- [ ] **Step 4: Implement the cache**

Add `import time` and `import jwt` to the imports in `mcp-gateway/auth.py`, then add:

```python
class JWKSCache:
    """Fetches and caches the authorization server's signing keys.

    PyJWT ships PyJWKClient, but it fetches over blocking urllib, which
    would stall the event loop on every cache miss. This uses the
    httpx.AsyncClient the gateway already holds.
    """

    def __init__(
        self,
        jwks_url: str,
        http_getter,
        ttl: int = 3600,
        min_refetch_interval: int = 30,
        clock=time.monotonic,
    ):
        self._url = jwks_url
        self._http_getter = http_getter
        self._ttl = ttl
        self._min_refetch_interval = min_refetch_interval
        self._clock = clock
        self._keys: dict[str, "jwt.PyJWK"] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None

    async def _fetch(self) -> bool:
        """Refresh the key set. Returns True on success. Never raises for
        a network or parse failure — the caller decides whether a stale
        cache is good enough."""
        self._last_attempt = self._clock()
        try:
            response = await self._http_getter().get(self._url, timeout=10.0)
            response.raise_for_status()
            key_set = jwt.PyJWKSet.from_dict(response.json())
        except Exception as exc:  # network, HTTP, JSON, or key parse
            print(f"[auth] JWKS fetch failed: {exc}", flush=True)
            return False
        self._keys = {k.key_id: k for k in key_set.keys if k.key_id}
        self._fetched_at = self._clock()
        return True

    def _expired(self) -> bool:
        return self._fetched_at is None or (
            self._clock() - self._fetched_at >= self._ttl
        )

    def _may_refetch(self) -> bool:
        return self._last_attempt is None or (
            self._clock() - self._last_attempt >= self._min_refetch_interval
        )

    async def get_key(self, kid: str) -> "jwt.PyJWK":
        if self._expired() and self._may_refetch():
            # A failure here is survivable if we still hold keys. Gated by
            # _may_refetch() too: without it, a sustained outage past ttl
            # would trigger a fresh 10s-timeout fetch attempt on every
            # request, stalling requests that already have a valid cached
            # key -- exactly the amplifier min_refetch_interval exists to
            # prevent, just reached via the TTL path instead of unknown-kid.
            await self._fetch()

        if kid in self._keys:
            return self._keys[kid]

        # Unknown kid: the AS may have rotated. Refetch once, rate-limited
        # so junk kids cannot drive unbounded outbound requests.
        if self._may_refetch() and await self._fetch() and kid in self._keys:
            return self._keys[kid]

        raise JWKSUnavailable(f"no signing key for kid={kid!r}")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcp-gateway/auth.py mcp-gateway/tests/
git commit -m "feat: cache authorization server signing keys

Uses the existing httpx.AsyncClient rather than PyJWKClient, whose
blocking urllib fetch would stall the event loop on every cache miss.

Absorbs key rotation by refetching on an unknown kid, rate-limited so a
client sending junk kid values cannot turn the gateway into an outbound
request amplifier. A fetch failure with warm cache serves stale keys; a
cold cache raises JWKSUnavailable, which the middleware renders as 503."
```

---

## Task 7: Validate OAuth access tokens

The audience check is the load-bearing one. The MCP spec requires that servers accept only tokens minted for them, which is what stops a token issued for a different Authentik application from working here.

**Files:**
- Modify: `mcp-gateway/auth.py`
- Modify: `mcp-gateway/tests/test_auth.py`
- Modify: `mcp-gateway/server.py`

**Interfaces:**
- Consumes: `auth.JWKSCache`, `auth.Principal`, `auth.Unauthorized`
- Produces: `auth.validate_jwt(token: str, config, jwks: JWKSCache) -> Principal` and `auth.extract_scopes(claims: dict) -> frozenset[str]`

- [ ] **Step 1: Write the failing validation tests**

Append to `mcp-gateway/tests/test_auth.py`:

```python
import time as _time

from conftest import ISSUER, RESOURCE

JWT_ENV = dict(
    BASE_ENV,
    MCP_OAUTH_ISSUER=ISSUER,
    MCP_OAUTH_JWKS_URL=JWKS_URL,
    MCP_RESOURCE_URI=RESOURCE,
)


@pytest.fixture
def jwt_config():
    return auth.AuthConfig.from_env(JWT_ENV)


@pytest.fixture
def jwks(jwks_server):
    return auth.JWKSCache(JWKS_URL, lambda: jwks_server.client)


async def test_valid_token_yields_principal(jwt_config, jwks, mint_token):
    principal = await auth.validate_jwt(mint_token(), jwt_config, jwks)
    assert principal.subject == "user-1"
    assert principal.method == "oauth"
    assert principal.scopes == auth.ALL_SCOPES


async def test_wrong_audience_rejected(jwt_config, jwks, mint_token):
    token = mint_token(aud="https://someone-elses-server.example.com/mcp")
    with pytest.raises(auth.Unauthorized):
        await auth.validate_jwt(token, jwt_config, jwks)


async def test_audience_as_list_containing_resource_accepted(jwt_config, jwks, mint_token):
    token = mint_token(aud=["https://other.example.com", RESOURCE])
    principal = await auth.validate_jwt(token, jwt_config, jwks)
    assert principal.subject == "user-1"


async def test_missing_audience_rejected(jwt_config, jwks, mint_token):
    with pytest.raises(auth.Unauthorized):
        await auth.validate_jwt(mint_token(aud=None), jwt_config, jwks)


async def test_wrong_issuer_rejected(jwt_config, jwks, mint_token):
    token = mint_token(iss="https://evil.example.com/application/o/openbrain-mcp/")
    with pytest.raises(auth.Unauthorized):
        await auth.validate_jwt(token, jwt_config, jwks)


async def test_issuer_trailing_slash_difference_rejected(jwt_config, jwks, mint_token):
    # The spec forbids normalizing before comparison. A near-miss is a miss.
    token = mint_token(iss=ISSUER.rstrip("/"))
    with pytest.raises(auth.Unauthorized):
        await auth.validate_jwt(token, jwt_config, jwks)


async def test_expired_token_rejected(jwt_config, jwks, mint_token):
    now = int(_time.time())
    token = mint_token(exp=now - 600, iat=now - 900)
    with pytest.raises(auth.Unauthorized):
        await auth.validate_jwt(token, jwt_config, jwks)


async def test_token_signed_by_unknown_key_rejected(
    jwt_config, jwks, mint_token, foreign_key
):
    token = mint_token(key=foreign_key, kid=KID)
    with pytest.raises(auth.Unauthorized):
        await auth.validate_jwt(token, jwt_config, jwks)


async def test_garbage_token_rejected(jwt_config, jwks):
    with pytest.raises(auth.Unauthorized):
        await auth.validate_jwt("not-a-jwt", jwt_config, jwks)


async def test_unsigned_token_rejected(jwt_config, jwks):
    # alg=none must never be honoured. PyJWT requires key=None to encode it.
    # A kid is required so the token reaches jwt.decode() -- otherwise this
    # test would only prove the kid guard works, not the algorithm allowlist.
    token = jwt.encode(
        {"sub": "x", "aud": RESOURCE, "iss": ISSUER},
        key=None,
        algorithm="none",
        headers={"kid": KID},
    )
    with pytest.raises(auth.Unauthorized):
        await auth.validate_jwt(token, jwt_config, jwks)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


async def test_algorithm_confusion_hs256_with_public_key_rejected(
    jwt_config, jwks, signing_key
):
    """Classic RS256-to-HS256 confusion: sign with HS256 using the RSA
    public key's PEM bytes as the HMAC secret. An attacker can obtain the
    public key from the JWKS document, so if `algorithms` were ever
    derived from the token header instead of hardcoded to ["RS256"], this
    forged token would validate.

    PyJWT's own encoder refuses to build this token (it detects a
    PEM-shaped HMAC key and raises), so the forgery is assembled by hand
    to exercise the server's allowlist rather than the client library's
    unrelated guard.
    """
    public_pem = signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    now = int(_time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": KID}
    payload = {
        "sub": "user-1",
        "aud": RESOURCE,
        "iss": ISSUER,
        "iat": now,
        "exp": now + 300,
    }
    signing_input = (
        f"{_b64url(json.dumps(header).encode())}."
        f"{_b64url(json.dumps(payload).encode())}"
    )
    signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    token = f"{signing_input}.{_b64url(signature)}"

    with pytest.raises(auth.Unauthorized):
        await auth.validate_jwt(token, jwt_config, jwks)


def test_extract_scopes_from_space_delimited_string():
    assert auth.extract_scopes({"scope": "a b"}) == frozenset({"a", "b"})


def test_extract_scopes_from_scp_list():
    assert auth.extract_scopes({"scp": ["a", "b"]}) == frozenset({"a", "b"})


def test_extract_scopes_when_absent():
    assert auth.extract_scopes({}) == frozenset()
```

Add `import base64`, `import hashlib`, `import hmac`, `import jwt`, and `from cryptography.hazmat.primitives import serialization` to the top of `tests/test_auth.py` (`json` and `pytest` are already imported there).

The `alg=none` test must carry a `kid` — without one, `validate_jwt` rejects it at the `if not kid` guard before `jwt.decode` ever runs, so the test would pass even if `algorithms` were mistakenly derived from the token header instead of hardcoded. The `alg=HS256`-with-public-key test is the other half of that same regression check: it is the classic RS256-to-HS256 key-confusion attack, and nothing else in the suite exercises it.

Sanity-check both before moving on: temporarily widen `algorithms=["RS256"]` to `algorithms=["RS256", "none", "HS256"]` in `auth.py` and confirm at least the HS256-confusion test breaks (it will raise an unhandled `TypeError` rather than the `Unauthorized` the test expects, because `validate_jwt` always passes the resolved JWKS key object, not raw PEM bytes, to `jwt.decode`). The `alg=none` test may keep passing even under this widening — PyJWT's own `NoneAlgorithm.prepare_key` rejects a non-empty key, and `validate_jwt` always supplies the real resolved signing key, so an unsigned token can never validate through this code path regardless of what the `algorithms` allowlist contains. That is a second, independent line of defense, not a gap in the test. Revert the widening afterwards; do not commit it.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -k "validate or token or scopes or audience or issuer" -v
```

Expected: FAIL — `AttributeError: module 'auth' has no attribute 'validate_jwt'`

- [ ] **Step 3: Implement validation**

Add to `mcp-gateway/auth.py`:

```python
def extract_scopes(claims: dict) -> frozenset[str]:
    """Authentik emits a space-delimited `scope` string; some servers emit
    an `scp` array. Accept either."""
    raw = claims.get("scope") or claims.get("scp") or ""
    if isinstance(raw, str):
        return frozenset(raw.split())
    return frozenset(raw)


async def validate_jwt(token: str, config: AuthConfig, jwks: JWKSCache) -> Principal:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        # The client gets one undifferentiated 401; the detail goes to the
        # log, not the response.
        print(f"[auth] token rejected: malformed token: {exc}", flush=True)
        raise Unauthorized("token rejected", config) from exc

    kid = header.get("kid")
    if not kid:
        print("[auth] token rejected: token header has no kid", flush=True)
        raise Unauthorized("token rejected", config)

    # A failure to resolve the key is JWKSUnavailable, which the middleware
    # renders as 503. Do not convert it to 401 here.
    signing_key = await jwks.get_key(kid)

    try:
        claims = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=["RS256"],
            audience=config.resource_uri,
            issuer=config.issuer,
            leeway=30,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # Covers bad signature, wrong audience, wrong issuer, expiry, and
        # missing required claims. The client gets one undifferentiated
        # 401; the detail goes to the log, not the response.
        print(f"[auth] token rejected: {exc}", flush=True)
        raise Unauthorized("token rejected", config) from exc

    return Principal(
        subject=str(claims["sub"]),
        scopes=extract_scopes(claims),
        method="oauth",
    )
```

Note the `algorithms=["RS256"]` allowlist — it is what makes an `alg: none` token fail rather than validate.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -v
```

Expected: PASS

- [ ] **Step 5: Wire validation into `server.py`**

Add near the other module-level singletons, after `AUTH_CONFIG`:

```python
JWKS_CACHE = auth.JWKSCache(
    AUTH_CONFIG.jwks_url,
    _get_http,
    ttl=AUTH_CONFIG.jwks_cache_ttl,
)
```

`_get_http` already exists in `server.py` and asserts the REST app context is initialized, which by the lifespan ordering it always is by the time a request arrives.

Replace `_authenticate` with:

```python
async def _authenticate(authorization, config):
    token = auth.bearer_token(authorization, config)
    principal = auth.match_static_token(token, config)
    if principal is not None:
        return principal
    return await auth.validate_jwt(token, config, JWKS_CACHE)
```

- [ ] **Step 6: Commit**

```bash
git add mcp-gateway/auth.py mcp-gateway/server.py mcp-gateway/tests/test_auth.py
git commit -m "feat: validate OAuth access tokens against Authentik

Verifies RS256 signature, exact issuer match with no normalization,
expiry, and — the load-bearing check — that the token's audience is this
server's canonical URI. Without the audience check a token minted for
any other Authentik application would work here.

The RS256 algorithm allowlist is what makes an alg=none token fail
rather than validate. Rejection reasons go to the log; the client gets
one undifferentiated 401."
```

---

## Task 8: Enforce required scopes

**Files:**
- Modify: `mcp-gateway/auth.py`
- Modify: `mcp-gateway/tests/test_auth.py`
- Modify: `mcp-gateway/server.py`

**Interfaces:**
- Consumes: `auth.Principal`, `auth.InsufficientScope`
- Produces: `auth.require_scopes(principal: Principal, config) -> Principal`

- [ ] **Step 1: Write the failing scope tests**

Append to `mcp-gateway/tests/test_auth.py`:

```python
def test_principal_with_required_scope_passes(jwt_config):
    principal = auth.Principal(
        subject="u", scopes=frozenset({"openbrain:read"}), method="oauth"
    )
    assert auth.require_scopes(principal, jwt_config) is principal


def test_principal_missing_required_scope_raises(jwt_config):
    principal = auth.Principal(
        subject="u", scopes=frozenset({"openbrain:write"}), method="oauth"
    )
    with pytest.raises(auth.InsufficientScope) as exc:
        auth.require_scopes(principal, jwt_config)
    assert exc.value.needed == frozenset({"openbrain:read"})


def test_principal_with_no_scopes_raises(jwt_config):
    principal = auth.Principal(subject="u", scopes=frozenset(), method="oauth")
    with pytest.raises(auth.InsufficientScope):
        auth.require_scopes(principal, jwt_config)


def test_only_missing_scopes_are_challenged():
    cfg = auth.AuthConfig.from_env(
        dict(JWT_ENV, MCP_REQUIRED_SCOPES="openbrain:read openbrain:write")
    )
    principal = auth.Principal(
        subject="u", scopes=frozenset({"openbrain:read"}), method="oauth"
    )
    with pytest.raises(auth.InsufficientScope) as exc:
        auth.require_scopes(principal, cfg)
    assert exc.value.needed == frozenset({"openbrain:write"})


def test_static_token_principal_satisfies_scopes(static_config):
    principal = auth.match_static_token("tok-alpha", static_config)
    assert auth.require_scopes(principal, static_config) is principal
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -k scope -v
```

Expected: FAIL — `AttributeError: module 'auth' has no attribute 'require_scopes'`

- [ ] **Step 3: Implement scope enforcement**

Add to `mcp-gateway/auth.py`:

```python
def require_scopes(principal: Principal, config: AuthConfig) -> Principal:
    """Challenge with only the scopes actually missing, not the whole
    required set — the spec asks servers to name what the current
    operation needs, and a client unions the challenge with what it
    already holds."""
    missing = config.required_scopes - principal.scopes
    if missing:
        raise InsufficientScope(frozenset(missing), config)
    return principal
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd mcp-gateway
python -m pytest tests/test_auth.py -v
```

Expected: PASS — the full suite.

- [ ] **Step 5: Apply scope enforcement in `server.py`**

```python
async def _authenticate(authorization, config):
    token = auth.bearer_token(authorization, config)
    principal = auth.match_static_token(token, config)
    if principal is None:
        principal = await auth.validate_jwt(token, config, JWKS_CACHE)
    return auth.require_scopes(principal, config)
```

- [ ] **Step 6: Run the whole suite one more time and check coverage of the matrix**

```bash
cd mcp-gateway
python -m pytest tests/ -v
```

Expected: PASS. Confirm the suite covers spec §9 cases 1–8, 9–10, 16, 17.

- [ ] **Step 7: Commit**

```bash
git add mcp-gateway/auth.py mcp-gateway/server.py mcp-gateway/tests/test_auth.py
git commit -m "feat: enforce required scopes with a 403 insufficient_scope challenge

Challenges name only the scopes actually missing rather than the whole
required set, so a client can union the challenge with what it already
holds instead of re-requesting everything."
```

---

## Task 9: Add the cloudflared sidecar

The tunnel is an outbound connection, so the homelab keeps no listening port on its public interface. The ingress rules are the only line of defence against the write API being reached through the tunnel: `cloudflared` and `mcp-gateway` share Compose's default network, so `cloudflared` has the same network-level reachability to port 3002 as it does to 3001. What actually protects the write API is that the ingress rule targets port 3001, whose listener 404s everything outside `/mcp` and the metadata prefix — reaching `/api/*` through the tunnel would require someone to explicitly add a rule naming `:3002`.

**Files:**
- Create: `cloudflared/config.yml`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: the MCP listener on container port 3001 from Task 2
- Produces: `https://openbrain-mcp.streckercm.com` reaching `/mcp` and the metadata paths only

- [ ] **Step 1: Create the tunnel ingress configuration**

Create `cloudflared/config.yml`:

```yaml
# Locally-managed tunnel configuration, kept in the repo so the public
# surface is reviewable in version control rather than living in a
# dashboard. Replace TUNNEL_ID with the id from `cloudflared tunnel create`.
tunnel: TUNNEL_ID
credentials-file: /etc/cloudflared/credentials.json

# Rules match in order; the first match wins and the last rule is the
# catch-all. cloudflared reaches mcp-gateway over the compose network; it
# has the same network-level reachability to port 3002 as it does to 3001
# (Compose's default network has no per-service isolation). What keeps the
# write API private is that these rules only ever target 3001, whose
# listener 404s anything outside /mcp and the metadata prefix — exposing
# /api/* would require someone to add a rule naming :3002 explicitly.
ingress:
  - hostname: openbrain-mcp.streckercm.com
    path: ^/mcp$
    service: http://mcp-gateway:3001

  - hostname: openbrain-mcp.streckercm.com
    path: ^/\.well-known/oauth-protected-resource
    service: http://mcp-gateway:3001

  - service: http_status:404
```

- [ ] **Step 2: Add the service to `docker-compose.yml`**

```yaml
  cloudflared:
    image: cloudflare/cloudflared:2026.8.1
    restart: unless-stopped
    command: tunnel --no-autoupdate --config /etc/cloudflared/config.yml run
    volumes:
      - ./cloudflared/config.yml:/etc/cloudflared/config.yml:ro
      - ${CLOUDFLARED_CREDENTIALS_FILE:?Set CLOUDFLARED_CREDENTIALS_FILE in .env}:/etc/cloudflared/credentials.json:ro
    depends_on:
      - mcp-gateway
```

Pinning the image tag matters for the same reason the Python dependencies are pinned — `latest` turns an unrelated `docker compose up` into an unreviewed upgrade of the component holding the public ingress.

- [ ] **Step 3: Add the credentials path to `.env.example`**

```bash
# Path on the host to the tunnel credentials JSON from
# `cloudflared tunnel create`. Keep this outside the repository.
CLOUDFLARED_CREDENTIALS_FILE=/etc/cloudflared/openbrain-mcp.json
```

- [ ] **Step 4: Exclude credentials from git**

Append to `.gitignore`:

```
# Cloudflare tunnel credentials — never commit
cloudflared/*.json
```

- [ ] **Step 5: Verify the compose file renders**

```bash
CLOUDFLARED_CREDENTIALS_FILE=/tmp/fake.json docker compose config >/dev/null && echo "compose OK"
```

Expected: `compose OK`

- [ ] **Step 6: Confirm the ingress config parses**

```bash
docker run --rm -v "$PWD/cloudflared/config.yml:/etc/cloudflared/config.yml:ro" \
  cloudflare/cloudflared:2026.8.1 tunnel --config /etc/cloudflared/config.yml ingress validate
```

Expected: validation passes. It will report the tunnel id as unresolved until a real one is set — the rules themselves must validate.

- [ ] **Step 7: Confirm the ingress rules match as intended**

```bash
for p in /mcp /api/bulk-delete / /mcp/extra /.well-known/oauth-protected-resource; do
  echo -n "$p -> "
  docker run --rm -v "$PWD/cloudflared/config.yml:/etc/cloudflared/config.yml:ro" \
    cloudflare/cloudflared:2026.8.1 tunnel --config /etc/cloudflared/config.yml \
    ingress rule "https://openbrain-mcp.streckercm.com$p" 2>&1 | tail -1
done
```

Expected: `/mcp` and the metadata path resolve to `http://mcp-gateway:3001`; `/api/bulk-delete`, `/`, and `/mcp/extra` resolve to `http_status:404`.

- [ ] **Step 8: Commit**

```bash
git add cloudflared/config.yml docker-compose.yml .env.example .gitignore
git commit -m "feat: add cloudflared sidecar for the public MCP endpoint

The tunnel is an outbound connection, so no listening port exists on the
public interface to scan or forward. Ingress is restricted to /mcp and
the protected resource metadata paths, but the primary control is that
cloudflared can only reach port 3001 — the write API on 3002 is not
routable from this container.

Config lives in the repo rather than the Cloudflare dashboard so the
public surface is reviewable in version control. Image tag is pinned so
an unrelated 'compose up' cannot silently upgrade the ingress."
```

---

## Task 10: Document remote access

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: everything above
- Produces: setup documentation

- [ ] **Step 1: Add a remote access section to `README.md`**

Add before the final section:

```markdown
## Remote access

The MCP endpoint at `https://openbrain-mcp.streckercm.com/mcp` is reachable from the
public internet and requires an OAuth 2.1 bearer token. Everything else — the web UI,
PostgREST, Adminer, and the write REST API — is reachable only from the LAN and tailnet.

Authentication is enforced inside the gateway, not at the edge. A request arriving over
the Cloudflare tunnel, the tailnet, or the LAN faces the same check. This is why the
hostname can safely appear in local DNS.

### Authentik setup

Create an OAuth2/OpenID provider named `OpenBrain MCP`:

| Setting | Value |
|---|---|
| Client type | Confidential |
| Redirect URIs | The callback URLs claude.ai and ChatGPT present during connector setup |
| Signing key | An RS256 certificate |
| Scopes | `openid`, `profile`, `email`, `openbrain:read`, `openbrain:write` |
| Subject mode | Based on user ID |

Create an application with slug `openbrain-mcp`, and bind a policy restricting it to your
own account or a dedicated group — without one, every Authentik user can mint a working
token.

Confirm that issued tokens carry `https://openbrain-mcp.streckercm.com/mcp` in `aud`.
If they do not, add a scope mapping that sets it; the gateway rejects tokens whose
audience is not this server.

### Browser clients

In claude.ai or ChatGPT, add a custom connector pointing at
`https://openbrain-mcp.streckercm.com/mcp` and enter the Client ID and Secret from
Authentik under Advanced settings. Dynamic client registration is not used.

### Headless agents

Agents with no browser use a static token instead. Add it to `MCP_STATIC_TOKENS` in
`.env`, then configure the client:

```json
{
  "mcpServers": {
    "openbrain": {
      "type": "http",
      "url": "https://openbrain-mcp.streckercm.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN_HERE" }
    }
  }
}
```

Static tokens carry both scopes and never expire. Rotate them by editing
`MCP_STATIC_TOKENS` and restarting the gateway.

### Running without authentication

On a fully private deployment, set `MCP_AUTH_ENABLED=false`. The gateway refuses to start
if auth is enabled and the issuer, JWKS URL, or resource URI is unset, rather than
falling back to serving unauthenticated.
```

- [ ] **Step 2: Verify the links and commands**

Read the section back and confirm every environment variable named matches `.env.example`
and every path matches `docker-compose.yml`.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document remote MCP access setup"
```

---

## Deployment Runbook

Not code tasks — these are operational steps requiring the live environment. Run in
order, after Tasks 1–10 are merged and the local suite passes.

- [ ] **Enroll CT 115 and CT 127 on the tailnet.** Record CT 115's tailnet address.
- [ ] **Set `PRIVATE_BIND`** to CT 115's tailnet address in `/docker/openbrain/.env`.
- [ ] **Repoint the NPMplus vhosts** on CT 127 at the CT 115 tailnet address. Verify the
      web UI and docs load from a tailnet client.
- [ ] **Check the CT 115 working tree is clean** before pulling. It should be as of
      `243c606`. Local modifications to `docker-compose.yml` mean something re-diverged —
      investigate rather than stashing, because this deploy changes that file.
- [ ] **Configure the Authentik provider and application** per the README.
- [ ] **Generate a static token** and add it to `MCP_STATIC_TOKENS`, then update
      `.mcp.json` on each machine, *before* restarting the stack. Auth defaults to on, so
      unconfigured clients break at cutover.
- [ ] **Add the remaining variables** to `/docker/openbrain/.env`.
- [ ] **Deploy and verify** the gateway starts and existing Claude Code clients work.
- [ ] **Create the Cloudflare tunnel**, place credentials at
      `CLOUDFLARED_CREDENTIALS_FILE`, set the real tunnel id in `cloudflared/config.yml`,
      and start the sidecar.
- [ ] **Spec §9 test 18** — decode a real Authentik token and confirm `aud`. This gates
      trusting the audience check at all.
- [ ] **Spec §9 tests 19–21** — unauthenticated `POST /mcp` returns `401` from a phone on
      cellular, from inside the LAN, *and* from the tailnet. Test 19 passing while 20
      fails is the exact shape of the earlier Cloudflare Access bug; all three must fail
      closed.
- [ ] **Spec §9 test 22** — a valid token works over both the Cloudflare and NPMplus
      paths, returning identical tool lists.
- [ ] **Spec §9 tests 23–24** — `/api/bulk-delete` over the public hostname returns `404`;
      the web UI hostname is unreachable from off-tailnet.
- [ ] **Spec §9 test 25** — a long-running streaming MCP response over the Cloudflare path
      completes without truncation.
- [ ] **Enable Cloudflare rate limiting** scoped to `/mcp`, with a threshold well above
      what one agent produces in a burst.
- [ ] **Enable the WAF in log-only mode.** Do not set anything to block yet.
- [ ] **Spec §9 tests 27–29** — configure the claude.ai connector, the ChatGPT connector,
      and Claude Code with a static token.
- [ ] **After a week (spec §9 test 26)** — review WAF matches and enable blocking only for
      rules that produced no false positives on knowledge-base payloads.

---

## Follow-on Work: extract `db.py` and collapse MCP/REST duplication

**Not part of this plan.** Recorded here because this plan introduces the first pytest
harness `mcp-gateway` has ever had, which is the prerequisite that makes the work below
safe. Do not fold it into the auth tasks — the auth change touches ~30 lines of
`server.py` plus one new file, and mixing a 2,000-line refactor into a security change
makes the security change unreviewable.

### Why

`server.py` is 2,100 lines in five clean bands:

| Lines | Band | Size |
|---|---|---|
| 1–176 | Bootstrap, config, `AppContext`, schema apply | 176 |
| 177–724 | `_db_*` data layer, 14 functions | 548 |
| 725–1528 | 19 `@mcp.tool()` definitions | 804 |
| 1529–1959 | 18 REST handlers | 431 |
| 1960–2100 | Composition and lifespan | 141 |

Size is not the problem. The problem is that the two front doors do not share a spine.
Raw SQL call sites per band: data layer **55**, MCP tools **32**, REST handlers **6**.
REST delegates to `_db_*`; the MCP tools mostly reimplement it inline. All 14 `_db_*`
functions are called by REST, but only 4 by any MCP tool.

The data layer is already nearly decoupled — every `_db_*` takes `pool` as a parameter
and references only two module globals (`ORPHAN_POLICY`, `get_embedding`), so extraction
is close to a pure move.

### Divergence audit, 2026-08-22

Every MCP tool was compared against its REST counterpart.

**Confirmed defect — `add_project` on a duplicate name.** `projects.name` is the only
`UNIQUE` column in the schema (`init.sql:22`). `rest_projects_create` (`server.py:1681`)
catches `asyncpg.UniqueViolationError` and returns `409 Project 'X' already exists`. The
MCP tool `add_project` (`server.py:933`) does not catch it, so an agent registering a
project that already exists gets an unhandled database exception instead of the clean
`{"error": ...}` every other path in that tool returns — and it generates Sentry noise.
Fix: catch `UniqueViolationError` in `add_project`, or move the insert behind a
`_db_add_project` that both callers share.

**Dead defensive code.** `rest_knowledge_create` (`server.py:1581`) and
`rest_memories_create` (`server.py:1630`) also catch `UniqueViolationError`, but neither
`knowledge` nor `memories` has a unique constraint — confirmed against `init.sql` and
`migrate.sql`. Those handlers cannot fire. They are the copy-paste fingerprint of the
same problem, pointing the other way.

**Pure duplication, no behavioral difference found:**

| Operation | MCP | REST | Notes |
|---|---|---|---|
| archive knowledge / memory | inline, `server.py:1238`, `:1275` | `_db_archive`, `:367` | Same UPDATE plus the same `project_links` cascade |
| unarchive knowledge / memory | inline, `:1336`, `:1367` | `_db_unarchive`, `:396` | Both correctly skip the link cascade; the asymmetry is intentional and documented |
| unarchive project | inline, `:1398` | inline, `:1763` | Neither uses a helper — two inline copies of identical SQL |
| link to project | inline, `:1424` | `_db_link`, `:414` | Byte-identical SQL; differ only in error representation |
| unlink from project | inline, `:1485` | `_db_unlink`, `:464` | Byte-identical SQL |
| create project | inline, `:933` | inline, `:1660` | Identical INSERT; see the defect above |
| update project | inline, `:1021` | inline, `:1685` | Byte-identical dynamic UPDATE builder, ~30 lines each |
| search | `search_knowledge` `:773`, `recall_memory` `:1107` | `_db_search`, `:497` | Parallel implementations of vector-then-`ILIKE`-fallback, ~130 lines total |

**Capability differences that are probably intentional, not defects.** The MCP search
tools accept `project`, `category`, `memory_type`, `include_archived`, and a `limit`
defaulting to 10. `_db_search` hardcodes `LIMIT 20` per type and `status = 'active'`, and
adds a `mode="exact"` switch the MCP tools lack. Decide deliberately whether these should
converge when consolidating rather than picking one side by accident.

Search is where a future divergence would hurt most. A drift in ranking or fallback there
does not raise an error — it quietly returns worse results on one of the two paths, which
is the kind of regression nobody notices.

### Adjacent finding, not a divergence

`_db_save_memory` (`server.py:264`) is a plain `INSERT` with no upsert, and `memories.name`
has no unique constraint. Calling `save_memory` twice with the same name creates two rows
rather than updating one. Both front doors behave identically, so it is not a divergence —
but duplicate memories dilute search results, which feeds the recall risk in the spec.
Worth a decision: upsert on `(name, project)`, or leave duplicates and dedupe at read time.

### To do

- [x] **A. Fix the `add_project` duplicate-name defect.** DONE 2026-08-22 on branch
  `fix/add-project-duplicate-name`. Fully independent of everything else. `server.py:933`, after
  `app = _get_app_ctx(ctx)`:

  ```python
      try:
          row = await app.pool.fetchrow(
              """INSERT INTO projects (name, description, repo_url, tech_stack, notes, orphan_policy)
                 VALUES ($1, $2, $3, $4, $5, $6)
                 RETURNING id, name, status, orphan_policy, created_at""",
              name, description, repo_url, tech_stack or [], notes, orphan_policy,
          )
      except asyncpg.UniqueViolationError:
          return json.dumps({"error": f"Project '{name}' already exists"})
      return _format_rows([row])
  ```

  The message deliberately matches `rest_projects_create`'s 409 text, so the two front
  doors say the same thing. Regression test: call `add_project` twice with the same name
  and assert the second returns `{"error": "Project 'X' already exists"}` rather than
  raising. Verify a Sentry event is *not* produced for the second call.

- [ ] **B. Extract `db.py`.** Pure move of `server.py:177-724` — the 14 `_db_*` functions
  plus the two globals they touch (`ORPHAN_POLICY`, `get_embedding`). No logic changes in
  this step; verify by imports resolving and existing behavior being untouched. This is
  the step that makes reuse the path of least resistance, which is the actual fix for the
  duplication problem.

- [ ] **C. Migrate the MCP tools' 32 raw SQL sites onto `db.py`.** One group at a time,
  cheapest and safest first. Write characterization tests against current behavior
  *before* each move, so the tests describe what the code does today rather than what the
  refactor makes it do.

  - [ ] link / unlink — byte-identical to `_db_link` / `_db_unlink`, lowest risk
  - [ ] archive / unarchive (knowledge, memory) — identical to `_db_archive` / `_db_unarchive`
  - [ ] project create / update / unarchive — needs a new `_db_add_project`,
        `_db_update_project`, `_db_unarchive_project`; folds in fix A properly
  - [ ] search — highest risk and highest value. Decide deliberately whether the limit
        (10 vs 20), `include_archived`, `mode="exact"`, and the `project` / `category` /
        `memory_type` filters converge, rather than picking one side by accident.

- [ ] **D. Split `tools.py` and `rest.py`.** Optional. By this point `server.py` is roughly
  500 lines and the split is cosmetic.

  Circular-import gotcha: `tools.py` needs the `mcp` FastMCP instance to decorate
  against, and `server.py` needs `tools` imported for registration to happen. Define
  `mcp` in a small `core.py` that both import, and have `server.py` import `tools` for
  the side effect.

- [ ] **E. Decide on `save_memory` duplicates** (the adjacent finding above): upsert on
  `(name, project)`, or keep duplicates and dedupe at read time.

### Root cause worth remembering

This duplication is the expected failure mode of subagent-driven development: an agent
given one task writes the code that task needs and does not reliably discover that
`_db_archive` already exists three hundred lines up. The mitigation is not more careful
prompting — it is having the shared layer be a separate importable module with an obvious
name, so reuse is the path of least resistance rather than a discovery problem. That is
the strongest argument for step 1, ahead of any file-size concern.
