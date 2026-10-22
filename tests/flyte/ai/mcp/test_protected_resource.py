"""RFC 9728 protected resource metadata on `FlyteMCPAppEnvironment`."""

from types import SimpleNamespace

import pytest

import flyte
from flyte.ai.mcp import FlyteMCPAppEnvironment, ProtectedResourceMetadata, _flyte_mcp_app

AUTH_SERVER = "https://auth.example.com"
SUFFIXED = "/.well-known/oauth-protected-resource/flyte-mcp/mcp"
ROOT = "/.well-known/oauth-protected-resource"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _env(**prm_kwargs) -> FlyteMCPAppEnvironment:
    prm_kwargs.setdefault("authorization_servers", [AUTH_SERVER])
    return FlyteMCPAppEnvironment(
        name="test-mcp",
        requires_auth=False,
        protected_resource_metadata=ProtectedResourceMetadata(**prm_kwargs),
    )


def _client(env: FlyteMCPAppEnvironment, **kwargs):
    from starlette.testclient import TestClient

    return TestClient(env._starlette_app, raise_server_exceptions=False, **kwargs)


@pytest.fixture
def fake_init_passthrough(monkeypatch):
    calls = []

    class FakeInitPassthrough:
        async def aio(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(flyte, "init_passthrough", FakeInitPassthrough())
    return calls


class TestConfig:
    def test_exported_from_package(self):
        import flyte.ai.mcp

        assert "ProtectedResourceMetadata" in flyte.ai.mcp.__all__

    def test_off_by_default(self):
        assert FlyteMCPAppEnvironment(name="test-mcp").protected_resource_metadata is None

    def test_empty_authorization_servers_rejected(self):
        with pytest.raises(ValueError, match="authorization_servers must not be empty"):
            ProtectedResourceMetadata(authorization_servers=[])

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"authorization_servers": ["auth.example.com"]},
            {"resource": "/flyte-mcp/mcp"},
            {"resource_documentation": "ftp://docs.example.com"},
        ],
    )
    def test_non_http_urls_rejected(self, kwargs):
        with pytest.raises(ValueError, match="absolute http"):
            ProtectedResourceMetadata(**kwargs)

    def test_stdio_rejected(self):
        with pytest.raises(ValueError, match="requires an HTTP transport"):
            FlyteMCPAppEnvironment(
                name="test-mcp",
                transport="stdio",
                protected_resource_metadata=ProtectedResourceMetadata(authorization_servers=[AUTH_SERVER]),
            )

    def test_rich_repr_includes_metadata(self):
        env = _env()
        assert dict(env.__rich_repr__())["protected_resource_metadata"] == env.protected_resource_metadata


class TestDisabled:
    """Without the config, nothing about the HTTP surface changes."""

    def test_requires_auth_keeps_plain_bearer_challenge(self):
        env = FlyteMCPAppEnvironment(name="test-mcp")
        client = _client(env)
        response = client.post("/flyte-mcp/mcp", json={})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        # The metadata path is just another authenticated path.
        assert client.get(ROOT).status_code == 401

    def test_requires_auth_installs_only_passthrough_middleware(self):
        from flyte.app.extras import FastAPIPassthroughAuthMiddleware

        env = FlyteMCPAppEnvironment(name="test-mcp")
        middleware = env._starlette_middleware()
        assert [m.cls for m in middleware] == [FastAPIPassthroughAuthMiddleware]
        assert middleware[0].kwargs["excluded_paths"] == {"/health"}

    def test_no_auth_serves_no_metadata_and_installs_no_middleware(self):
        env = FlyteMCPAppEnvironment(name="test-mcp", requires_auth=False)
        assert env._starlette_middleware() == []
        client = _client(env)
        assert client.get(SUFFIXED).status_code == 404
        assert client.get(ROOT).status_code == 404

    @pytest.mark.asyncio
    async def test_no_auth_skips_passthrough_init(self, fake_init_passthrough):
        await FlyteMCPAppEnvironment(name="test-mcp", requires_auth=False)._starlette_lifespan_startup()
        assert fake_init_passthrough == []


class TestMetadataDocument:
    @pytest.mark.parametrize("path", [SUFFIXED, ROOT])
    def test_served_without_auth(self, path):
        response = _client(_env()).get(path)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {
            "resource": "http://testserver/flyte-mcp/mcp",
            "authorization_servers": [AUTH_SERVER],
            "bearer_methods_supported": ["header"],
        }

    def test_optional_fields(self):
        env = _env(
            scopes_supported=["all"],
            resource_name="Flyte MCP",
            resource_documentation="https://docs.example.com/mcp",
        )
        body = _client(env).get(SUFFIXED).json()
        assert body["scopes_supported"] == ["all"]
        assert body["resource_name"] == "Flyte MCP"
        assert body["resource_documentation"] == "https://docs.example.com/mcp"

    def test_urls_are_advertised_verbatim(self):
        # A bare-host issuer must not gain a trailing slash: clients compare it with the
        # authorization server's own `issuer` as a string.
        env = _env(resource="https://mcp.example.com")
        body = _client(env).get(ROOT).json()
        assert body["authorization_servers"] == ["https://auth.example.com"]
        assert body["resource"] == "https://mcp.example.com"

    def test_cors_preflight_allowed(self):
        response = _client(_env()).options(
            SUFFIXED,
            headers={"Origin": "https://inspector.example.com", "Access-Control-Request-Method": "GET"},
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"

    def test_excluded_from_passthrough_middleware(self):
        from flyte.app.extras import FastAPIPassthroughAuthMiddleware

        middleware = _env()._starlette_middleware()
        passthrough = next(m for m in middleware if m.cls is FastAPIPassthroughAuthMiddleware)
        assert passthrough.kwargs["excluded_paths"] == {"/health", SUFFIXED, ROOT}

    def test_health_still_unauthenticated(self):
        assert _client(_env()).get("/health").status_code == 200


class TestResourceAndPaths:
    def test_resource_derived_from_request_host(self):
        client = _client(_env(), base_url="https://mcp.example.com")
        assert client.get(SUFFIXED).json()["resource"] == "https://mcp.example.com/flyte-mcp/mcp"

    def test_custom_mount_path(self):
        env = FlyteMCPAppEnvironment(
            name="test-mcp",
            requires_auth=False,
            mcp_mount_path="/tools",
            protected_resource_metadata=ProtectedResourceMetadata(authorization_servers=[AUTH_SERVER]),
        )
        response = _client(env).get("/.well-known/oauth-protected-resource/tools/mcp")
        assert response.status_code == 200
        assert response.json()["resource"] == "http://testserver/tools/mcp"

    def test_sse_transport_uses_sse_endpoint(self):
        env = FlyteMCPAppEnvironment(
            name="test-mcp",
            requires_auth=False,
            transport="sse",
            protected_resource_metadata=ProtectedResourceMetadata(authorization_servers=[AUTH_SERVER]),
        )
        response = _client(env).get("/.well-known/oauth-protected-resource/flyte-mcp/sse")
        assert response.status_code == 200
        assert response.json()["resource"] == "http://testserver/flyte-mcp/sse"

    def test_resource_override_is_used_and_moves_suffixed_path(self):
        # A proxy that serves the app under a prefix: the RFC places the metadata after the
        # host and before the resource's own path.
        env = _env(resource="https://public.example.com/team/flyte-mcp/mcp")
        client = _client(env)
        response = client.get("/.well-known/oauth-protected-resource/team/flyte-mcp/mcp")
        assert response.status_code == 200
        assert response.json()["resource"] == "https://public.example.com/team/flyte-mcp/mcp"
        assert client.get(ROOT).json()["resource"] == "https://public.example.com/team/flyte-mcp/mcp"

        challenge = client.post("/flyte-mcp/mcp", json={}).headers["www-authenticate"]
        assert challenge == (
            'Bearer resource_metadata="https://public.example.com'
            '/.well-known/oauth-protected-resource/team/flyte-mcp/mcp"'
        )


class TestChallenge:
    def test_missing_token_gets_rfc9728_challenge(self):
        response = _client(_env()).post("/flyte-mcp/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == (
            'Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource/flyte-mcp/mcp"'
        )
        assert response.json() == {"detail": "Authentication credentials required"}

    def test_challenge_url_follows_request_host(self):
        client = _client(_env(), base_url="https://mcp.example.com")
        challenge = client.post("/flyte-mcp/mcp", json={}).headers["www-authenticate"]
        assert challenge == f'Bearer resource_metadata="https://mcp.example.com{SUFFIXED}"'

    def test_challenge_carries_scopes(self):
        challenge = _client(_env(scopes_supported=["read", "write"])).post("/flyte-mcp/mcp", json={})
        assert challenge.headers["www-authenticate"].endswith(', scope="read write"')

    @pytest.mark.parametrize(
        "headers",
        [
            {"Authorization": "Basic dXNlcjpwYXNz"},
            {"Authorization": "Bearer "},
            {"Cookie": "session=abc"},
        ],
    )
    def test_anything_but_a_bearer_token_is_challenged(self, headers):
        response = _client(_env()).post("/flyte-mcp/mcp", json={}, headers=headers)
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]

    def test_bearer_token_reaches_mcp_server(self, fake_init_passthrough):
        with _client(_env()) as client:
            response = client.post(
                "/flyte-mcp/mcp",
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Authorization": "Bearer abc"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["serverInfo"]["name"] == "test-mcp"

    def test_bearer_token_is_forwarded_to_flyte(self, monkeypatch):
        # The passthrough middleware still runs behind the challenge, so tool calls see the token.
        from flyte.app.extras import FastAPIPassthroughAuthMiddleware

        seen = []
        original = FastAPIPassthroughAuthMiddleware.extract_authorization_header

        def spy(request):
            result = original(request)
            seen.append(result)
            return result

        monkeypatch.setattr(FastAPIPassthroughAuthMiddleware, "extract_authorization_header", staticmethod(spy))
        _client(_env()).post("/flyte-mcp/mcp", json={}, headers={"Authorization": "Bearer abc"})
        assert seen == [("authorization", "Bearer abc")]


class TestPassthroughWithoutIngressAuth:
    """`requires_auth=False` with metadata still acts as the caller, never as the app."""

    def test_passthrough_middleware_installed(self):
        from flyte.ai.mcp._protected_resource import BearerChallengeMiddleware
        from flyte.app.extras import FastAPIPassthroughAuthMiddleware

        classes = [m.cls for m in _env()._starlette_middleware()]
        assert classes == [BearerChallengeMiddleware, FastAPIPassthroughAuthMiddleware]

    @pytest.mark.asyncio
    async def test_lifespan_initializes_passthrough(self, fake_init_passthrough):
        await _env()._starlette_lifespan_startup()
        assert len(fake_init_passthrough) == 1


class TestAuthorizationServerDiscovery:
    def test_discovered_issuer_is_advertised_and_cached(self, monkeypatch):
        calls = []

        async def fake_discover():
            calls.append(1)
            return "https://issuer.example.com/oauth2/default"

        monkeypatch.setattr(_flyte_mcp_app, "discover_authorization_server", fake_discover)
        client = _client(_env(authorization_servers=None))
        for _ in range(2):
            body = client.get(SUFFIXED).json()
            assert body["authorization_servers"] == ["https://issuer.example.com/oauth2/default"]
        assert len(calls) == 1

    def test_failed_discovery_returns_503_and_retries(self, monkeypatch):
        outcomes = [RuntimeError("control plane down"), "https://issuer.example.com"]

        async def flaky_discover():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(_flyte_mcp_app, "discover_authorization_server", flaky_discover)
        client = _client(_env(authorization_servers=None))
        failed = client.get(SUFFIXED)
        assert failed.status_code == 503
        assert "cache-control" not in failed.headers
        assert client.get(SUFFIXED).json()["authorization_servers"] == ["https://issuer.example.com"]

    @pytest.mark.asyncio
    async def test_discover_reads_issuer_from_auth_metadata_service(self, monkeypatch):
        import flyteidl2.auth.auth_service_connect as auth_connect

        import flyte._initialize
        from flyte.ai.mcp._protected_resource import discover_authorization_server

        constructed = {}

        class FakeAuthMetadataServiceClient:
            def __init__(self, address, http_client):
                constructed.update(address=address, http_client=http_client)

            async def get_o_auth2_metadata(self, request):
                return SimpleNamespace(issuer="https://issuer.example.com")

        http_client = object()
        session = SimpleNamespace(endpoint="https://flyte.example.com", http_client=http_client)
        cfg = SimpleNamespace(client=SimpleNamespace(session_config=session))
        monkeypatch.setattr(flyte._initialize, "_get_init_config", lambda: cfg)
        monkeypatch.setattr(auth_connect, "AuthMetadataServiceClient", FakeAuthMetadataServiceClient)

        assert await discover_authorization_server() == "https://issuer.example.com"
        # The session's http client, without the passthrough interceptors.
        assert constructed == {"address": "https://flyte.example.com", "http_client": http_client}

    @pytest.mark.asyncio
    async def test_discover_requires_initialized_sdk(self, monkeypatch):
        import flyte._initialize
        from flyte.ai.mcp._protected_resource import discover_authorization_server

        monkeypatch.setattr(flyte._initialize, "_get_init_config", lambda: None)
        with pytest.raises(RuntimeError, match="not initialized"):
            await discover_authorization_server()

    @pytest.mark.asyncio
    async def test_discover_rejects_missing_issuer(self, monkeypatch):
        import flyteidl2.auth.auth_service_connect as auth_connect

        import flyte._initialize
        from flyte.ai.mcp._protected_resource import discover_authorization_server

        class FakeAuthMetadataServiceClient:
            def __init__(self, address, http_client):
                pass

            async def get_o_auth2_metadata(self, request):
                return SimpleNamespace(issuer="")

        session = SimpleNamespace(endpoint="https://flyte.example.com", http_client=None)
        cfg = SimpleNamespace(client=SimpleNamespace(session_config=session))
        monkeypatch.setattr(flyte._initialize, "_get_init_config", lambda: cfg)
        monkeypatch.setattr(auth_connect, "AuthMetadataServiceClient", FakeAuthMetadataServiceClient)

        with pytest.raises(RuntimeError, match="advertises no OAuth issuer"):
            await discover_authorization_server()
