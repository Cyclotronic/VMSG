"""
Token authentication for the VMSG control API.

Why this is load-bearing
------------------------
VMSG binds its listeners to 0.0.0.0 by design, and the HTTP API can change
instrument mappings, send arbitrary SCPI to real hardware, and stop or restart
the gateway. Without authentication, anyone who can reach port 8080 can drive
the bench. There is no second control to fall back on, so this is the boundary.

Two distinct threats, both covered:

1. **Direct access** from another host on the network. Handled by requiring a
   shared token on every state-changing request.

2. **The browser** the user is already running. A page on an unrelated site can
   issue a cross-origin POST to http://localhost:8080 without ever reading the
   response - enough to wipe mappings or stop the gateway. The token defeats
   this because the attacking page cannot read the token out of the dashboard
   (same-origin policy), and `allow_origins=["*"]` is replaced with an explicit
   local-origin list so a browser will not even send the preflight through.

The token is injected into the dashboard HTML at serve time, so the UI keeps
working with no login step while remaining unreadable to other origins.
"""

import hmac
import os
import secrets
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from .logger import logger

TOKEN_HEADER = "X-VMSG-Token"
TOKEN_QUERY = "token"

# Unauthenticated: a liveness probe that reveals nothing actionable. Everything
# else - including GETs, which expose the bench layout - requires the token.
PUBLIC_PATHS = {"/api/status"}


def resolve_token(config) -> str:
    """Return the API token, generating and persisting one on first run.

    VMSG_API_TOKEN wins when set, which is what CI and container deployments
    should use. Otherwise a random token is generated once and stored in the
    config file so it survives restarts.
    """
    env_token = (os.environ.get("VMSG_API_TOKEN") or "").strip()
    if env_token:
        return env_token

    token = (config.get_setting("api_token", "") or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        config.update_settings({"api_token": token})
        logger.info("APIAUTH", "Generated a new API token and saved it to the config file.")
    return token


def request_token(request: Request) -> Optional[str]:
    header = request.headers.get(TOKEN_HEADER)
    if header:
        return header.strip()
    # Query fallback exists for curl and simple scripts. Tokens in URLs can end
    # up in logs, so the header is preferred and documented as such.
    q = request.query_params.get(TOKEN_QUERY)
    return q.strip() if q else None


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS


def install(app, config, enabled: bool = True) -> str:
    """Attach the auth middleware. Returns the active token."""
    token = resolve_token(config)

    if not enabled:
        logger.warning(
            "APIAUTH",
            "API authentication is DISABLED. The control API can change mappings, "
            "send SCPI to instruments and stop the gateway - do not run this way "
            "on a shared network.")
        return token

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api") or is_public(path):
            return await call_next(request)

        supplied = request_token(request)
        # compare_digest avoids leaking the token through timing.
        if supplied and hmac.compare_digest(supplied, token):
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        logger.warning("APIAUTH",
                       f"Rejected unauthenticated {request.method} {path} from {client}")
        # Middleware must *return* a response; an HTTPException raised here is
        # not routed through FastAPI's exception handlers.
        return JSONResponse(
            status_code=401,
            content={"detail": (
                f"Missing or invalid API token. Send it in the {TOKEN_HEADER} "
                f"header (or ?{TOKEN_QUERY}=...). The token is in the VMSG "
                f"config file, or set VMSG_API_TOKEN.")})

    logger.info("APIAUTH", "API token authentication enabled.")
    return token
