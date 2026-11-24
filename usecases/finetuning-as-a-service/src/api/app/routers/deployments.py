# Copyright (C) 2024-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Serving a fine-tuned model.

A deployment is one KServe ``LLMInferenceService`` in the inference namespace:
the platform's KServe runs vLLM for it the way it runs the platform's own
models, and this service registers the model with the LiteLLM gateway once it
answers (see ``kserve_serving`` for what the resource holds). Progress is read
back from the resource and its pod on every status call, so nothing here keeps
state of its own.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Query, Request

from ..auth import get_current_user
from ..config import get_settings
from ..database import db_manager
from ..errors import (
    ConflictError, InvalidRequestError, ResourceNotFoundError, ServerError,
    ServiceUnavailableError,
    PermissionError as ForbiddenError,
)
from ..gateway import GatewayError, gateway_client
from ..k8s import KubeApiError, kube_client
from ..middleware import limiter
from ..model_naming import release_name_for_job, resolve_served_model_name
from ..observability import get_logger
from ..schemas import (
    DeploymentCapacity, DeploymentLogs, DeploymentPhase, DeploymentStep,
    DeploymentStepStatus, DeployModelRequest, ModelDeploymentStatus,
)
from .. import capacity as capacity_mod
from .. import kserve_serving as ks
from ..capacity import QuantityError, format_cpu, format_memory
from ..sizing import node_budget, recommend, request_bounds
from .. import serving_defaults as serving

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/fine_tuning", tags=["Fine-tuning"])
settings = get_settings()

# Steps a deployment goes through, in order, with the share of the overall
# progress bar each one is worth. The base-model download and the weight load
# are the long poles; creating the resource takes a moment.
STEP_INSTALL = "install"
STEP_DOWNLOAD = "download"
STEP_EXTRACT = "extract"
STEP_SERVE = "serve"
STEP_REGISTER = "register"

STEPS: List[Tuple[str, str, int]] = [
    (STEP_INSTALL, "Create the model service", 5),
    (STEP_DOWNLOAD, "Fetch the fine-tuned adapter", 10),
    (STEP_EXTRACT, "Download the base model", 40),
    (STEP_SERVE, "Start vLLM and load the model", 40),
    (STEP_REGISTER, "Register with LiteLLM gateway", 5),
]

PHASE_FOR_STEP = {
    STEP_INSTALL: DeploymentPhase.INSTALLING,
    STEP_DOWNLOAD: DeploymentPhase.DOWNLOADING,
    STEP_EXTRACT: DeploymentPhase.EXTRACTING,
    STEP_SERVE: DeploymentPhase.LOADING,
    STEP_REGISTER: DeploymentPhase.REGISTERING,
}

MESSAGE_FOR_PHASE = {
    DeploymentPhase.NOT_DEPLOYED: "Not deployed",
    DeploymentPhase.INSTALLING: "Creating the model service",
    DeploymentPhase.DOWNLOADING: "Fetching the fine-tuned adapter from object storage",
    DeploymentPhase.EXTRACTING: "Downloading the base model into the model store (once per base model)",
    DeploymentPhase.LOADING: "Starting vLLM and loading the model weights",
    DeploymentPhase.REGISTERING: "Model is serving — registering it with the LiteLLM gateway",
    DeploymentPhase.READY: "Model is serving and registered with the LiteLLM gateway",
    DeploymentPhase.FAILED: "Deployment failed",
    DeploymentPhase.UNINSTALLING: "Removing the model deployment",
    DeploymentPhase.UNAVAILABLE: "Deploying from the UI is not available on this installation",
}

# A pod that has not got past scheduling for this long is not going to on its
# own; the scheduler's reason is the useful thing to show.
UNSCHEDULABLE = "Unschedulable"


async def _load_owned_job(job_id: str, user_id: str) -> Dict[str, Any]:
    """Fetch a job from the local cache, enforcing ownership."""
    row = await db_manager.fetch_one("""
        SELECT id, model, status, created_at, fine_tuned_model, suffix, user_id
        FROM fine_tuning_jobs
        WHERE id = $1
    """, job_id, timeout=30)

    if not row:
        raise ResourceNotFoundError("fine-tuning job", job_id)

    if str(row.get("user_id", "")) != str(user_id):
        logger.warning(
            "Unauthorized model deployment access attempt — returning 403",
            extra={"job_id": job_id, "requesting_user_id": user_id}
        )
        raise ForbiddenError("You do not have permission to access this fine-tuning job")

    return dict(row)


def _container_state(statuses: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for status in statuses or []:
        if status.get("name") == name:
            return status
    return None


def _step_from_container(
    container: Optional[Dict[str, Any]]
) -> Tuple[DeploymentStepStatus, Optional[str]]:
    """Map a container status onto a step status."""
    if not container:
        return DeploymentStepStatus.PENDING, None

    state = container.get("state") or {}
    last = (container.get("lastState") or {}).get("terminated") or {}

    terminated = state.get("terminated")
    if terminated:
        if terminated.get("exitCode") == 0:
            return DeploymentStepStatus.DONE, None
        return (
            DeploymentStepStatus.ERROR,
            terminated.get("message") or terminated.get("reason") or "Container failed",
        )

    if state.get("running"):
        return DeploymentStepStatus.ACTIVE, None

    waiting = state.get("waiting") or {}
    reason = waiting.get("reason")
    # A container that keeps crashing is not going to become ready on its own.
    if reason == "CrashLoopBackOff":
        return DeploymentStepStatus.ERROR, last.get("reason") or waiting.get("message") or reason
    if reason in ("ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"):
        return DeploymentStepStatus.ERROR, waiting.get("message") or reason
    return DeploymentStepStatus.PENDING, reason


def _pod_is_ready(pod: Dict[str, Any]) -> bool:
    for condition in (pod.get("status") or {}).get("conditions") or []:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def _unschedulable(pod: Dict[str, Any]) -> Optional[str]:
    for condition in (pod.get("status") or {}).get("conditions") or []:
        if condition.get("type") == "PodScheduled" and condition.get("reason") == UNSCHEDULABLE:
            return condition.get("message") or UNSCHEDULABLE
    return None


def _newest_pod(pods: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The most recent pod of the model that is not on its way out."""
    candidates = [p for p in pods if not (p.get("metadata") or {}).get("deletionTimestamp")]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.get("metadata") or {}).get("creationTimestamp") or "")


def _pod_selector(name: str) -> str:
    return f"app.kubernetes.io/name={name},kserve.io/component=workload"


def _build_steps(
    service: Dict[str, Any], pod: Optional[Dict[str, Any]]
) -> Dict[str, Tuple[DeploymentStepStatus, Optional[str]]]:
    """Derive each step's state from the resource and its newest pod."""
    states: Dict[str, Tuple[DeploymentStepStatus, Optional[str]]] = {
        key: (DeploymentStepStatus.PENDING, None) for key, _, _ in STEPS
    }
    states[STEP_INSTALL] = (DeploymentStepStatus.DONE, None)

    if not pod:
        # KServe could not create the workload at all (a bad template, a
        # missing claim): its Ready condition says why.
        ready = ks.condition(service, "Ready") or {}
        if ready.get("status") == "False" and "failed" in (ready.get("message") or "").lower():
            states[STEP_INSTALL] = (DeploymentStepStatus.ERROR, ready.get("message"))
        else:
            states[STEP_DOWNLOAD] = (DeploymentStepStatus.PENDING, "Waiting for a pod to be scheduled")
        return states

    unschedulable = _unschedulable(pod)
    if unschedulable:
        states[STEP_DOWNLOAD] = (DeploymentStepStatus.PENDING, unschedulable)
        return states

    pod_status = pod.get("status") or {}
    init_statuses = pod_status.get("initContainerStatuses") or []
    states[STEP_DOWNLOAD] = _step_from_container(_container_state(init_statuses, ks.FETCH_ADAPTER_CONTAINER))
    states[STEP_EXTRACT] = _step_from_container(_container_state(init_statuses, ks.FETCH_BASE_CONTAINER))

    serve_container = _container_state(pod_status.get("containerStatuses") or [], ks.SERVE_CONTAINER)
    serve_state, serve_detail = _step_from_container(serve_container)
    crashed = ((serve_container or {}).get("lastState") or {}).get("terminated") or {}
    if serve_state == DeploymentStepStatus.ACTIVE and _pod_is_ready(pod):
        serve_state, serve_detail = DeploymentStepStatus.DONE, None
    elif crashed and crashed.get("exitCode", 0) != 0:
        # vLLM exited and is being restarted: a start that failed once (a
        # setting that does not fit) fails the same way again, so it is
        # reported, with the deployment retryable, rather than as loading.
        serve_state = DeploymentStepStatus.ERROR
        serve_detail = "vLLM exited during start; its log says why (Logs tab)"
    elif serve_state == DeploymentStepStatus.ACTIVE:
        serve_detail = serve_detail or "Loading model weights"
    states[STEP_SERVE] = (serve_state, serve_detail)

    annotations = (service.get("metadata") or {}).get("annotations") or {}
    if annotations.get(ks.ANNOTATION_REGISTERED) == "true":
        states[STEP_REGISTER] = (DeploymentStepStatus.DONE, None)
    elif serve_state == DeploymentStepStatus.DONE:
        states[STEP_REGISTER] = (DeploymentStepStatus.ACTIVE, "Adding the model to the gateway")
    return states


def _progress(states: Dict[str, Tuple[DeploymentStepStatus, Optional[str]]]) -> int:
    total = 0
    for key, _, weight in STEPS:
        state = states[key][0]
        if state == DeploymentStepStatus.DONE:
            total += weight
        elif state == DeploymentStepStatus.ACTIVE:
            total += weight // 2
    return min(total, 100)


def _phase(
    states: Dict[str, Tuple[DeploymentStepStatus, Optional[str]]]
) -> Tuple[DeploymentPhase, Optional[str]]:
    for key, _, _ in STEPS:
        state, detail = states[key]
        if state == DeploymentStepStatus.ERROR:
            return DeploymentPhase.FAILED, detail
    for key, _, _ in STEPS:
        if states[key][0] != DeploymentStepStatus.DONE:
            return PHASE_FOR_STEP[key], None
    return DeploymentPhase.READY, None


def _failed_container(pod: Optional[Dict[str, Any]]) -> Optional[str]:
    """Name of the container that broke in this pod, if one of them did."""
    if not pod:
        return None
    pod_status = pod.get("status") or {}
    for init_status in pod_status.get("initContainerStatuses") or []:
        if _step_from_container(init_status)[0] == DeploymentStepStatus.ERROR:
            return init_status.get("name")
    return ks.SERVE_CONTAINER


# Kubernetes probes a serving model every few seconds and vLLM logs every one of
# them, so on a healthy deployment they are the only lines there are: a 40-line
# tail comes back as forty copies of `"GET /health HTTP/1.1" 200 OK` and nothing
# about the model. Dropped by default, with the count reported so a reader can
# tell a filtered log from a quiet one.
_PROBE_LINE = re.compile(r'"(?:GET|HEAD) (?:/health|/ping|/metrics|/v1/health)[^"]*" 2\d\d')


def _without_probe_lines(lines: List[str]) -> Tuple[List[str], int]:
    """The log with successful health-check requests removed, and how many went."""
    kept = [line for line in lines if not _PROBE_LINE.search(line)]
    return kept, len(lines) - len(kept)


async def _collect_logs(
    namespace: str,
    phase: DeploymentPhase,
    pod: Optional[Dict[str, Any]],
    tail: Optional[int] = None,
) -> Tuple[List[str], Optional[str]]:
    """Tail the log of whichever container is currently doing the work."""
    tail = tail or settings.deployment.log_tail_lines
    container = {
        DeploymentPhase.DOWNLOADING: ks.FETCH_ADAPTER_CONTAINER,
        DeploymentPhase.EXTRACTING: ks.FETCH_BASE_CONTAINER,
        DeploymentPhase.LOADING: ks.SERVE_CONTAINER,
        DeploymentPhase.REGISTERING: ks.SERVE_CONTAINER,
        DeploymentPhase.READY: ks.SERVE_CONTAINER,
    }.get(phase)
    if phase == DeploymentPhase.FAILED:
        container = _failed_container(pod)
    if not container or not pod:
        return [], None
    pod_name = (pod.get("metadata") or {}).get("name")
    try:
        log = await kube_client.read_pod_log(namespace, pod_name, container, tail)
    except KubeApiError as exc:
        # A container that has not started yet has no log to read.
        if exc.status == 400:
            return [], f"{pod_name}/{container}"
        raise
    return [line for line in log.splitlines() if line.strip()], f"{pod_name}/{container}"


async def register_if_ready(namespace: str, service: Dict[str, Any]) -> bool:
    """
    Register a ready model with the gateway, once. True when it is registered.

    Called from the status read, so a user watching the deployment sees it go
    live without waiting, and from the background loop, so a model nobody is
    watching gets there too. Registering twice is harmless: the gateway entry
    is replaced, not duplicated.
    """
    metadata = service.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    if annotations.get(ks.ANNOTATION_REGISTERED) == "true":
        return True
    served_name = annotations.get(ks.ANNOTATION_SERVED_NAME)
    if not served_name or metadata.get("deletionTimestamp") or not ks.is_ready(service):
        return False
    mode = annotations.get(ks.ANNOTATION_GATEWAY_MODE, "chat")
    extra = {"encoding_format": "float"} if mode == "embedding" else None
    try:
        await gateway_client.register_model(
            served_name, ks.service_url(metadata["name"], namespace), mode=mode, extra_params=extra
        )
    except GatewayError as exc:
        logger.warning(f"Could not register {served_name} with the gateway yet: {exc.message}")
        return False
    await kube_client.annotate_llm_service(namespace, metadata["name"], {ks.ANNOTATION_REGISTERED: "true"})
    logger.info("Registered a served model with the gateway", extra={"model": served_name})
    return True


async def _deployment_status(
    job_row: Dict[str, Any], include_logs: bool = False, log_tail: Optional[int] = None
) -> ModelDeploymentStatus:
    """
    Assemble the deployment status of one job's model from cluster state.

    Logs are off by default. Status is polled every few seconds while a deployment
    comes up, and each tail is a separate kubelet round-trip through the API server
    for output that is only being looked at when the Logs view is open; that view
    asks for them itself.
    """
    job_id = job_row["id"]
    namespace = settings.deployment.namespace
    name = release_name_for_job(job_id)
    served_model_name = resolve_served_model_name(
        job_row["model"], job_row["created_at"], job_row.get("suffix")
    )
    deployable = job_row["status"] == "succeeded" and bool(job_row.get("fine_tuned_model"))

    base = {
        "job_id": job_id,
        "release_name": name,
        "served_model_name": served_model_name,
        "namespace": namespace,
    }

    if not settings.deployment.enabled or not kube_client.available:
        return ModelDeploymentStatus(
            **base,
            phase=DeploymentPhase.UNAVAILABLE,
            message=MESSAGE_FOR_PHASE[DeploymentPhase.UNAVAILABLE],
        )

    service = await kube_client.get_llm_service(namespace, name)
    if not service:
        return ModelDeploymentStatus(
            **base,
            phase=DeploymentPhase.NOT_DEPLOYED,
            message=MESSAGE_FOR_PHASE[DeploymentPhase.NOT_DEPLOYED],
            can_deploy=deployable,
        )

    # On its way out: reporting on its pods would be misleading.
    if (service.get("metadata") or {}).get("deletionTimestamp"):
        return ModelDeploymentStatus(
            **base,
            phase=DeploymentPhase.UNINSTALLING,
            message=MESSAGE_FOR_PHASE[DeploymentPhase.UNINSTALLING],
        )

    if ks.is_ready(service):
        await register_if_ready(namespace, service)
        service = await kube_client.get_llm_service(namespace, name) or service

    pod = _newest_pod(await kube_client.list_pods(namespace, label_selector=_pod_selector(name)))
    states = _build_steps(service, pod)
    phase, failure = _phase(states)
    logs, log_source = (
        await _collect_logs(namespace, phase, pod, tail=log_tail) if include_logs else ([], None)
    )

    steps = [
        DeploymentStep(key=key, title=title, status=states[key][0], detail=states[key][1])
        for key, title, _ in STEPS
    ]

    return ModelDeploymentStatus(
        **base,
        phase=phase,
        message=MESSAGE_FOR_PHASE[phase],
        progress=_progress(states),
        steps=steps,
        logs=logs,
        log_source=log_source,
        gateway_registered=states[STEP_REGISTER][0] == DeploymentStepStatus.DONE,
        service_url=ks.service_url(name, namespace),
        # Retrying a failed deployment is the normal way out of it.
        can_deploy=deployable and phase == DeploymentPhase.FAILED,
        can_undeploy=True,
        error=failure,
    )


def _amount(values: Dict[str, int]) -> Dict[str, Any]:
    """A cpu/memory pair with printable forms alongside the machine units."""
    out: Dict[str, Any] = {
        "cpu_millis": values.get("cpu_millis", 0),
        "memory_bytes": values.get("memory_bytes", 0),
        "cpu": format_cpu(values.get("cpu_millis", 0)),
        "memory": format_memory(values.get("memory_bytes", 0)),
    }
    if "pods" in values:
        out["pods"] = values["pods"]
    return out


async def _deployments_in_use(namespace: str) -> int:
    """How many models are being served, for the cap that is separate from size."""
    try:
        live = await kube_client.list_llm_services(
            namespace, label_selector=f"{ks.LABEL_ROLE}={ks.ROLE_FINE_TUNED}"
        )
    except KubeApiError:
        return 0
    return len(live)


def _parse_override(field: str, value: str, *, is_cpu: bool) -> int:
    """A user-supplied quantity, in machine units, or a 400 naming the field."""
    try:
        return capacity_mod.parse_cpu_millis(value) if is_cpu else capacity_mod.parse_memory_bytes(value)
    except QuantityError as exc:
        raise InvalidRequestError(str(exc), param=field, code="invalid_quantity") from exc


def _validate_bounds(cpu_millis: int, memory_bytes: int, bounds: Dict[str, Any]) -> None:
    """
    Hold a request to the range the form was shown, using the same computation.

    The floor is the model's, so the message quotes the formula behind it: told
    only "memory must be at least 54Gi" a caller has no way to see that lowering
    the KV cache would lower the floor too.
    """
    if cpu_millis < bounds["cpu_min_millis"]:
        raise InvalidRequestError(
            f"cpu must be at least {bounds['cpu_min']} cores for this model "
            f"({bounds['cpu_formula']})",
            param="cpu", code="cpu_out_of_range",
        )
    if cpu_millis > bounds["cpu_max_millis"]:
        raise InvalidRequestError(
            f"cpu must be at most {format_cpu(bounds['cpu_max_millis'])} cores, "
            f"which is what the roomiest node has",
            param="cpu", code="cpu_out_of_range",
        )
    if memory_bytes < bounds["memory_min_bytes"]:
        raise InvalidRequestError(
            f"memory must be at least {bounds['memory_min']} for this model "
            f"({bounds['memory_formula']})",
            param="memory", code="memory_out_of_range",
        )
    if memory_bytes > bounds["memory_max_bytes"]:
        raise InvalidRequestError(
            f"memory must be at most {format_memory(bounds['memory_max_bytes'])}, "
            f"which is what the roomiest node has",
            param="memory", code="memory_out_of_range",
        )


async def _serving_profile() -> Dict[str, Any]:
    """The serving profile the deploy published (image, flags, model store)."""
    config = settings.deployment
    cm = await kube_client.get_config_map(config.namespace, config.profile_config_map)
    if not cm:
        raise ks.ServingProfileError(
            f"ConfigMap {config.profile_config_map} is missing in namespace {config.namespace}"
        )
    return ks.parse_profile((cm.get("data") or {}).get(ks.PROFILE_KEY))


async def _serving_defaults() -> Dict[str, Any]:
    """
    What a deployment will serve with, read from the serving profile.

    Not restated in this service, so it cannot drift from what is deployed.
    Falls back to documented values if the profile is unreadable, flagged by
    `source`.
    """
    try:
        profile = await _serving_profile()
    except (KubeApiError, ks.ServingProfileError) as exc:
        logger.info("Serving profile unreadable for serving defaults", extra={"error": str(exc)})
        profile = None
    return serving.from_profile(profile)


async def _build_capacity(
    model_id: str, overrides: Optional[DeployModelRequest] = None
) -> DeploymentCapacity:
    """
    What is free, what this model should ask for, and whether that fits.

    Capacity being unreadable is reported as ``available: false`` with a reason
    rather than as zero free: the difference between "no room" and "we cannot
    see" decides whether a deployment should be blocked, and only one of them
    should block it.
    """
    config = settings.deployment
    used = await _deployments_in_use(config.namespace)
    defaults = await _serving_defaults()
    # A smaller KV cache reservation means a smaller memory request, so the
    # suggestion has to follow whatever the caller has dialled in.
    kv_gib = defaults.get("kv_cache_space_gib")
    if overrides and overrides.kv_cache_space_gib is not None:
        kv_gib = overrides.kv_cache_space_gib
    base = {
        "deployments_used": used,
        "deployments_max": config.max_deployments,
        "override_allowed": config.allow_capacity_override,
        "serving_defaults": defaults,
        "serving_limits": serving.LIMITS,
        "dtype_choices": serving.DTYPE_CHOICES,
    }

    try:
        snap = await capacity_mod.snapshot()
    except capacity_mod.CapacityUnavailable as exc:
        logger.info("Deployment capacity unavailable", extra={"reason": str(exc)})
        return DeploymentCapacity(
            available=False,
            message=(
                f"Cluster capacity is unknown: {exc}. The model can still be deployed, "
                f"but this cannot tell you in advance whether it will fit."
            ),
            recommended=recommend(model_id, kv_cache_space_gib=kv_gib),
            # No node to read, so the ceiling falls back to the absolute maximum.
            # The floor is the model's and does not depend on cluster state.
            request_limits=request_bounds(model_id, kv_cache_space_gib=kv_gib),
            **base,
        )

    schedulable = [row for row in snap["nodes"] if row["schedulable"]]
    biggest_row = max(
        schedulable,
        key=lambda row: row["allocatable"]["cpu_millis"],
        default=None,
    )
    biggest = (biggest_row or {}).get("allocatable") or {"cpu_millis": 0, "memory_bytes": 0}
    budget = node_budget(biggest, config.max_node_fraction)
    rec = recommend(
        model_id,
        max_cpu_millis=budget["cpu_millis"] or None,
        max_memory_bytes=budget["memory_bytes"] or None,
        kv_cache_space_gib=kv_gib,
    )
    # One pod runs on one node, so the ceiling is that node's allocatable rather
    # than the cluster total. Computed here and returned, so the form and this
    # validator cannot disagree about what the user was allowed to enter.
    bounds = request_bounds(
        model_id,
        kv_cache_space_gib=kv_gib,
        node_allocatable={**biggest, "name": (biggest_row or {}).get("name")},
    )

    cpu_millis, memory_bytes = rec["cpu_millis"], rec["memory_bytes"]
    if overrides and overrides.cpu:
        cpu_millis = _parse_override("cpu", overrides.cpu, is_cpu=True)
    if overrides and overrides.memory:
        memory_bytes = _parse_override("memory", overrides.memory, is_cpu=False)
    if overrides and (overrides.cpu or overrides.memory):
        _validate_bounds(cpu_millis, memory_bytes, bounds)

    fit = capacity_mod.check_fit(snap, cpu_millis, memory_bytes)

    return DeploymentCapacity(
        available=True,
        basis=snap["basis"],
        live_usage_available=snap["live_usage_available"],
        nodes=[
            {
                "name": row["name"],
                "schedulable": row["schedulable"],
                "unschedulable_reason": row["unschedulable_reason"],
                "allocatable": _amount(row["allocatable"]),
                "committed": _amount(row["committed"]),
                "free": _amount(row["free"]),
            }
            for row in snap["nodes"]
        ],
        totals={key: _amount(value) for key, value in snap["totals"].items()},
        largest_free=_amount(fit["largest_free"]),
        recommended=rec,
        request_limits=bounds,
        fits=fit["fits"],
        shortfall=None if fit["fits"] else capacity_mod.describe_shortfall(fit),
        **base,
    )


async def _resolve_deploy_request(
    model_id: str, overrides: Optional[DeployModelRequest]
) -> Dict[str, Any]:
    """
    Settle the values to install with, refusing a request that cannot be placed.

    The check is here and not only in the UI because this endpoint is reachable
    directly. It is skippable with ``force`` because the snapshot is a moment old
    and vLLM's real appetite is not exactly its request — but an override is
    recorded, so a node that fell over has a trail leading back to it.
    """
    config = settings.deployment
    forced = bool(overrides and overrides.force)
    report = await _build_capacity(model_id, overrides)

    if report.available and report.fits is False:
        if not forced:
            raise ConflictError(
                f"Not enough room to serve this model: needs {report.shortfall}. Reduce the CPU "
                f"or memory request, remove a deployed model, or deploy anyway with force.",
                code="insufficient_cluster_resources",
            )
        if not config.allow_capacity_override:
            raise ConflictError(
                f"Not enough room to serve this model: needs {report.shortfall}. Overriding the "
                f"capacity check is disabled on this installation.",
                code="insufficient_cluster_resources",
            )
        logger.warning(
            "Deploying a model that does not fit, by explicit override",
            extra={"model": model_id, "shortfall": report.shortfall},
        )

    recommended = report.recommended or {}
    resolved: Dict[str, Any] = {
        "cpu": (overrides.cpu if overrides and overrides.cpu else recommended.get("cpu")),
        "memory": (overrides.memory if overrides and overrides.memory else recommended.get("memory")),
        "forced": forced,
    }
    if overrides:
        for field in (
            "tensor_parallel_size", "pipeline_parallel_size", "kv_cache_space_gib",
            "max_model_len", "max_num_seqs", "max_num_batched_tokens", "dtype",
            "temperature", "top_p",
        ):
            value = getattr(overrides, field)
            if value is not None:
                resolved[field] = value
    return resolved



async def _wait_until_gone(namespace: str, name: str) -> None:
    """A failed attempt is removed before a retry; deletion is asynchronous."""
    for _ in range(60):
        if await kube_client.get_llm_service(namespace, name) is None:
            return
        await asyncio.sleep(1)
    raise ServiceUnavailableError(
        "The previous deployment attempt is still being removed. Please try again in a moment."
    )


def _require_deployment_support() -> None:
    if not settings.deployment.enabled:
        raise ServiceUnavailableError("Deploying models from the UI is disabled on this installation.")
    if not kube_client.available:
        raise ServiceUnavailableError(
            "This service is not running inside a Kubernetes cluster, so it cannot deploy models."
        )


@router.get("/jobs/{job_id}/deployment-capacity", response_model=DeploymentCapacity)
@limiter.limit(f"{settings.rate_limit.job_read}/minute")
async def get_deployment_capacity(
    request: Request,
    job_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Room to serve this model, and what to ask for.

    Reports each node's allocatable, already-reserved and free CPU/memory, a
    suggested request derived from the base model, and whether that request fits.
    Numbers are **reservations, not measurements**: Kubernetes admits a pod by
    comparing requests against allocatable, and this cluster has no
    metrics-server, so live utilisation is unavailable (`live_usage_available`).
    """
    user_id = current_user["user_id"]
    job_row = await _load_owned_job(job_id, user_id)
    return await _build_capacity(job_row["model"])


@router.post("/jobs/{job_id}/deploy", response_model=ModelDeploymentStatus)
@limiter.limit(f"{settings.rate_limit.job_create}/minute")
async def deploy_fine_tuned_model(
    request: Request,
    job_id: str,
    overrides: Optional[DeployModelRequest] = None,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Deploy a fine-tuned model and register it with the LiteLLM gateway.

    Creates a KServe LLMInferenceService in the inference namespace. Poll
    `GET /v1/fine_tuning/jobs/{job_id}/deployment` for progress.
    """
    user_id = current_user["user_id"]
    correlation_id = getattr(request.state, "correlation_id", "unknown")

    try:
        _require_deployment_support()
        job_row = await _load_owned_job(job_id, user_id)

        if job_row["status"] != "succeeded":
            raise InvalidRequestError(
                f"Only a succeeded fine-tuning job can be deployed (this job is {job_row['status']})",
                param="job_id"
            )

        result_file_id = job_row.get("fine_tuned_model")
        if not result_file_id:
            raise InvalidRequestError(
                "This job has no result file to deploy yet", param="job_id"
            )

        config = settings.deployment
        namespace = config.namespace
        name = release_name_for_job(job_id)
        served_model_name = resolve_served_model_name(
            job_row["model"], job_row["created_at"], job_row.get("suffix")
        )

        # Published by the deploy; without it there is nothing to say what to
        # run, so say what is missing rather than failing later in KServe.
        try:
            profile = await _serving_profile()
        except ks.ServingProfileError as exc:
            raise ServiceUnavailableError(
                f"Model serving is not set up on this cluster: {exc}. Re-run the use case deploy."
            ) from exc

        # One model per job, so a running deployment is only replaced on
        # purpose; a failed one is the normal thing to retry.
        existing = await kube_client.get_llm_service(namespace, name)
        if existing:
            status = await _deployment_status(job_row)
            if status.phase != DeploymentPhase.FAILED:
                raise ConflictError(
                    "This model is already deployed or being deployed. Remove it first to deploy it again.",
                    code="already_deployed"
                )

        # Off unless an installation asks for it. A count cannot tell a 1B model
        # from a 70B, so it blocks cheap deployments that would fit and permits
        # expensive ones that will not; the CPU and memory check below does the
        # real admission control against what the nodes actually have.
        if not existing and config.max_deployments > 0:
            if await _deployments_in_use(namespace) >= config.max_deployments:
                raise ConflictError(
                    f"The maximum number of deployed models ({config.max_deployments}) has been "
                    f"reached. Remove a deployed model before deploying another one.",
                    code="deployment_limit_reached"
                )

        # Size the deployment and refuse one that cannot be placed. Without a
        # request the scheduler would admit the pod onto a full node and the
        # failure would surface much later, as an OOM kill or as vLLM failing to
        # allocate.
        resolved = await _resolve_deploy_request(job_row["model"], overrides)

        if existing:
            await kube_client.delete_llm_service(namespace, name)
            await _wait_until_gone(namespace, name)

        defaults = serving.from_profile(profile)
        kv_gib = resolved.get("kv_cache_space_gib")
        manifest = ks.build_manifest(
            name=name,
            namespace=namespace,
            job_id=job_id,
            served_model_name=served_model_name,
            result_file_id=result_file_id,
            cpu=str(resolved.get("cpu") or config.default_cpu_cores),
            memory=str(resolved.get("memory") or f"{config.default_memory_gib}Gi"),
            profile=profile,
            extra_args=serving.to_cli_args(resolved),
            extra_env={"VLLM_CPU_KVCACHE_SPACE": kv_gib} if kv_gib is not None
            else ({"VLLM_CPU_KVCACHE_SPACE": defaults["kv_cache_space_gib"]}
                  if defaults.get("kv_cache_space_gib") else None),
        )

        logger.info(
            "Deploying fine-tuned model",
            extra={
                "job_id": job_id,
                "cpu": resolved.get("cpu"),
                "memory": resolved.get("memory"),
                "capacity_override": resolved.get("forced", False),
                "correlation_id": correlation_id,
            },
        )

        try:
            await kube_client.create_llm_service(namespace, manifest)
        except KubeApiError as exc:
            if exc.status == 409:
                raise ConflictError(
                    "This model is already being deployed", code="deployment_in_progress"
                ) from exc
            raise

        logger.info(
            "Model deployment started",
            extra={
                "job_id": job_id,
                "user_id": user_id,
                "release_name": name,
                "served_model_name": served_model_name,
                "result_file_id": result_file_id,
                "correlation_id": correlation_id,
            }
        )

        return await _deployment_status(job_row)

    except (InvalidRequestError, ConflictError, ResourceNotFoundError,
            ForbiddenError, ServiceUnavailableError):
        raise
    except KubeApiError as exc:
        logger.error(
            f"Model deployment failed for job {job_id}: {exc.message}",
            extra={"job_id": job_id, "status": exc.status}
        )
        raise ServiceUnavailableError("Unable to start the deployment. Please try again.") from exc
    except Exception as exc:
        logger.error(f"Model deployment failed for job {job_id}: {exc}", exc_info=True)
        raise ServerError("Unable to start the model deployment. Please try again later.")


@router.get("/jobs/{job_id}/deployment", response_model=ModelDeploymentStatus)
@limiter.limit(f"{settings.rate_limit.job_read}/minute")
async def get_fine_tuned_model_deployment(
    request: Request,
    job_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Progress of serving a fine-tuned model.

    Derived from cluster state on every call — the LLMInferenceService, its
    pod's init containers and readiness, and whether the gateway lists it.

    No logs: this is polled while a deployment comes up, and tailing a container on
    every poll spends a round-trip on output nobody has asked for. ``/logs`` fetches
    them on demand.
    """
    try:
        job_row = await _load_owned_job(job_id, current_user["user_id"])
        return await _deployment_status(job_row)
    except (ResourceNotFoundError, ForbiddenError):
        raise
    except KubeApiError as exc:
        logger.warning(
            f"Deployment status unavailable for job {job_id}: {exc.message}",
            extra={"job_id": job_id, "status": exc.status}
        )
        raise ServiceUnavailableError(
            "Unable to read the deployment status from the cluster. Please try again."
        ) from exc
    except Exception as exc:
        logger.error(f"Get deployment status failed for job {job_id}: {exc}", exc_info=True)
        raise ServerError("Unable to retrieve the deployment status. Please try again later.")


@router.get("/jobs/{job_id}/deployment/logs", response_model=DeploymentLogs)
@limiter.limit(f"{settings.rate_limit.job_read}/minute")
async def get_fine_tuned_model_deployment_logs(
    request: Request,
    job_id: str,
    tail: int = Query(default=200, ge=10, le=2000, description="How many lines to return"),
    hide_probes: bool = Query(
        default=True,
        description="Drop successful health-check requests, which otherwise fill the whole tail",
    ),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    A tail of the log of whatever this deployment is currently doing.

    Separate from the status endpoint so it runs when a reader asks for it, not on
    every status poll. Which container is read follows the phase: the fetch init
    containers while downloading, vLLM while loading or serving, and whichever
    one broke on a failure.

    When probes are hidden a larger tail is read from the cluster and then trimmed,
    because filtering a 200-line tail of pure health checks would otherwise return
    nothing.
    """
    try:
        job_row = await _load_owned_job(job_id, current_user["user_id"])
        fetch = min(tail * 5, 5000) if hide_probes else tail
        status = await _deployment_status(job_row, include_logs=True, log_tail=fetch)

        lines, hidden = status.logs, 0
        if hide_probes:
            lines, hidden = _without_probe_lines(lines)
        lines = lines[-tail:]

        message = None
        if not lines and hidden:
            message = (
                f"The only output in the last {fetch} lines was {hidden} health checks, which is "
                f"what a healthy idle model looks like. Turn off 'hide health checks' to see them."
            )
        elif not lines and status.phase == DeploymentPhase.NOT_DEPLOYED:
            message = "This model has not been deployed, so there is nothing to read."
        elif not lines:
            message = "No output was reported by the container for this phase."

        return DeploymentLogs(
            job_id=job_id,
            logs=lines,
            log_source=status.log_source,
            phase=status.phase,
            tail=tail,
            hidden_lines=hidden,
            message=message,
        )
    except (ResourceNotFoundError, ForbiddenError):
        raise
    except KubeApiError as exc:
        logger.warning(
            f"Deployment logs unavailable for job {job_id}: {exc.message}",
            extra={"job_id": job_id, "status": exc.status},
        )
        raise ServiceUnavailableError(
            "Unable to read the deployment logs from the cluster. Please try again."
        ) from exc
    except Exception as exc:
        logger.error(f"Get deployment logs failed for job {job_id}: {exc}", exc_info=True)
        raise ServerError("Unable to retrieve the deployment logs. Please try again later.")



@router.delete("/jobs/{job_id}/deployment", response_model=ModelDeploymentStatus)
@limiter.limit(f"{settings.rate_limit.job_cancel}/minute")
async def undeploy_fine_tuned_model(
    request: Request,
    job_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Stop serving a fine-tuned model.

    Removes the model from the LiteLLM gateway, then deletes its
    LLMInferenceService. The adapter stays in the user's files.
    """
    user_id = current_user["user_id"]

    try:
        _require_deployment_support()
        job_row = await _load_owned_job(job_id, user_id)

        namespace = settings.deployment.namespace
        name = release_name_for_job(job_id)
        served_model_name = resolve_served_model_name(
            job_row["model"], job_row["created_at"], job_row.get("suffix")
        )

        service = await kube_client.get_llm_service(namespace, name)
        if not service:
            raise ConflictError("This model is not deployed", code="not_deployed")

        # A route naming a model that no longer exists would send matching queries
        # into a 404, so it goes with the deployment. Best-effort: failing to tidy
        # the router must not block removing the model.
        try:
            from .semantic_routes import remove_route_for_model

            if await remove_route_for_model(served_model_name):
                logger.info("Removed the semantic route for an undeployed model", extra={"job_id": job_id})
        except Exception as exc:
            logger.warning(f"Could not remove the semantic route for {job_id}: {exc}")

        # Out of the gateway first, so nothing is sent to a model that is going.
        try:
            await gateway_client.deregister_model(served_model_name)
        except GatewayError as exc:
            raise ServiceUnavailableError(
                f"Could not remove the model from the LiteLLM gateway ({exc.message}); it is still "
                f"deployed. Please try again."
            ) from exc

        await kube_client.delete_llm_service(namespace, name)

        logger.info(
            "Model deployment removal started",
            extra={"job_id": job_id, "user_id": user_id, "release_name": name}
        )

        return await _deployment_status(job_row)

    except (ConflictError, ResourceNotFoundError, ForbiddenError, ServiceUnavailableError):
        raise
    except KubeApiError as exc:
        logger.error(
            f"Model removal failed for job {job_id}: {exc.message}",
            extra={"job_id": job_id, "status": exc.status}
        )
        raise ServiceUnavailableError("Unable to remove the deployment. Please try again.") from exc
    except Exception as exc:
        logger.error(f"Model removal failed for job {job_id}: {exc}", exc_info=True)
        raise ServerError("Unable to remove the model deployment. Please try again later.")
