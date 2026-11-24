# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Per-job Files API token handed to the training pod.

The pod runs on the GPU cluster with no browser session, and reaches the Data
Prep files API back through the gateway to read its dataset and upload the
model. This token is what it presents there: signed with a key only this
service and Data Prep hold, for the user who submitted the job, and good only
for that job's files and one kind of write (the model upload). Data Prep
verifies it (core/handlers/auth_handler.py); the ``ftjob_`` prefix is also what
the gateway routes those requests on.
"""

import time
from typing import Iterable

import jwt

JOB_TOKEN_PREFIX = "ftjob_"
JOB_TOKEN_ISSUER = "finetuning-api"
JOB_TOKEN_AUDIENCE = "dataprep-files"


def mint_job_token(secret: str, user_id: str, job_id: str, file_ids: Iterable[str], ttl_seconds: int) -> str:
    """Token for ``user_id``'s job ``job_id``: read ``file_ids``, upload the model."""
    now = int(time.time())
    claims = {
        "iss": JOB_TOKEN_ISSUER,
        "aud": JOB_TOKEN_AUDIENCE,
        "sub": user_id,
        "job": job_id,
        "files": [f for f in file_ids if f],
        "upload": True,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return JOB_TOKEN_PREFIX + jwt.encode(claims, secret, algorithm="HS256")
