"""Bearer-token gate for remote control-plane routers.

Enabled when ``OMICSCLAW_REMOTE_AUTH_TOKEN`` is set to a non-empty value.
When unset (or whitespace-only) loopback development and single-user SSH
tunnels remain unauthenticated.  As defense in depth, an explicitly reported
non-loopback ASGI socket is rejected unless a token was configured.

The token is validated constant-time to avoid timing side-channels.
"""

from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import MutableMapping
from urllib.parse import unquote_to_bytes

from fastapi import FastAPI, Header, HTTPException, Request, status
from starlette.responses import JSONResponse
from starlette.routing import get_route_path
from starlette.types import ASGIApp, Receive, Scope, Send

TOKEN_ENV = "OMICSCLAW_REMOTE_AUTH_TOKEN"
AUTHORITY_STATE_ATTR = "remote_bearer_authority"
AUTHORITY_UNAVAILABLE_DETAIL = "remote bearer authority is not initialized"
EXPECTED_BACKEND_PROCESS_EPOCH_HEADER = b"x-omicsclaw-expected-backend-process-epoch"
BACKEND_PROCESS_EPOCH_MISMATCH_DETAIL = "backend_process_epoch_mismatch"
_MAX_PATH_DECODE_ROUNDS = 8


class _PathAuthority(Enum):
    ORDINARY = "ordinary"
    DELEGATED = "delegated"
    INVALID_DELEGATED = "invalid_delegated"


@dataclass(frozen=True, slots=True)
class BearerGatePolicy:
    """One already-resolved bearer policy selected before ASGI dispatch."""

    token: str = field(repr=False)
    realm: str
    unconfigured_detail: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def enforce(self, authorization: str | None) -> None:
        if not self.token:
            if self.unconfigured_detail is not None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=self.unconfigured_detail,
                )
            return
        challenge = {"WWW-Authenticate": f'Bearer realm="{self.realm}"'}
        if authorization is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers=challenge,
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.strip(), self.token
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
                headers=challenge,
            )


def _enforce_bearer_token(authorization: str | None, expected: str) -> None:
    """Validate one bearer header against the remote HTTP authority."""

    BearerGatePolicy(token=expected, realm="omicsclaw-remote").enforce(authorization)


@dataclass(frozen=True, slots=True)
class RemoteBearerAuthority:
    """One process-lifetime remote HTTP authority captured at startup."""

    token: str = field(repr=False)

    @classmethod
    def capture(cls, environ: MutableMapping[str, str]) -> "RemoteBearerAuthority":
        return cls(token=str(environ.get(TOKEN_ENV, "") or "").strip())

    def enforce(self, authorization: str | None) -> None:
        _enforce_bearer_token(authorization, self.token)

    @property
    def configured(self) -> bool:
        return bool(self.token)


def capture_remote_bearer_authority(
    app: FastAPI,
    environ: MutableMapping[str, str],
) -> RemoteBearerAuthority:
    """Freeze remote bearer policy for one FastAPI lifespan."""

    authority = RemoteBearerAuthority.capture(environ)
    setattr(app.state, AUTHORITY_STATE_ATTR, authority)
    return authority


def release_remote_bearer_authority(
    app: FastAPI,
    authority: RemoteBearerAuthority,
) -> None:
    """Remove one authority only if this lifespan still owns the state slot."""

    if _authority_for_app(app) is authority:
        delattr(app.state, AUTHORITY_STATE_ATTR)


def _authority_for_app(app: object) -> RemoteBearerAuthority | None:
    state = getattr(app, "state", None)
    authority = getattr(state, AUTHORITY_STATE_ATTR, None)
    return authority if isinstance(authority, RemoteBearerAuthority) else None


def remote_bearer_authority_for_app(app: object) -> RemoteBearerAuthority:
    """Resolve only the authority captured for this process lifespan."""

    authority = _authority_for_app(app)
    if authority is not None:
        return authority
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=AUTHORITY_UNAVAILABLE_DETAIL,
    )


def _expected_token_for_scope(scope: Scope) -> str:
    return remote_bearer_authority_for_app(scope.get("app")).token


def _has_explicit_non_loopback_server(scope: Scope) -> bool:
    """Detect direct wildcard/external ASGI binds as defense in depth.

    The canonical launcher rejects these binds before startup. This request-time
    check also covers direct ``uvicorn module:app`` launches when the ASGI
    server reports its socket address. Missing/non-tuple server metadata is not
    guessed; the launcher remains authoritative for that case.
    """

    server = scope.get("server")
    if not isinstance(server, (tuple, list)) or not server:
        return False
    host = str(server[0] or "").strip()
    if host.casefold() in {"localhost", "testserver"}:
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return bool(host)


async def require_bearer_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    remote_bearer_authority_for_app(request.app).enforce(authorization)


class RemoteBearerMiddleware:
    """Authenticate every Desktop HTTP request before route/body handling.

    ``public_paths`` contains exact GET/HEAD liveness exceptions.
    ``delegated_path_prefixes`` contains paths that apply a stronger,
    independently owned credential policy. Delegated prefixes match either
    the exact path or a slash-delimited descendant, never a lookalike path.
    When configured, ``backend_process_epoch_resolver`` fences an authenticated
    HTTP request that carries an expected epoch before route or body handling.
    Omitting the header preserves compatibility with existing clients.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        public_paths: Sequence[str] = (),
        delegated_path_prefixes: Sequence[str] = (),
        delegated_policy_resolver: Callable[[Scope], BearerGatePolicy] | None = None,
        backend_process_epoch_resolver: Callable[[Scope], str] | None = None,
    ) -> None:
        self._app = app
        self._public_paths = frozenset(public_paths)
        self._delegated_path_prefixes = tuple(
            prefix.rstrip("/") or "/" for prefix in delegated_path_prefixes
        )
        if self._delegated_path_prefixes and delegated_policy_resolver is None:
            raise ValueError(
                "delegated paths require an explicit bearer policy resolver"
            )
        self._delegated_policy_resolver = delegated_policy_resolver
        self._backend_process_epoch_resolver = backend_process_epoch_resolver

    @staticmethod
    def _strict_percent_decode(value: str) -> str | None:
        """Decode one path layer only when escapes and UTF-8 are valid."""

        for index, character in enumerate(value):
            if character != "%":
                continue
            escape = value[index + 1 : index + 3]
            if len(escape) != 2 or any(
                digit not in "0123456789abcdefABCDEF" for digit in escape
            ):
                return None
        try:
            return unquote_to_bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _has_ambiguous_path_encoding(value: str) -> bool:
        """Reject path forms whose later decoding can change routing syntax."""

        current = value
        for _ in range(_MAX_PATH_DECODE_ROUNDS):
            if "\\" in current or any(
                segment in {".", ".."} for segment in current.split("/")
            ):
                return True
            if "%" not in current:
                return False
            decoded = RemoteBearerMiddleware._strict_percent_decode(current)
            if decoded is None:
                return True
            if decoded == current:
                return False
            if (
                decoded.count(".") > current.count(".")
                or decoded.count("/") > current.count("/")
                or decoded.count("\\") > current.count("\\")
            ):
                return True
            current = decoded
        # A still-changing representation after the bounded normalization
        # budget is ambiguous; it never receives a privileged authority.
        decoded = RemoteBearerMiddleware._strict_percent_decode(current)
        return decoded is None or decoded != current

    def _has_verified_raw_path(self, scope: Scope, path: str) -> bool:
        """Prove that the full raw request path represents ``scope.path``."""

        raw_path = scope.get("raw_path")
        if not isinstance(raw_path, bytes):
            return False
        try:
            raw_value = raw_path.decode("ascii")
        except UnicodeDecodeError:
            return False
        normalized_raw_path = self._strict_percent_decode(raw_value)
        return (
            normalized_raw_path is not None
            and normalized_raw_path == path
            and not self._has_ambiguous_path_encoding(raw_value)
            and not self._has_ambiguous_path_encoding(path)
        )

    def _path_authority(
        self,
        scope: Scope,
        *,
        path: str,
        route_path: str,
    ) -> _PathAuthority:
        route_is_delegated = any(
            route_path == prefix or route_path.startswith(f"{prefix}/")
            for prefix in self._delegated_path_prefixes
        )
        if not route_is_delegated:
            return _PathAuthority.ORDINARY

        # Servers expose a decoded ``path`` but retain the request target in
        # ``raw_path``. Check both so single- and multiply-encoded routing
        # syntax cannot select the stronger delegated authority.
        if not self._has_verified_raw_path(scope, path):
            return _PathAuthority.INVALID_DELEGATED
        return _PathAuthority.DELEGATED

    def _policy_for_scope(self, scope: Scope, *, delegated: bool) -> BearerGatePolicy:
        if delegated:
            resolver = self._delegated_policy_resolver
            if resolver is None:  # guarded during construction
                raise RuntimeError("delegated bearer policy resolver is unavailable")
            return resolver(scope)
        return BearerGatePolicy(
            token=_expected_token_for_scope(scope),
            realm="omicsclaw-remote",
        )

    @staticmethod
    def _authorization(scope: Scope) -> str | None:
        values = [
            value.decode("latin-1")
            for name, value in scope.get("headers", ())
            if name.lower() == b"authorization"
        ]
        if len(values) != 1:
            return None if not values else ""
        return values[0]

    def _enforce_expected_backend_process_epoch(self, scope: Scope) -> None:
        """Fence an authenticated HTTP request to one Backend process epoch."""

        resolver = self._backend_process_epoch_resolver
        if resolver is None:
            return
        values = [
            value.decode("latin-1")
            for name, value in scope.get("headers", ())
            if name.lower() == EXPECTED_BACKEND_PROCESS_EPOCH_HEADER
        ]
        if not values:
            return
        candidate = values[0] if len(values) == 1 else ""
        expected = resolver(scope)
        valid_candidate = len(candidate) == 64 and all(
            character in "0123456789abcdef" for character in candidate
        )
        valid_expected = len(expected) == 64 and all(
            character in "0123456789abcdef" for character in expected
        )
        if (
            not valid_candidate
            or not valid_expected
            or not hmac.compare_digest(candidate, expected)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=BACKEND_PROCESS_EPOCH_MISMATCH_DETAIL,
            )

    @staticmethod
    async def _send_auth_failure(
        scope: Scope,
        receive: Receive,
        send: Send,
        exc: HTTPException,
    ) -> None:
        if scope["type"] == "websocket":
            # Uvicorn maps a close sent before ``websocket.accept`` to an
            # HTTP 403 handshake rejection. TestClient exposes code 1008.
            await send(
                {
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "bearer authentication required",
                }
            )
            return
        response = JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=dict(exc.headers or {}),
        )
        await response(scope, receive, send)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        route_path = get_route_path(scope)
        method = str(scope.get("method", "")).upper()
        try:
            remote_token = _expected_token_for_scope(scope)
        except HTTPException as exc:
            await self._send_auth_failure(scope, receive, send, exc)
            return
        if not remote_token and _has_explicit_non_loopback_server(scope):
            if scope["type"] == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1008,
                        "reason": "remote bearer token required for non-loopback access",
                    }
                )
                return
            response = JSONResponse(
                {
                    "detail": (
                        "remote bearer token is required for non-loopback "
                        "Desktop API access"
                    )
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
            await response(scope, receive, send)
            return
        if (
            scope["type"] == "http"
            and route_path in self._public_paths
            and method in {"GET", "HEAD"}
            and self._has_verified_raw_path(scope, path)
        ):
            await self._app(scope, receive, send)
            return

        path_authority = self._path_authority(
            scope,
            path=path,
            route_path=route_path,
        )
        policy = self._policy_for_scope(
            scope,
            delegated=path_authority is _PathAuthority.DELEGATED,
        )
        try:
            policy.enforce(self._authorization(scope))
            if path_authority is _PathAuthority.INVALID_DELEGATED:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="ambiguous delegated request path",
                )
            if scope["type"] == "http":
                self._enforce_expected_backend_process_epoch(scope)
        except HTTPException as exc:
            await self._send_auth_failure(scope, receive, send, exc)
            return
        await self._app(scope, receive, send)
