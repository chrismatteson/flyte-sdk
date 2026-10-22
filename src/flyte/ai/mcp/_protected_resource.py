"""OAuth 2.0 Protected Resource Metadata ([RFC 9728](https://www.rfc-editor.org/rfc/rfc9728)) for MCP servers.

MCP clients that implement the MCP authorization spec find out how to sign in from two things
the server provides: a `401` whose `WWW-Authenticate: Bearer resource_metadata="..."` header
points at a metadata document, and that document, which names the authorization server(s) that
issue tokens for this server. This module holds the user-facing configuration and the two
pieces of HTTP plumbing; `FlyteMCPAppEnvironment` wires them in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

#: RFC 9728 §3: the well-known URI suffix for protected resource metadata.
WELL_KNOWN_PATH = "/.well-known/oauth-protected-resource"


def _require_http_url(field_name: str, url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute http(s) URL, got {url!r}.")


@dataclass(frozen=True, kw_only=True)
class ProtectedResourceMetadata:
    """What an MCP server advertises about its own authorization, per RFC 9728.

    Pass one to `FlyteMCPAppEnvironment.protected_resource_metadata` to let MCP clients that
    implement the [MCP authorization spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
    discover how to obtain a token instead of needing one pasted into their configuration.

    Args:
        authorization_servers: Issuer URLs of the OAuth 2.0 authorization servers whose tokens
            this server accepts. `None` (the default) advertises the issuer that the Flyte
            endpoint the server is connected to advertises itself, fetched once when the
            metadata is first requested; the server forwards each caller's token to that
            endpoint, so that issuer is normally the right one.
        resource: The resource identifier, i.e. the full URL clients use to reach the MCP
            endpoint (for example `https://mcp.example.com/flyte-mcp/mcp`). Clients reject
            metadata whose `resource` does not match the URL they connected to. `None` (the
            default) derives it from each request's scheme and host plus the MCP endpoint
            path; set it when a proxy in front of the server rewrites the host, scheme or path.
        scopes_supported: OAuth scopes a client should request. Also sent as the `scope`
            parameter of the `401` challenge.
        resource_name: Human-readable name for the server, shown by some clients on their
            consent screen.
        resource_documentation: URL of documentation for developers using the server.

    Example:
    ```python
    from flyte.ai.mcp import FlyteMCPAppEnvironment, ProtectedResourceMetadata

    env = FlyteMCPAppEnvironment(
        name="flyte-mcp",
        requires_auth=False,
        protected_resource_metadata=ProtectedResourceMetadata(
            authorization_servers=["https://auth.example.com/oauth2/default"],
        ),
    )
    ```
    """

    authorization_servers: list[str] | None = None
    resource: str | None = None
    scopes_supported: list[str] | None = None
    resource_name: str | None = None
    resource_documentation: str | None = None

    def __post_init__(self):
        if self.authorization_servers is not None:
            if not self.authorization_servers:
                # RFC 9728 makes the list optional, but MCP requires at least one entry, and an
                # empty list is far more likely a bug than a request for discovery.
                raise ValueError(
                    "authorization_servers must not be empty; pass None to advertise the issuer "
                    "the Flyte endpoint advertises."
                )
            for url in self.authorization_servers:
                _require_http_url("authorization_servers", url)
        if self.resource is not None:
            _require_http_url("resource", self.resource)
        if self.resource_documentation is not None:
            _require_http_url("resource_documentation", self.resource_documentation)


def metadata_path(resource_path: str) -> str:
    """Return the path at which RFC 9728 §3.1 places the metadata for a resource with this path.

    The well-known suffix goes between the host and the resource's path, so the metadata for
    `https://host/flyte-mcp/mcp` lives at `https://host/.well-known/oauth-protected-resource/flyte-mcp/mcp`.
    This is the rule `mcp.server.auth.routes.build_resource_metadata_url` implements; it is
    restated here on a bare path because when `resource` is derived per request the host is
    not known when the route is registered, and the route and the `WWW-Authenticate` URL must
    come from the same function so they cannot disagree.
    """
    if resource_path in ("", "/"):
        return WELL_KNOWN_PATH
    return WELL_KNOWN_PATH + resource_path


class BearerChallengeMiddleware:
    """Answer requests that carry no bearer token with an RFC 9728 `401` challenge.

    `mcp.server.auth.middleware.bearer_auth.RequireAuthMiddleware` sends the same header, but
    only after a `TokenVerifier` has validated the token in-process. This server does not
    validate tokens itself — it forwards them to the Flyte endpoint on every remote call — so
    using it would mean stacking `AuthenticationMiddleware` and a verifier that accepts
    anything, just to test whether the header is present. This does that test directly.

    Args:
        app: The ASGI app to protect.
        resource_url: Returns the resource identifier for a request.
        excluded_paths: Paths served without a token (health check, the metadata itself).
        scopes: Sent as the challenge's `scope` parameter when given.
    """

    def __init__(
        self,
        app: ASGIApp,
        resource_url: Callable[[Request], str],
        excluded_paths: set[str],
        scopes: list[str] | None = None,
    ):
        self.app = app
        self.resource_url = resource_url
        self.excluded_paths = excluded_paths
        self.scopes = scopes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        if scope["type"] != "http" or scope["path"] in self.excluded_paths:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        auth_scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if auth_scheme.lower() == "bearer" and token.strip():
            await self.app(scope, receive, send)
            return

        resource = urlparse(self.resource_url(request))
        metadata_url = f"{resource.scheme}://{resource.netloc}{metadata_path(resource.path)}"
        # No `error` parameter: RFC 6750 §3.1 says to omit it when the request carried no
        # credentials at all, as opposed to carrying a bad one.
        challenge = f'Bearer resource_metadata="{metadata_url}"'
        if self.scopes:
            challenge += f', scope="{" ".join(self.scopes)}"'
        response = JSONResponse(
            status_code=401,
            # Same body as `FastAPIPassthroughAuthMiddleware`'s 401, which this replaces.
            content={"detail": "Authentication credentials required"},
            headers={"WWW-Authenticate": challenge},
        )
        await response(scope, receive, send)


async def discover_authorization_server() -> str:
    """Return the OAuth issuer advertised by the Flyte endpoint the SDK is initialized against.

    This is the same `AuthMetadataService` lookup the SDK's own authenticators use to find
    where to sign in. The service is unauthenticated, so the call carries no credentials.

    Raises:
        RuntimeError: if the SDK is not initialized or the endpoint advertises no issuer.
    """
    from flyteidl2.auth.auth_service_connect import AuthMetadataServiceClient
    from flyteidl2.auth.auth_service_pb2 import GetOAuth2MetadataRequest

    from flyte._initialize import _get_init_config

    cfg = _get_init_config()
    if cfg is None or cfg.client is None:
        raise RuntimeError(
            "Flyte is not initialized, so its authorization server cannot be discovered. "
            "Set ProtectedResourceMetadata.authorization_servers explicitly."
        )
    session = cfg.client.session_config
    # The session's http client, but not its interceptors: those add the caller's credentials,
    # and in passthrough mode there is no caller here.
    client = AuthMetadataServiceClient(address=session.endpoint, http_client=session.http_client)
    metadata = await client.get_o_auth2_metadata(GetOAuth2MetadataRequest())
    if not metadata.issuer:
        raise RuntimeError(
            f"The Flyte endpoint {session.endpoint} advertises no OAuth issuer. "
            "Set ProtectedResourceMetadata.authorization_servers explicitly."
        )
    return metadata.issuer
