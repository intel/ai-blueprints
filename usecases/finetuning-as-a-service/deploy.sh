#!/usr/bin/env bash
# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# deploy.sh — FineTuning-as-a-Service, on top of a platform installed by
# Toolkit Manager (Intel® Inference Toolkit: Keycloak, LiteLLM and Langfuse).
#
#   ./deploy.sh                      install or upgrade everything
#   ./deploy.sh --tags ui            redeploy one component (see below)
#   ./deploy.sh --tags build,api     rebuild the images, then redeploy the API
#   ./deploy.sh --dry-run            check the config and the playbooks, change nothing
#   ./deploy.sh --uninstall          remove the Helm releases (data is kept)
#
# Options:
#   --config FILE        settings file (default: ./finetune-config.cfg)
#   --tags LIST          only these components: build, keycloak, dataprep,
#                        embedding, engine, api, ui, routes
#   --skip-tags LIST     everything except these
#   -K, --ask-become-pass  prompt for sudo (needed once, to let containerd pull
#                        from the in-cluster registry, when sudo needs a password)
#   --mode kubernetes    accepted for deploy-usecase.sh; docker is not supported
#   -v                   more Ansible output (-vv, -vvv also work)
#
# Everything the platform provides — the cluster domain, the gateway master key,
# Langfuse keys, Keycloak — is read from the cluster, so this needs
# only kubectl and helm access to it. Passwords are generated on first install
# and kept in the Secret finetuning-api/finetuning-as-a-service-secrets.
#
# Ansible runs from a virtualenv in ./.venv, created on first use (it needs
# network access, through the proxy in the environment if there is one). Set
# ANSIBLE_PLAYBOOK to an existing ansible-playbook to skip that; its Python
# then needs the kubernetes package and the kubernetes.core collection.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${FTAAS_VENV:-${SCRIPT_DIR}/.venv}"
CONFIG_FILE="${SCRIPT_DIR}/finetune-config.cfg"
TAGS=""
SKIP_TAGS=""
DRY_RUN=false
UNINSTALL=false
EXTRA_ARGS=()

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "==> $*"; }
usage() { sed -n '5,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="$(realpath "${2:?--config needs a file}")"; shift ;;
    --tags) TAGS="${2:?--tags needs a list}"; shift ;;
    --skip-tags) SKIP_TAGS="${2:?--skip-tags needs a list}"; shift ;;
    --dry-run) DRY_RUN=true ;;
    --uninstall) UNINSTALL=true ;;
    -K|--ask-become-pass) EXTRA_ARGS+=("--ask-become-pass") ;;
    -v|-vv|-vvv|-vvvv) EXTRA_ARGS+=("$1") ;;
    --mode)
      [[ "${2:-}" == "kubernetes" ]] || die "only --mode kubernetes is supported: the engine trains on a GPU cluster"
      shift ;;
    --namespace)
      echo "NOTE: --namespace ignored; components use fixed namespaces (see vars/finetune-plugin-vars.yml)"
      shift ;;
    -h|--help) usage ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

for cmd in kubectl helm python3; do
  command -v "${cmd}" >/dev/null 2>&1 || die "${cmd} is required"
done
kubectl version >/dev/null 2>&1 || die "kubectl cannot reach the cluster (check KUBECONFIG)"

# ── Uninstall ─────────────────────────────────────────────────────────────────
# Releases only. PersistentVolumeClaims, namespaces, the state Secret and the
# clients in the shared Keycloak are left alone, so a later install picks the data
# and the passwords back up. Delete those by hand to start from scratch.
if [[ "${UNINSTALL}" == "true" ]]; then
  releases=(
    "finetuning-api:finetuning-gateway-routes"
    "finetuning-ui:finetuning-ui"
    "finetuning-api:finetuning-service"
    "finetuning-api:finetuning-service-postgresql"
    "finetuning-engine:finetuning-engine"
    "finetuning-engine:finetuning-engine-postgresql"
    "dataprep:data-prep-backend"
    "dataprep:dataprep-redis"
    "dataprep:dataprep-postgres"
    "dataprep:dataprep-minio"
  )
  log "Removing FineTuning-as-a-Service releases (data is kept)"
  for entry in "${releases[@]}"; do
    ns="${entry%%:*}"; rel="${entry#*:}"
    if helm status "${rel}" -n "${ns}" >/dev/null 2>&1; then
      helm uninstall "${rel}" -n "${ns}" --wait || echo "WARNING: could not remove ${ns}/${rel}"
    fi
  done
  kubectl -n dataprep delete deployment dataprep-celery-worker --ignore-not-found
  # The semantic router's encoder (models deployed from the UI are kept).
  kubectl -n finetuning-models delete llminferenceservice finetuning-embedding --ignore-not-found
  # Cluster-scoped rights of the engine for local training (GPU nodes in this cluster).
  kubectl delete clusterrolebinding,clusterrole finetuning-engine-nodes --ignore-not-found
  cat <<'EOF'

Releases removed. Kept: the PVCs and namespaces (dataprep, finetuning-api,
finetuning-engine, finetuning-ui), the state Secret
finetuning-api/finetuning-as-a-service-secrets, models deployed from the UI,
and the clients and group in the shared Keycloak's platform realm.
EOF
  exit 0
fi

[[ -f "${CONFIG_FILE}" ]] \
  || die "${CONFIG_FILE} not found — copy finetune-config.cfg.example to finetune-config.cfg and fill it in"

# ── Ansible ───────────────────────────────────────────────────────────────────
if [[ -n "${ANSIBLE_PLAYBOOK:-}" ]]; then
  PLAYBOOK_BIN="${ANSIBLE_PLAYBOOK}"
  PYTHON_BIN="${ANSIBLE_PYTHON:-python3}"
else
  PLAYBOOK_BIN="${VENV_DIR}/bin/ansible-playbook"
  PYTHON_BIN="${VENV_DIR}/bin/python"
  if [[ ! -x "${PLAYBOOK_BIN}" ]]; then
    log "Setting up Ansible in ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}" || die "python3 -m venv failed (install python3-venv)"
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet -r "${SCRIPT_DIR}/playbooks/requirements.txt"
    "${VENV_DIR}/bin/ansible-galaxy" collection install --upgrade -r "${SCRIPT_DIR}/playbooks/requirements.yml" >/dev/null
  fi
fi
"${PYTHON_BIN}" -c 'import kubernetes' 2>/dev/null \
  || die "${PYTHON_BIN} has no kubernetes package (pip install kubernetes)"

# The trailing comma makes "localhost," an inline inventory, not a file name.
# shellcheck disable=SC2054
args=(
  -i localhost,
  -c local
  -e "ansible_python_interpreter=${PYTHON_BIN}"
  -e "finetune_config_file=${CONFIG_FILE}"
)
[[ -n "${TAGS}" ]] && args+=(--tags "${TAGS}")
[[ -n "${SKIP_TAGS}" ]] && args+=(--skip-tags "${SKIP_TAGS}")

# Ansible refuses to run on non-blocking stdio, which some CI runners and IDE
# terminals hand it; reading from /dev/null avoids that unless sudo must prompt.
stdin_redirect=/dev/stdin
[[ " ${EXTRA_ARGS[*]:-} " == *" --ask-become-pass "* ]] || stdin_redirect=/dev/null

if [[ "${DRY_RUN}" == "true" ]]; then
  log "Checking the playbooks"
  "${PLAYBOOK_BIN}" "${args[@]}" --syntax-check "${SCRIPT_DIR}/playbooks/deploy-all.yml" < /dev/null
  "${PYTHON_BIN}" -c 'import sys, yaml; yaml.safe_load(open(sys.argv[1]))' "${CONFIG_FILE}" \
    || die "${CONFIG_FILE} is not valid YAML"
  kubectl get namespace litellm >/dev/null 2>&1 \
    || die "namespace litellm not found — install the Toolkit Manager inference layer with LiteLLM first (auth_type: litellm_vkey)"
  kubectl -n keycloak get statefulset keycloak >/dev/null 2>&1 \
    || die "Keycloak not found in namespace keycloak — install the platform with Toolkit Manager first"
  log "Config and playbooks are valid, and the platform is installed. Nothing was changed."
  exit 0
fi

log "Deploying FineTuning-as-a-Service"
ANSIBLE_FORCE_COLOR="${ANSIBLE_FORCE_COLOR:-1}" \
  "${PLAYBOOK_BIN}" "${args[@]}" "${EXTRA_ARGS[@]}" "${SCRIPT_DIR}/playbooks/deploy-all.yml" < "${stdin_redirect}"
