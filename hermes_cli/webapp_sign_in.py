"""Behind the sign-in gate, a Webapp login lasts only while the browser credential it is bound to does.

On the gateway's Webapp (``app.state.ui_surface == "webapp"``) with the sign-in gate engaged, every
successful cookie login (a password login or an OAuth callback) also sets ``hermes_webapp_signin``: the
hex HMAC-SHA256 of ``"webapp-sign-in"`` keyed with the browser session token the gateway serves
(``web_server._SESSION_TOKEN``, kept in the gateway home by ``gateway.webapp_listener``). The gate treats
a cookie session without a matching value as signed out. Replacing that token (``hermes webapp
--sign-out``, or the CLI replacing the stored token while no gateway runs) therefore signs every browser
out, whichever provider it signed in with.

A request that passes records the digest of the token it was checked against on
``request.state.webapp_sign_in``; the socket tickets it mints carry that digest, so the sockets they open
are tracked (``web_session_sockets.track``) and close at sign-out, and a ticket minted before a sign-out
opens nothing after it. The standalone dashboard never sets or checks the binding.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from fastapi import Request
from fastapi.responses import Response

_MESSAGE = b"webapp-sign-in"


def binding(token: str) -> str:
    """The ``hermes_webapp_signin`` value for browser session token *token*."""
    return hmac.new(token.encode(), _MESSAGE, hashlib.sha256).hexdigest()


def applies(request: Request) -> bool:
    """Whether *request* reaches the gateway's Webapp behind the sign-in gate."""
    state = request.app.state
    return getattr(state, "ui_surface", None) == "webapp" and bool(getattr(state, "auth_required", False))


def _current_token() -> str:
    from hermes_cli.web_server import _SESSION_TOKEN
    return _SESSION_TOKEN


def check(request: Request) -> bool:
    """Whether the cookie login on *request* is bound to the browser credential in use now. When it
    is, records that credential's digest on ``request.state.webapp_sign_in``."""
    from hermes_cli.dashboard_auth.cookies import read_webapp_signin_cookie
    from hermes_cli.web_session_sockets import token_digest

    token = _current_token()
    presented = read_webapp_signin_cookie(request) or ""
    if not hmac.compare_digest(presented.encode(), binding(token).encode()):
        return False
    request.state.webapp_sign_in = token_digest(token)
    return True


def sign_in_of(request: Request) -> Optional[str]:
    """The browser credential digest *request*'s login was checked against, or None off the Webapp's
    gate (the socket tickets it mints then carry no binding)."""
    return getattr(request.state, "webapp_sign_in", None)


def _set(response: Response, request: Request, token: str) -> None:
    from hermes_cli.dashboard_auth.cookies import detect_https, set_webapp_signin_cookie
    from hermes_cli.dashboard_auth.prefix import prefix_from_request

    set_webapp_signin_cookie(response, value=binding(token), use_https=detect_https(request),
                             prefix=prefix_from_request(request))


def bind(response: Response, request: Request) -> None:
    """Bind the login *request* just completed to the browser credential in use now."""
    if applies(request):
        _set(response, request, _current_token())


def renew(response: Response, request: Request) -> None:
    """Keep the binding as long-lived as the session cookies a refresh just rotated, unless the
    credential it was checked against was replaced while the request ran."""
    from hermes_cli.web_session_sockets import token_digest

    signed_in = sign_in_of(request)
    token = _current_token()
    if signed_in is not None and hmac.compare_digest(signed_in, token_digest(token)):
        _set(response, request, token)
