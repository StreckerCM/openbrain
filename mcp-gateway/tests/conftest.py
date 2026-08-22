"""Shared fixtures for mcp-gateway tests.

Populated with signing-key and JWKS fixtures in Task 3.
"""
import os

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
