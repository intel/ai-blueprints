# Copyright (C) 2024-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Caller identity, as the gateway established it.

The platform gateway (Envoy) signs people in in front of this service and
passes the signed-in user's name in `X-Forwarded-User`, replacing any value a
client sent. This module turns that header into the `current_user` dict the rest
of the codebase expects, via `Depends(get_current_user)`.
"""

import logging
from typing import Any, Dict

from fastapi import Request

logger = logging.getLogger(__name__)

DEFAULT_USER = "default"


def _anonymous_identity(username: str = DEFAULT_USER) -> Dict[str, Any]:
    return {
        "user_id": username,
        "email": f"{username}@local",
        "username": username,
        "token": "",
        "roles": [],
        "client_roles": {},
    }


async def get_current_user(request: Request) -> Dict[str, Any]:
    """
    Resolve the current user from the `X-Forwarded-User` header the gateway sets.
    Falls back to a shared default identity when the header is missing.
    """
    username = (
        request.headers.get("x-forwarded-user")
        or request.headers.get("x-user")
        or DEFAULT_USER
    ).strip()
    if not username:
        username = DEFAULT_USER
    identity = _anonymous_identity(username)
    request.state.user = identity
    return identity


__all__ = ["get_current_user"]
