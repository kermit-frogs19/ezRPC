"""Authentication schemes and the ``Security`` dependency marker.

Mirrors FastAPI's split of responsibility: the framework ships the *plumbing*
(extract the credential, run the scheme, inject the result, reject on failure),
not the crypto. Basic uses a constant-time compare; JWT delegates to PyJWT with
pinned algorithms. Declare a scheme on a handler parameter and the framework
enforces it and injects the principal:

    jwt_auth = JWTAuth(secret=KEY, algorithms=["HS256"])

    @app.function()
    async def me(claims=Security(jwt_auth)) -> str:
        return claims["sub"]
"""

import base64
import secrets
from inspect import isawaitable
from typing import Callable

from ezRPC.common.exceptions import AuthError


class Security:
    """Marker for a handler parameter whose value is produced by running ``scheme``
    against the call's auth token. The call is rejected (AuthError -> the client)
    if the scheme rejects. Use it as the parameter's default: ``user = Security(scheme)``.
    A *scheme* is any callable ``scheme(ctx) -> principal`` (sync or async) that
    returns a truthy principal or raises ``AuthError``.
    """
    __slots__ = ("scheme",)

    def __init__(self, scheme: Callable):
        self.scheme = scheme


async def _resolve(value):
    return await value if isawaitable(value) else value


class BasicAuth:
    """HTTP Basic-style: token is ``"Basic <base64(user:pass)>"``.

    Give it either ``users={name: password}`` (compared in constant time) or a
    ``verify(user, password)`` callable returning a truthy principal or None.
    """

    def __init__(self, verify: Callable = None, users: dict = None):
        if (verify is None) == (users is None):
            raise ValueError("BasicAuth needs exactly one of verify= or users=")
        self._verify = verify
        self._users = users

    async def __call__(self, ctx):
        scheme, _, cred = (ctx.auth or "").partition(" ")
        if scheme.lower() != "basic" or not cred:
            raise AuthError("unauthenticated")
        try:
            user, _, password = base64.b64decode(cred).decode("utf-8").partition(":")
        except Exception:
            raise AuthError("unauthenticated")

        if self._users is not None:
            expected = self._users.get(user)
            if expected is None or not secrets.compare_digest(password, expected):
                raise AuthError("unauthenticated")
            return user

        principal = await _resolve(self._verify(user, password))
        if not principal:
            raise AuthError("unauthenticated")
        return principal


class BearerAuth:
    """Opaque bearer token: ``"Bearer <token>"``. ``verify(token)`` returns a truthy
    principal or None (sync or async). Bring your own validation — for JWTs, use
    ``JWTAuth`` instead of hand-verifying here."""

    def __init__(self, verify: Callable):
        self._verify = verify

    async def __call__(self, ctx):
        scheme, _, cred = (ctx.auth or "").partition(" ")
        if scheme.lower() != "bearer" or not cred:
            raise AuthError("unauthenticated")
        principal = await _resolve(self._verify(cred))
        if not principal:
            raise AuthError("unauthenticated")
        return principal


class JWTAuth:
    """Bearer JWT verified with PyJWT. Algorithms are **pinned** (never inferred from
    the token) to avoid alg-confusion attacks; expiry is enforced by PyJWT. Returns
    the decoded claims dict. Requires the optional ``pyjwt`` dependency
    (``pip install ezRPC[jwt]``)."""

    def __init__(self, secret, algorithms, **decode_options):
        try:
            import jwt
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "JWTAuth requires PyJWT — install it with `pip install ezRPC[jwt]`"
            ) from e
        self._jwt = jwt
        self._secret = secret
        self._algorithms = list(algorithms)
        self._options = decode_options

    async def __call__(self, ctx):
        scheme, _, cred = (ctx.auth or "").partition(" ")
        if scheme.lower() != "bearer" or not cred:
            raise AuthError("unauthenticated")
        try:
            return self._jwt.decode(cred, self._secret, algorithms=self._algorithms, **self._options)
        except self._jwt.ExpiredSignatureError:
            raise AuthError("token expired")
        except self._jwt.InvalidTokenError:
            raise AuthError("unauthenticated")
