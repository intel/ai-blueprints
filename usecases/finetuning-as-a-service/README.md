# FineTuning-as-a-Service

Fine-tune open models on your own data and serve the result next to the models
the platform already runs. Upload a dataset (or import traces
from Langfuse), train a LoRA adapter on the GPU nodes (only the adapter comes
back), and deploy it on its base model behind the same LiteLLM gateway
with one click, optionally with a semantic route that sends matching prompts to
it.

Training runs on a **separate GPU cluster**, and everything else, including
serving the fine-tuned models, on the platform's Xeon nodes: validated
with Toolkit Manager on a Xeon node and training on a B300 cluster (see
[docs/validation.md](docs/validation.md)). GPU nodes in the same cluster can be
used instead.

It installs **on top of** a platform installed by [Toolkit Manager](https://github.com/intel/enterprise-ai-solutions)
with its `inference` layer, Intel® Inference Toolkit (Keycloak, LiteLLM and KServe). The platform
is installed and upgraded on its own; nothing in it needs to change.

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Operations](#operations)
- [Local Development](#local-development)
- [Troubleshooting](#troubleshooting)

## Overview

```
 ┌───────── One Kubernetes cluster installed by Toolkit Manager (Xeon + GPU) ──────────┐
 │                                                                                     │
 │  Envoy Gateway ─┬─ https://litellm.<domain>/               → LiteLLM (models)       │
 │                 ├─ https://keycloak.<domain>/              → Keycloak, realm platform│
 │                 └─ https://<domain>/finetuning-as-a-service/* (gateway sign-in)     │
 │                      ├── .../ui                            → Fine-Tuning UI         │
 │                      ├── .../api, .../v1/fine_tuning, .../v1/models                 │
 │                      │                                     → Fine-Tuning API ──┐    │
 │                      └── .../v1/files, .../dataprep        → Data Prep ◄───┐   │    │
 │                                                                            │   │    │
 │  Xeon nodes   Data Prep: backend, Celery worker, SeaweedFS (S3), Postgres, │   │    │
 │               Redis                                                        │   │    │
 │               Fine-Tuning Engine (no GPU) ◄── OAuth2 client credentials ───┼───┘    │
 │                 │  Deploy Model → KServe + vLLM in "finetuning-models"     │        │
 │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄│┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄│┄┄┄┄┄┄┄┄│
 │  GPU nodes      ▼ one Job per training run (Unsloth), namespace "ftaas" ───┘ dataset │
 │                   in, LoRA adapter out, through Data Prep's Service                 │
 └─────────────────────────────────────────────────────────────────────────────────────┘
```

With a **separate GPU cluster** the engine creates the Jobs there with a
kubeconfig, and the training pod comes back to the Files API through this
cluster's ingress (`https://<domain>/finetuning-as-a-service/v1/files`).

| Namespace | Release(s) |
|---|---|
| `dataprep` | `data-prep-backend`, `dataprep-minio` (SeaweedFS S3 store; the release keeps its MinIO name), `dataprep-postgres`, `dataprep-redis`, plus the Celery worker |
| `finetuning-engine` | `finetuning-engine`, `finetuning-engine-postgresql` |
| `finetuning-api` | `finetuning-service`, `finetuning-service-postgresql`, `finetuning-gateway-routes` (its routes and sign-in on the platform gateway) |
| `finetuning-ui` | `finetuning-ui` |
| `finetuning-models` | KServe `LLMInferenceService`s: `finetuning-embedding` (encoder for semantic routing) and every model deployed from the UI; the model store PVC |
| `keycloak` | shared. Installed by Toolkit Manager; this use case adds two clients and the group `finetuning-users` to its `platform` realm |
| `ftaas` | training Jobs and their shared work volume; on a separate GPU cluster, there instead |

Every namespace above except `keycloak` joins the platform's Istio ambient mesh
(`istio.io/dataplane-mode=ambient`): with its mesh-wide STRICT mTLS, Keycloak,
LiteLLM and Langfuse refuse pods outside it.

Every page and API is behind the platform gateway's sign-in (Envoy Gateway
OIDC) against the `platform` realm, the same login as every other platform UI.
Models are served by the platform's KServe. The use case adds no gateway,
model server or identity provider of its own.

| Document | For |
|---|---|
| [docs/user-guide.md](docs/user-guide.md) | people using the portal: datasets, training, deploying, routing, chatting |
| [docs/deployment.md](docs/deployment.md) | whoever installs it: platform checks, GPU cluster, configuration, troubleshooting |
| [docs/implementation.md](docs/implementation.md) | maintainers: how the pieces fit and why |
| [docs/validation.md](docs/validation.md) | reviewers: the end-to-end checks and their last results |

## Prerequisites

- **Toolkit Manager** from its `main` branch, with the `inference` layer
  (Intel® Inference Toolkit) installed and healthy, with LiteLLM turned on: `auth_type: "litellm_vkey"` in
  `env/<name>/config.inference.yaml` (Keycloak is always deployed). That gives
  Keycloak in namespace `keycloak`, LiteLLM and Langfuse in `litellm` and
  `langfuse`, Envoy Gateway `envoy-gateway-system/eg-gateway` serving the base
  domain over TLS, and a default `local-path` storage class.
  - The shared `platform` realm people sign in with came with
    [intel/enterprise-ai-solutions#61](https://github.com/intel/enterprise-ai-solutions/pull/61)
    and [intel/enterprise-inference#39](https://github.com/intel/enterprise-inference/pull/39),
    merged after Toolkit Manager release-1.2 (Inference Toolkit release-1.1).
    Those releases do not have the realm, so install from `main` until the
    next release.
  - The `agent-toolkit` layer (Intel® Agent Toolkit) is not needed.
  - LiteLLM must admit this use case's in-cluster callers: add `finetuning-api`
    and `dataprep` to `litellm_allowed_namespaces` in
    `env/<name>/config.inference.yaml` (keeping `envoy-gateway-system` and
    `litellm`) and re-run `./toolkit_manager.sh install litellm --env <name>`.
    The deploy stops and says so if they are missing.
  - The gateway must use the default **subdomain** routing mode. Path mode
    (`routing_mode: path`) is not supported yet; the deploy stops if it finds
    it.
  - Tested on Kubernetes (Kubespray); not on OpenShift.
- **GPUs for training**, one of:
  - **GPU nodes in this cluster:** nodes with the NVIDIA GPU Operator
    installed (it labels them `nvidia.com/gpu.present=true`), and a
    ReadWriteMany storage class (for example `ceph-filesystem`). Nothing else
    to prepare: the deploy finds the GPU nodes and sets up the training
    namespace and rights.
  - **A separate GPU cluster:** a kubeconfig for it, a namespace with a GPU
    quota and a ReadWriteMany storage class there, and a network path from it to
    this cluster's ingress on 443. Its administrator's checklist, including the
    exact RBAC, is in [docs/deployment.md](docs/deployment.md#2b-separate-gpu-cluster).
- **The in-cluster image registry** (`registry.kube-system.svc.cluster.local:5000`,
  deployed by the platform's Kubespray install), or another registry the cluster
  can pull from (`finetune_registry`).
- On the machine you deploy from: `kubectl` and `helm` with admin access to the
  cluster, and `python3` with `venv`. Ansible is installed into `./.venv` on
  first run.
- About 10 CPU cores and 24 GiB of memory free for the services, plus whatever
  the models you deploy need (sized from the parameter count, e.g. 12 cores /
  57 GiB for a 3B model).

## Quick Start

```bash
cd usecases/finetuning-as-a-service
cp finetune-config.cfg.example finetune-config.cfg
```

With **GPU nodes in this cluster** there is nothing to fill in.

With a **separate GPU cluster**, also fill in its block:

```yaml
finetune_train_kubeconfig: "/home/me/.kube/gpu-cluster.yaml"   # mode 0600
finetune_files_api_ip: "10.0.0.10"          # this cluster's ingress, as the GPU cluster reaches it
finetune_train_https_proxy: ""              # if GPU nodes reach Hugging Face through a proxy
finetune_train_no_proxy: "api.example.com,10.0.0.10"
```

The deploy's proxy comes from your shell (`HTTP_PROXY` / `HTTPS_PROXY` /
`NO_PROXY`); export it first if this site needs one.

Then deploy (20–40 minutes the first time, most of it image builds):

```bash
./deploy.sh
```

Or from the repository root: `./deploy-usecase.sh finetuning-as-a-service`.

### Access the UI

Open `https://<cluster host>/finetuning-as-a-service/ui`, where the cluster host is
the platform's base domain (`base_domain_name`, e.g. `api.example.com`). Signing in
redirects to Keycloak on its own host, `https://keycloak.<cluster host>`.

1. **Name resolution.** Unless the base domain is in DNS, point both hosts at the
   platform gateway in the hosts file of the machine you browse from
   (`/etc/hosts`, or `C:\Windows\System32\drivers\etc\hosts`):

   ```bash
   # The gateway address to use:
   kubectl -n envoy-gateway-system get svc \
     -o jsonpath='{.items[?(@.spec.type=="LoadBalancer")].status.loadBalancer.ingress[0].ip}'; echo
   ```

   ```
   <gateway address>  api.example.com
   <gateway address>  keycloak.api.example.com
   ```

2. **Proxy.** If the browser uses a proxy, add the base domain and its
   subdomains (`api.example.com`, `.api.example.com`) to its bypass list.
3. **Certificate.** With `gateway_tls_mode: selfsigned` the browser warns on both
   hosts; accept it for each (or trust the platform CA).
4. **Sign in** with your platform account (Keycloak realm `platform`, the same
   login as Headlamp, Grafana, Langfuse and LiteLLM). The portal admits members
   of the groups `platform-admins` and `finetuning-users`; a platform admin adds
   people to `finetuning-users` in the Keycloak admin console. On a fresh
   platform the first account is `platform-admin`, with a temporary password
   changed at first sign-in:

   ```bash
   kubectl -n keycloak get secret platform-bootstrap-admin -o jsonpath='{.data.value}' | base64 -d; echo
   ```

Users, groups, password changes and AD/LDAP federation are managed in the
Keycloak admin console, `https://keycloak.<cluster host>/admin/`, realm
`platform`. Its admin login is in the platform's Secret:

```bash
kubectl -n keycloak get secret keycloak-admin-secret -o jsonpath='{.data.username}' | base64 -d; echo
kubectl -n keycloak get secret keycloak-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
```

## Configuration

`finetune-config.cfg` (YAML, gitignored) holds only what cannot be discovered.
The template documents every key. In short:

| Group | Keys | Default when blank |
|---|---|---|
| Discovery overrides | `cluster_url`, `finetune_registry`, `langfuse_org_keys` | read from the cluster; in-cluster registry; none |
| Engine connection | `nvidia_finetune_backend_url`, `nvidia_keycloak_token_url`, `nvidia_keycloak_client_id`, `nvidia_keycloak_client_secret`, `nvidia_keycloak_verify_ssl` | the bundled engine and the shared Keycloak; secret generated |
| Training | `finetune_engine_enabled`, `finetune_train_mode`, `finetune_train_namespace`, `finetune_train_pvc_storage_class`, `finetune_train_*_proxy` (`files_job_token_secret` is generated) | `auto`: the GPU nodes of this cluster; namespace `ftaas`; `ceph-filesystem`; the shell's proxy |
| Separate GPU cluster | `finetune_train_kubeconfig`, `finetune_files_api_ip`, `finetune_files_api_tls_verify` | not used; **must be set** to train on another cluster |
| Sign-in | `keycloak_realm`, `finetune_users_group`, `finetune_allowed_groups`, `oidc_client_id`, `oidc_client_secret`, `keycloak_public_url` | realm `platform`; groups `platform-admins` and `finetuning-users`; secret generated |

Sizing, images, ports, namespaces, the URL prefix (`finetune_url_prefix`,
default `/finetuning-as-a-service`) and the serving profile (`finetune_serving_*`,
`finetune_model_store_*`) are in `vars/finetune-plugin-vars.yml` and normally
need no change.

**What is read from the platform, at deploy time:** the cluster host (the
`https-apex` listener of Gateway `envoy-gateway-system/eg-gateway`), the LiteLLM
master key (Secret `litellm/litellm-master-key`, or the `LITELLM_MASTER_KEY`
environment variable), the Langfuse project keys (Secret
`litellm/langfuse-api-keys`), the Keycloak issuer (the `keycloak/keycloak`
Keycloak resource), the Hugging Face token (Secret
`finetuning-models/hugging-face-token`, or `HF_TOKEN`), and the proxy
(`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` in your shell; the cluster's own
names are added to `NO_PROXY` for the pods). TLS is terminated by the platform
gateway, so no certificate is copied.

**Secrets** left blank are generated on the first install and stored in the
Secret `finetuning-api/finetuning-as-a-service-secrets`; every later run reuses
them, so re-running never rotates a database password.

## API Reference

All under `https://<cluster host>/finetuning-as-a-service`, with a browser session from
the gateway's sign-in:

| Path | Service | Docs |
|---|---|---|
| `/ui` | Web UI | — |
| `/api/...`, `/v1/fine_tuning/jobs`, `/v1/models` | Fine-Tuning API (OpenAI-compatible jobs) | `/finetuning-as-a-service/api/docs` |
| `/v1/files`, `/v1/dataprep/...`, `/v1/langfuse/...` | Data Prep (datasets, Q&A generation, trace import) | `/finetuning-as-a-service/dataprep/docs` |

Deployed fine-tuned models are served by LiteLLM like any other model:
`POST https://litellm.<domain>/v1/chat/completions` with a LiteLLM key.

## Operations

| Task | Command |
|---|---|
| Redeploy one component | `./deploy.sh --tags ui` (also `api`, `engine`, `dataprep`, `embedding`, `keycloak`, `routes`) |
| Rebuild images and roll them out | `./deploy.sh --tags build,api,ui,dataprep,engine` |
| Check config and playbooks only | `./deploy.sh --dry-run` |
| Remove the releases, keep the data | `./deploy.sh --uninstall` |
| Status | `kubectl get pods -n dataprep -n finetuning-api -n finetuning-engine -n finetuning-ui`; models: `kubectl get llminferenceservice -n finetuning-models` |
| Logs | `kubectl logs -n finetuning-api deploy/finetuning-service -f` |

`--uninstall` keeps the PersistentVolumeClaims (they carry
`helm.sh/resource-policy: keep`, and the model store), the namespaces, the
state Secret, the models deployed from the UI and the clients and group in the
`platform` realm. Delete those by hand to start over.

**Rotating a secret:** delete its key from the state Secret, then update the
component that holds the old value (for a database, `ALTER USER` first) and
re-run. For the OIDC client secret, re-run with `--tags keycloak,routes`.

## Local Development

Each component runs on its own for development; see
[src/api/README.md](src/api/README.md), [src/dataprep/README.md](src/dataprep/README.md)
and [src/ui/README.md](src/ui/README.md). Images are built in-cluster with
BuildKit (`src/*/buildkit/`), so a laptop needs no Docker daemon to ship a
change: `./deploy.sh --tags build,<component>`.

## Troubleshooting

**`Namespace litellm not found`** — Toolkit Manager's `inference` layer is not installed,
LiteLLM is off (set `auth_type: "litellm_vkey"`, or `litellm_enabled: true`, in
`env/<name>/config.inference.yaml` and re-run its install), or `kubectl` points at
another cluster.

**`Could not read master_key from Secret litellm/litellm-master-key`** — LiteLLM
keeps its key elsewhere; `export LITELLM_MASTER_KEY=...` and re-run.

**Pods log `Connection reset by peer` to a service that is up** — the namespace is
outside the Istio ambient mesh, or a NetworkPolicy there does not allow port
15008 (ztunnel) and the probe source `169.254.7.127/32`.

**No GPU nodes to train on** (the deploy stops) — this cluster has no node
labelled `nvidia.com/gpu.present=true` and no `finetune_train_kubeconfig` is
set. Deploy on a cluster with GPU nodes, point
`finetune_train_kubeconfig` at a separate GPU cluster, or set
`finetune_engine_enabled: false`.

**Jobs fail with `Failed to download file-...` or `Upload failed`** — with a
separate GPU cluster, the training pod reaches the Files API through this
cluster's ingress. Check, in order: `finetune_files_api_ip` is the ingress address *today* (it moves when a
single-node cluster is rebuilt); and `finetune_train_no_proxy` covers the host
and that address — sending the upload through a proxy gets a 403 after training
has finished. A 401 is a job token Data Prep rejected (expired, or a mismatched
`files_job_token_secret`); a 403 is a request outside the job's own files. A 404
means the token was accepted but the file belongs to another user.

**`403 RBAC: access denied` after signing in** — the account is in neither
`platform-admins` nor `finetuning-users` (realm `platform`). Add it to
`finetuning-users` in the Keycloak admin console, then sign out and in again.

**`OAuth flow failed` right after login** — a second tab or the Back button
raced the login, or the client secret changed. Open
`/finetuning-as-a-service/ui` afresh; after rotating the secret, re-run with
`--tags keycloak,routes`.

**A model deployed from the UI is OOM-killed or stuck `Pending`** — the requested
size did not fit. The API sizes deployments from the parameter count plus the
KV cache and refuses with 409 when no node has room; a `Pending` pod means
another workload took the space in between.

**Image builds fail** — `kubectl get jobs -A | grep buildkit`, then
`kubectl logs -n <namespace> job/<name>`. The jobs need the proxy from your shell
and the registry to be reachable.

**`/finetuning-as-a-service/*` returns 404 or 500** — check the routes and their
policies: `kubectl get httproute,securitypolicy -n finetuning-api` (each should
be `Accepted`), and the `ReferenceGrant`s in `dataprep` and `finetuning-ui`.

More, including slow or cut-off chat replies, LiteLLM restarts after a route
change and Langfuse import errors, in [docs/deployment.md](docs/deployment.md#troubleshooting).
