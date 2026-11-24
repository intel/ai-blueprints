# Copyright (C) 2024-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Names a fine-tuned model is deployed under.

This mirrors ``src/ui/src/features/finetuning/utils/fineTuningHelpers.ts``. The
UI shows the model name, while this service performs the deployment, so the
two have to derive the same name from
the same job or a deployment triggered from the UI would come up under a
different name than the page advertises. Keep both sides in step.
"""

import re
from datetime import datetime, timezone
from typing import Optional, Union

# Dots are allowed because base model names contain them
# (Llama-3.2-3B-Instruct); commas, equals signs and slashes stay out so the name
# is safe in a vLLM ``--lora-modules name=path`` and as ``--served-model-name``.
MODEL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
MAX_MODEL_NAME_LENGTH = 64

FINETUNED_MARKER = "-finetuned-"

_ILLEGAL_CHARS = re.compile(r"[^a-zA-Z0-9._-]+")
_LEADING_SEPARATORS = re.compile(r"^[-._]+")
_TRAILING_SEPARATORS = re.compile(r"[-._]+$")

# Deployment names are RFC 1123 labels, and KServe derives longer ones from
# them: <name>-kserve-workload-svc has to stay within 63 characters.
_NON_LABEL_CHARS = re.compile(r"[^a-z0-9-]")
_REPEATED_DASHES = re.compile(r"-+")
MAX_RELEASE_NAME_LENGTH = 43


def format_model_name_timestamp(moment: datetime) -> str:
    """Digits only — no separators, so the name stays a single safe token."""
    return moment.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def sanitize_model_name_part(value: str) -> str:
    """Reduce a base model id such as ``meta-llama/Llama-3.2-3B`` to its name."""
    name = (value or "").split("/")[-1]
    name = _ILLEGAL_CHARS.sub("-", name)
    name = _LEADING_SEPARATORS.sub("", name)
    return _TRAILING_SEPARATORS.sub("", name)


def build_fine_tuned_model_name(
    base_model: str,
    timestamp: Union[int, float, datetime],
) -> str:
    """Base model plus a UTC timestamp, which keeps repeated fine-tunes apart."""
    if isinstance(timestamp, datetime):
        moment = timestamp
    else:
        moment = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)

    stamp = format_model_name_timestamp(moment)
    budget = MAX_MODEL_NAME_LENGTH - len(FINETUNED_MARKER) - len(stamp)
    base = sanitize_model_name_part(base_model) or "model"
    base = _TRAILING_SEPARATORS.sub("", base[:budget])

    return f"{base}{FINETUNED_MARKER}{stamp}"


def resolve_served_model_name(
    base_model: str,
    created_at: Union[int, float, datetime],
    suffix: Optional[str] = None,
) -> str:
    """
    The name the model is served and registered under: whatever was chosen when
    the job was submitted, else a name derived from the job itself so jobs
    created before the name field existed still get a readable one.
    """
    chosen = (suffix or "").strip()
    if chosen and MODEL_NAME_PATTERN.match(chosen) and len(chosen) <= MAX_MODEL_NAME_LENGTH:
        return chosen
    return build_fine_tuned_model_name(base_model, created_at)


def release_name_for_job(job_id: str) -> str:
    """Name of a job's model deployment (its LLMInferenceService)."""
    name = _NON_LABEL_CHARS.sub("-", f"ft-{job_id}".lower())
    name = _REPEATED_DASHES.sub("-", name).strip("-")
    return name[:MAX_RELEASE_NAME_LENGTH].rstrip("-")
