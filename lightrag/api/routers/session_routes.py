"""Shared login endpoints for the regular server and the RAG-free batch manager."""

import asyncio

from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm

from lightrag.api.login_rate_limit import LoginRateLimiter
from lightrag.api.utils_api import get_combined_auth_dependency


def mount_session_routes(
    app, *, handler, args, metadata, api_key, limiter_type=LoginRateLimiter
):
    verify_auth = get_combined_auth_dependency(api_key, respect_whitelist=False)

    @app.get("/auth/verify", dependencies=[Depends(verify_auth)])
    async def verify_credentials():
        return {"status": "ok"}

    def guest():
        return {
            "access_token": handler.create_token(
                username="guest", role="guest", metadata={"auth_mode": "disabled"}
            ),
            "token_type": "bearer",
            "auth_mode": "disabled",
            "message": "Authentication is disabled. Using guest access.",
            **metadata,
        }

    @app.get("/auth-status")
    async def get_auth_status():
        if not handler.accounts:
            return {"auth_configured": False, **guest()}
        return {"auth_configured": True, "auth_mode": "enabled", **metadata}

    limiter = limiter_type(
        max_attempts=getattr(args, "login_max_failed_attempts", 5),
        window_seconds=getattr(args, "login_lockout_window_seconds", 300.0),
    )

    @app.post("/login")
    async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
        if not handler.accounts:
            return guest()
        username = form_data.username
        client_ip = request.client.host if request.client else "unknown"
        key = f"{client_ip}:{username}"
        retry_after = limiter.retry_after(key)
        if retry_after is not None:
            raise HTTPException(
                429,
                "Too many failed login attempts. Please try again later.",
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
        # Reserve before bcrypt yields, so concurrent attempts cannot pass the
        # same preflight. Keep the reservation until the outcome is recorded.
        limiter.reserve_attempt(key)
        try:
            valid = await asyncio.to_thread(
                handler.verify_password, username, form_data.password
            )
            if not valid:
                limiter.commit_failure(key)
                raise HTTPException(401, "Incorrect credentials")
            limiter.reset(key)
        finally:
            limiter.release(key)
        return {
            "access_token": handler.create_token(
                username=username, role="user", metadata={"auth_mode": "enabled"}
            ),
            "token_type": "bearer",
            "auth_mode": "enabled",
            **metadata,
        }
