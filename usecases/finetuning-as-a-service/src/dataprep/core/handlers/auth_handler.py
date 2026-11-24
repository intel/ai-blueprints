# Copyright (C) 2024-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Local user identity extraction (no Keycloak).

The UI proxies the resolved username in the `X-Forwarded-User` header
(fallback: the base64-encoded legacy bearer scheme is still accepted).

A training pod on the GPU cluster has no browser session. It presents a
per-job token instead (`Authorization: Bearer ftjob_<JWT>`), signed by the
Fine-Tuning API for the user who submitted the job, which only lets it read
that job's files and upload its model.
"""

import base64
import logging
from typing import Optional

import jwt
from fastapi import Header, HTTPException, Request
from fastapi.openapi.utils import get_openapi

from core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_USER = "default"

# Must match the Fine-Tuning API (app/job_token.py).
JOB_TOKEN_PREFIX = "ftjob_"
JOB_TOKEN_ISSUER = "finetuning-api"
JOB_TOKEN_AUDIENCE = "dataprep-files"

# The only file operations a training job performs: read its dataset (metadata
# for the real filename, then the content) and upload the trained model.
_JOB_READ_ROUTES = ("/v1/files/{file_id}", "/v1/files/{file_id}/content")
_JOB_UPLOAD_ROUTE = "/v1/files"


def _decode_base64_user(token: str) -> Optional[str]:
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8").strip()
        return decoded or None
    except Exception:
        return None


def _job_token_user(request: Request, token: str) -> str:
    """
    Verify a per-job token and that this request is within its scope.

    401 when the token is not valid, 403 when it is valid but this request is
    not one the job is allowed to make. Never falls back to another identity:
    the gateway routes these requests here without a login.
    """
    secret = settings.FILES_JOB_TOKEN_SECRET
    if not secret:
        logger.warning("Job token presented but FILES_JOB_TOKEN_SECRET is not set")
        raise HTTPException(status_code=401, detail="Invalid job token")
    try:
        claims = jwt.decode(
            token[len(JOB_TOKEN_PREFIX):],
            secret,
            algorithms=["HS256"],
            audience=JOB_TOKEN_AUDIENCE,
            issuer=JOB_TOKEN_ISSUER,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        logger.warning("Rejected job token: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid job token")

    route = request.scope.get("route")
    path = getattr(route, "path", "")
    method = request.method.upper()
    if method == "GET" and path in _JOB_READ_ROUTES:
        allowed = request.path_params.get("file_id") in (claims.get("files") or [])
    elif method == "POST" and path == _JOB_UPLOAD_ROUTE:
        allowed = claims.get("upload") is True
    else:
        allowed = False
    if not allowed:
        logger.warning("Job token for job %s used out of scope: %s %s", claims.get("job"), method, path)
        raise HTTPException(status_code=403, detail="Not permitted for this job token")
    return str(claims["sub"])


async def get_current_user_id(
    request: Request,
    x_forwarded_user: Optional[str] = Header(default=None, alias="X-Forwarded-User"),
    x_user: Optional[str] = Header(default=None, alias="X-User"),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """
    Resolve user identity from (in priority order):
      1. `X-Forwarded-User` header (set by the UI proxy)
      2. `X-User` header
      3. `Authorization: Bearer ftjob_<JWT>` (per-job token of a training pod)
      4. `Authorization: Bearer <base64-username>` (legacy compatibility)
      5. `DEFAULT_USER` fallback
    """
    if x_forwarded_user and x_forwarded_user.strip():
        return x_forwarded_user.strip()
    if x_user and x_user.strip():
        return x_user.strip()

    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            if parts[1].startswith(JOB_TOKEN_PREFIX):
                return _job_token_user(request, parts[1])
            decoded = _decode_base64_user(parts[1])
            if decoded:
                return decoded

    return DEFAULT_USER


def configure_openapi_auth(app):
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=settings.API_TITLE,
        version=settings.API_VERSION,
        description="File and Data Preparation Backend API's",
        routes=app.routes,
    )

    openapi_schema.setdefault("components", {})["securitySchemes"] = {
        "ForwardedUser": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Forwarded-User",
            "description": "Username set by the UI proxy (fine-tuning-ui)",
        }
    }
    openapi_schema["security"] = [{"ForwardedUser": []}]

    app.openapi_schema = openapi_schema
    return app.openapi_schema


__all__ = [
    "get_current_user_id",
    "configure_openapi_auth",
    "DEFAULT_USER",
]
