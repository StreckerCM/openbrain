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
