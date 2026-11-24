# Copyright (C) 2024-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
The serving settings a fine-tuned deployment will actually use.

Read out of the serving profile the deploy publishes (vLLM flags in ``args``,
environment in ``env``, the KV cache size in ``kvCacheSpaceGib``) rather than
restated here, so what the dialog shows is what a deployment runs with.

Two different mechanisms carry these settings, which is why overriding them is
not uniform:

* vLLM CLI flags. Overrides are appended after the profile's own flags;
  argparse keeps the last occurrence of a flag.
* environment. ``VLLM_CPU_KVCACHE_SPACE`` is the KV cache size in GiB — on CPU
  it is the single largest term in the pod's memory footprint.
"""

from typing import Any, Dict, List, Optional, Union


from .observability import get_logger

logger = get_logger(__name__)

# What a deployment falls back to when the profile cannot be read. These mirror
# the platform's vLLM defaults at the time of writing and exist only so the
# dialog has something to show; `source` says which is in force.
FALLBACK_DEFAULTS: Dict[str, Any] = {
    "max_model_len": None,
    "max_num_seqs": 128,
    "max_num_batched_tokens": 2048,
    "dtype": "bfloat16",
    "block_size": None,
    "kv_cache_space_gib": 8,
}

# Ranges the API enforces, published so the UI does not carry its own copy.
LIMITS: Dict[str, Dict[str, Any]] = {
    "max_model_len": {"min": 256, "max": 1048576, "unit": "tokens"},
    "max_num_seqs": {"min": 1, "max": 4096, "unit": "sequences"},
    "max_num_batched_tokens": {"min": 256, "max": 1048576, "unit": "tokens"},
    "kv_cache_space_gib": {"min": 1, "max": 512, "unit": "GiB"},
    "temperature": {"min": 0.0, "max": 2.0},
    "top_p": {"min": 0.0, "max": 1.0},
}

DTYPE_CHOICES = ["auto", "bfloat16", "float16", "float32"]

# extraCmdArgs flag -> the name used in the API and UI.
_FLAG_TO_FIELD = {
    "--max-model-len": "max_model_len",
    "--max_model_len": "max_model_len",
    "--max-num-seqs": "max_num_seqs",
    "--max_num_seqs": "max_num_seqs",
    "--max-num-batched-tokens": "max_num_batched_tokens",
    "--max_num_batched_tokens": "max_num_batched_tokens",
    "--dtype": "dtype",
    "--block-size": "block_size",
    "--block_size": "block_size",
}

_INT_FIELDS = {"max_model_len", "max_num_seqs", "max_num_batched_tokens", "block_size", "kv_cache_space_gib"}


def parse_extra_cmd_args(args: List[Any]) -> Dict[str, Union[str, bool]]:
    """
    A flat vLLM argv list as a mapping.

    ``["--dtype", "bfloat16", "--enforce-eager"]`` becomes
    ``{"--dtype": "bfloat16", "--enforce-eager": True}``. Later occurrences win,
    matching argparse, so the mapping agrees with what vLLM would end up using.
    """
    out: Dict[str, Union[str, bool]] = {}
    tokens = [str(a) for a in args or []]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            index += 1
            continue
        if "=" in token:
            flag, _, value = token.partition("=")
            out[flag] = value
            index += 1
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if following is not None and not following.startswith("-"):
            out[token] = following
            index += 2
        else:
            out[token] = True
            index += 1
    return out


def from_profile(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Serving defaults for a fine-tuned deployment, read from the serving profile.

    Returns the fallbacks with ``source: "fallback"`` if there is no profile: a
    dialog showing approximately-right numbers is better than one showing none,
    and the caller can say which it is looking at.
    """
    if not isinstance(profile, dict):
        return {**FALLBACK_DEFAULTS, "source": "fallback"}

    flags = parse_extra_cmd_args(profile.get("args") or [])
    env = profile.get("env") or {}

    defaults: Dict[str, Any] = dict(FALLBACK_DEFAULTS)
    for flag, field in _FLAG_TO_FIELD.items():
        if flag in flags and flags[flag] is not True:
            defaults[field] = flags[flag]
    if profile.get("kvCacheSpaceGib") is not None:
        defaults["kv_cache_space_gib"] = profile["kvCacheSpaceGib"]
    elif "VLLM_CPU_KVCACHE_SPACE" in env:
        defaults["kv_cache_space_gib"] = env["VLLM_CPU_KVCACHE_SPACE"]

    for field in _INT_FIELDS:
        value = defaults.get(field)
        if value is None:
            continue
        try:
            defaults[field] = int(str(value).strip())
        except (TypeError, ValueError):
            logger.warning(f"Serving profile default for {field} is not a number: {value!r}")
            defaults[field] = FALLBACK_DEFAULTS.get(field)

    defaults["source"] = "profile"
    return defaults


def to_cli_args(overrides: Dict[str, Any]) -> List[str]:
    """
    Turn requested settings into vLLM flags to append to the profile's own.

    ``temperature`` and ``top_p`` are **not** server settings in vLLM — they are
    per-request sampling parameters. The closest thing is
    ``--override-generation-config``, which changes the *default* the server
    applies when a request omits them; any client can still send its own. Use the
    gateway if a value has to be enforced.
    """
    args: List[str] = []
    if overrides.get("max_model_len") is not None:
        args += ["--max-model-len", str(overrides["max_model_len"])]
    if overrides.get("max_num_seqs") is not None:
        args += ["--max-num-seqs", str(overrides["max_num_seqs"])]
    if overrides.get("max_num_batched_tokens") is not None:
        args += ["--max-num-batched-tokens", str(overrides["max_num_batched_tokens"])]
    if overrides.get("dtype"):
        args += ["--dtype", str(overrides["dtype"])]

    generation: Dict[str, Any] = {}
    if overrides.get("temperature") is not None:
        generation["temperature"] = overrides["temperature"]
    if overrides.get("top_p") is not None:
        generation["top_p"] = overrides["top_p"]
    if generation:
        # KServe starts vLLM through `eval "vllm serve ... $@"`, so every
        # argument is parsed by a shell once more: the JSON is quoted for it.
        import json
        import shlex

        args += ["--override-generation-config", shlex.quote(json.dumps(generation, separators=(",", ":")))]
    return args
