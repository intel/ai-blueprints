# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
A fine-tuned model served by the platform's KServe.

Each deployment is one ``LLMInferenceService``: KServe runs vLLM for it the
way it runs the platform's own models (same image, same CPU placement), and
this module only says what to serve. A job's result is a LoRA adapter, so the
pod serves the adapter's base model with the adapter on top:

1. ``fetch-adapter`` (Data Prep image, which talks to the object store) pulls
   the job's archive and unpacks the adapter into an ``emptyDir``.
2. ``fetch-base-model`` (the vLLM image, which has ``huggingface_hub``) reads
   the base model from the adapter's ``adapter_config.json``, downloads it into
   the shared model store once, and links ``deployments/<name>`` to it.
3. KServe mounts ``pvc://<store>/deployments/<name>`` at ``/mnt/models`` and
   runs ``vllm serve /mnt/models`` with the arguments given here, which add the
   adapter under the model's own name.

The base model is fetched here rather than by KServe's storage initializer:
that container takes no proxy settings from the resource, and the model store
keeps a base model for the next deployment of it.

What to run with -- image, flags, environment, CPU placement, the model store
-- is the serving profile, a ConfigMap the deploy publishes, so the defaults
live in one place and follow the platform's.
"""

from typing import Any, Dict, List, Optional

import yaml

from .config import get_settings
from .observability import get_logger

logger = get_logger(__name__)
settings = get_settings()

PROFILE_KEY = "profile.yaml"

FETCH_ADAPTER_CONTAINER = "fetch-adapter"
FETCH_BASE_CONTAINER = "fetch-base-model"
SERVE_CONTAINER = "main"

# KServe's name for the volume behind a pvc:// model. The init containers mount
# that volume rather than a second one of their own: a pod that names the same
# claim twice never finishes mounting it.
KSERVE_PVC_VOLUME = "kserve-pvc-source"

LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
LABEL_JOB_ID = "finetuning.intel.com/job-id"
LABEL_ROLE = "finetuning.intel.com/role"
MANAGED_BY = "finetuning-service"
ROLE_FINE_TUNED = "fine-tuned-model"

# Set once the model is registered with the gateway, so a status read does not
# have to ask the gateway each time.
ANNOTATION_SERVED_NAME = "finetuning.intel.com/served-model-name"
ANNOTATION_GATEWAY_MODE = "finetuning.intel.com/gateway-mode"
ANNOTATION_REGISTERED = "finetuning.intel.com/gateway-registered"

ADAPTER_DIR = "/mnt/adapter"
STORE_DIR = "/store"

_FETCH_ADAPTER = r'''
import os, shutil, sys, tarfile
from minio import Minio

file_id, bucket, dest = sys.argv[1:4]
work = "/tmp/unpack"
client = Minio(f"{os.environ['MINIO_HOST']}:{os.environ['MINIO_PORT']}",
               access_key=os.environ["MINIO_ACCESS_KEY"],
               secret_key=os.environ["MINIO_SECRET_KEY"], secure=False)
# Results are stored as "<user>/<file id>", so the key is found by its name.
keys = sorted(o.object_name for o in client.list_objects(bucket, recursive=True)
              if os.path.basename(o.object_name).startswith(file_id))
if not keys:
    sys.exit(f"[fetch-adapter] no object named {file_id} in bucket {bucket}")
archive = "/tmp/model.tar.gz"
client.fget_object(bucket, keys[0], archive)
print(f"[fetch-adapter] {keys[0]}: {os.path.getsize(archive)} bytes")
# The "data" filter refuses absolute paths, links out of the tree and devices.
with tarfile.open(archive) as tar:
    tar.extractall(work, filter="data")
os.remove(archive)
found = sorted((root.count(os.sep), root) for root, _, files in os.walk(work)
               if "adapter_config.json" in files)
if not found:
    sys.exit("[fetch-adapter] this result is not a LoRA adapter (no adapter_config.json). "
             "Deploy Model serves adapters; serve a merged model with the platform's Model Manager.")
src = found[0][1]
for name in os.listdir(src):
    if os.path.isfile(os.path.join(src, name)) and not name.endswith((".py", ".sh", ".so")):
        shutil.copy2(os.path.join(src, name), dest)
print(f"[fetch-adapter] adapter from {os.path.relpath(src, work)}: {sorted(os.listdir(dest))}")
'''

_FETCH_BASE = r'''
import json, os, re, sys
from huggingface_hub import snapshot_download

adapter, store, name = sys.argv[1:4]
with open(os.path.join(adapter, "adapter_config.json"), encoding="utf-8") as f:
    repo = json.load(f).get("base_model_name_or_path") or ""
if not re.fullmatch(r"[A-Za-z0-9][\w.-]*/[\w.-]+", repo):
    sys.exit(f"[fetch-base-model] the adapter names no Hugging Face base model: {repo!r}")
base = os.path.join(store, "models", repo.replace("/", "--"))
if os.path.isfile(os.path.join(base, ".complete")):
    print(f"[fetch-base-model] {repo} is already in the model store")
else:
    print(f"[fetch-base-model] downloading {repo}")
    snapshot_download(repo, local_dir=base, max_workers=4,
                      allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt", "*.jinja", "*.tiktoken"])
    open(os.path.join(base, ".complete"), "w").close()
    print(f"[fetch-base-model] {repo} downloaded")
# KServe mounts deployments/<name>; the link makes that the base model.
os.makedirs(os.path.join(store, "deployments"), exist_ok=True)
link = os.path.join(store, "deployments", name)
tmp = link + ".new"
if os.path.lexists(tmp):
    os.remove(tmp)
os.symlink(os.path.join("..", "models", repo.replace("/", "--")), tmp)
os.replace(tmp, link)
'''


class ServingProfileError(Exception):
    """The serving profile is missing or unreadable."""


def parse_profile(text: Optional[str]) -> Dict[str, Any]:
    """The serving profile, as published by the deploy (see the API playbook)."""
    if not text:
        raise ServingProfileError("the serving profile is empty")
    try:
        profile = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ServingProfileError(f"the serving profile is not valid YAML: {exc}") from exc
    if not isinstance(profile, dict) or not profile.get("image") or not (profile.get("modelStore") or {}).get("claim"):
        raise ServingProfileError("the serving profile needs at least image and modelStore.claim")
    return profile


def _env(values: Dict[str, Any]) -> List[Dict[str, str]]:
    return [{"name": str(k), "value": str(v)} for k, v in (values or {}).items()]


def _proxy_env(profile: Dict[str, Any]) -> List[Dict[str, str]]:
    proxy = profile.get("proxy") or {}
    out = []
    for key, names in (("https", ("HTTPS_PROXY", "https_proxy")), ("http", ("HTTP_PROXY", "http_proxy")),
                       ("no", ("NO_PROXY", "no_proxy"))):
        if proxy.get(key):
            out += [{"name": n, "value": str(proxy[key])} for n in names]
    return out


_RESTRICTED = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "runAsNonRoot": True,
    "runAsUser": 1000,
    "runAsGroup": 1000,
    "seccompProfile": {"type": "RuntimeDefault"},
}


def build_manifest(
    *,
    name: str,
    namespace: str,
    job_id: str,
    served_model_name: str,
    result_file_id: str,
    cpu: str,
    memory: str,
    profile: Dict[str, Any],
    extra_args: Optional[List[str]] = None,
    extra_env: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The ``LLMInferenceService`` serving one job's adapter."""
    store = profile["modelStore"]["claim"]
    fetch = profile.get("fetch") or {}
    env = {**(profile.get("env") or {}), **(extra_env or {})}
    args = [
        *[str(a) for a in profile.get("args") or []],
        # The base model under its own name, the adapter under the model's name.
        "--served-model-name", f"{served_model_name}-base",
        "--enable-lora",
        "--max-lora-rank", str(profile.get("maxLoraRank", 64)),
        "--lora-modules", f"{served_model_name}={ADAPTER_DIR}",
        *(extra_args or []),
    ]
    template: Dict[str, Any] = {
        "initContainers": [
            {
                "name": FETCH_ADAPTER_CONTAINER,
                "image": fetch.get("image"),
                "imagePullPolicy": fetch.get("pullPolicy", "IfNotPresent"),
                "command": ["python3", "-c", _FETCH_ADAPTER, result_file_id,
                            fetch.get("bucket", "dataprep"), ADAPTER_DIR],
                "env": [{"name": "HOME", "value": "/tmp"}],
                "envFrom": [{"secretRef": {"name": fetch.get("credentialsSecret", "minio-internal-creds")}}],
                "volumeMounts": [
                    {"name": "adapter", "mountPath": ADAPTER_DIR},
                    {"name": "fetch-tmp", "mountPath": "/tmp"},
                ],
                "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                              "limits": {"cpu": "1", "memory": "1Gi"}},
                "securityContext": _RESTRICTED,
            },
            {
                "name": FETCH_BASE_CONTAINER,
                "image": profile["image"],
                "command": ["python3", "-c", _FETCH_BASE, ADAPTER_DIR, STORE_DIR, name],
                "env": [
                    {"name": "HOME", "value": "/tmp"},
                    {"name": "HF_HOME", "value": "/tmp/hf"},
                    # The Xet client's buffers alone exceed this container's limit.
                    {"name": "HF_HUB_DISABLE_XET", "value": "1"},
                    *_proxy_env(profile),
                ],
                "volumeMounts": [
                    {"name": "adapter", "mountPath": ADAPTER_DIR, "readOnly": True},
                    {"name": KSERVE_PVC_VOLUME, "mountPath": STORE_DIR},
                    {"name": "fetch-tmp", "mountPath": "/tmp"},
                ],
                "resources": {"requests": {"cpu": "500m", "memory": "512Mi"},
                              "limits": {"cpu": "2", "memory": "4Gi"}},
                "securityContext": _RESTRICTED,
            },
        ],
        "containers": [
            {
                "name": SERVE_CONTAINER,
                "image": profile["image"],
                "args": args,
                "env": _env(env),
                "resources": {"requests": {"cpu": cpu, "memory": memory},
                              "limits": {"cpu": cpu, "memory": memory}},
                "volumeMounts": [{"name": "adapter", "mountPath": ADAPTER_DIR, "readOnly": True}],
                # Loading a base model on CPU takes minutes; the platform's own
                # models use the same probes.
                "startupProbe": {"httpGet": {"path": "/health", "port": 8000},
                                 "failureThreshold": 120, "periodSeconds": 10},
                "livenessProbe": {"httpGet": {"path": "/health", "port": 8000},
                                  "failureThreshold": 12, "periodSeconds": 10, "timeoutSeconds": 10},
                "readinessProbe": {"httpGet": {"path": "/health", "port": 8000},
                                   "failureThreshold": 5, "periodSeconds": 10, "timeoutSeconds": 5},
                "securityContext": _RESTRICTED,
            }
        ],
        "volumes": [
            {"name": "adapter", "emptyDir": {"sizeLimit": "4Gi"}},
            {"name": "fetch-tmp", "emptyDir": {"sizeLimit": "8Gi"}},
        ],
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
    }
    if profile.get("affinity"):
        template["affinity"] = profile["affinity"]

    return {
        "apiVersion": "serving.kserve.io/v1alpha2",
        "kind": "LLMInferenceService",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                LABEL_MANAGED_BY: MANAGED_BY,
                LABEL_ROLE: ROLE_FINE_TUNED,
                LABEL_JOB_ID: job_id,
            },
            "annotations": {
                ANNOTATION_SERVED_NAME: served_model_name,
                ANNOTATION_GATEWAY_MODE: "chat",
            },
        },
        "spec": {
            # CPU placement, as the platform's models get it (NRI balloons).
            "annotations": dict(profile.get("annotations") or {}),
            "model": {"uri": f"pvc://{store}/deployments/{name}", "name": f"{served_model_name}-base"},
            "replicas": 1,
            "template": template,
        },
    }


def service_url(name: str, namespace: str) -> str:
    """vLLM's OpenAI endpoint behind the workload Service KServe creates."""
    return f"http://{name}-kserve-workload-svc.{namespace}.svc.cluster.local:8000/v1"


def condition(resource: Optional[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    for cond in ((resource or {}).get("status") or {}).get("conditions") or []:
        if cond.get("type") == kind:
            return cond
    return None


def is_ready(resource: Optional[Dict[str, Any]]) -> bool:
    return (condition(resource, "Ready") or {}).get("status") == "True"
