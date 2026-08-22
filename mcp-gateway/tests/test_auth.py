import json

import jwt
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


def test_from_env_default_reads_os_environ_and_raises_when_unset(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    for var in ("MCP_OAUTH_ISSUER", "MCP_OAUTH_JWKS_URL", "MCP_RESOURCE_URI"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(auth.AuthConfigError) as exc:
        auth.AuthConfig.from_env()
    message = str(exc.value)
    assert "MCP_OAUTH_ISSUER" in message
    assert "MCP_OAUTH_JWKS_URL" in message
    assert "MCP_RESOURCE_URI" in message


def test_from_env_default_reads_os_environ_when_set(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("MCP_OAUTH_ISSUER", BASE_ENV["MCP_OAUTH_ISSUER"])
    monkeypatch.setenv("MCP_OAUTH_JWKS_URL", BASE_ENV["MCP_OAUTH_JWKS_URL"])
    monkeypatch.setenv("MCP_RESOURCE_URI", BASE_ENV["MCP_RESOURCE_URI"])
    monkeypatch.delenv("MCP_STATIC_TOKENS", raising=False)
    monkeypatch.delenv("MCP_REQUIRED_SCOPES", raising=False)

    cfg = auth.AuthConfig.from_env()

    assert cfg.enabled is True
    assert cfg.issuer == BASE_ENV["MCP_OAUTH_ISSUER"]
    assert cfg.jwks_url == BASE_ENV["MCP_OAUTH_JWKS_URL"]
    assert cfg.resource_uri == BASE_ENV["MCP_RESOURCE_URI"]


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
    token = jwt.encode(
        {"sub": "x", "aud": RESOURCE, "iss": ISSUER}, key=None, algorithm="none"
    )
    with pytest.raises(auth.Unauthorized):
        await auth.validate_jwt(token, jwt_config, jwks)


def test_extract_scopes_from_space_delimited_string():
    assert auth.extract_scopes({"scope": "a b"}) == frozenset({"a", "b"})


def test_extract_scopes_from_scp_list():
    assert auth.extract_scopes({"scp": ["a", "b"]}) == frozenset({"a", "b"})


def test_extract_scopes_when_absent():
    assert auth.extract_scopes({}) == frozenset()
