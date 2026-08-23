"""Shared fixtures for mcp-gateway tests."""
import json
import os
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

# server.py loads AuthConfig.from_env() at module import time and fails
# closed if auth is enabled without issuer/JWKS URL/resource URI set.
# test_projects.py imports server directly, so collection would break
# outside a container that already has real values in its environment
# (e.g. via .env). setdefault leaves a real environment untouched and
# only fills the gap for a bare local pytest run.
os.environ.setdefault(
    "MCP_OAUTH_ISSUER", "https://auth.example.com/application/o/openbrain-mcp/"
)
os.environ.setdefault(
    "MCP_OAUTH_JWKS_URL", "https://auth.example.com/application/o/openbrain-mcp/jwks/"
)
os.environ.setdefault(
    "MCP_RESOURCE_URI", "https://openbrain-mcp.example.com/mcp"
)

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
