# FineTuning-as-a-Service — Implementation

How the components fit together, and the decisions behind the less obvious
parts. For installation see [deployment.md](deployment.md).

## Components

| Component | Source | Chart | Role |
|---|---|---|---|
| Fine-Tuning UI | `src/ui` (Next.js) | `src/ui/deployment/helmcharts/finetuning-ui-chart` | Datasets, jobs, deployments, semantic routes |
| Fine-Tuning API | `src/api` (FastAPI) | `src/api/helm-charts/finetuning-api` | OpenAI-compatible fine-tuning jobs; model deployment; semantic routing |
| Fine-Tuning Engine | `src/engine` (FastAPI + trainer) | `src/engine/helm-charts/finetuning-engine` | Turns each job into a Kubernetes Job on the GPU cluster (Unsloth) |
| Data Prep | `src/dataprep` (FastAPI + Celery) | `src/dataprep/helmcharts/*` | Files API, Q&A generation, Langfuse trace import, S3 storage (SeaweedFS) |
| Routes | — | `helm-charts/gateway-routes` | Its routes on the platform's Envoy Gateway, sign-in included |
| Serving | — | Intel® Inference Toolkit (KServe) | Fine-tuned models and the router's encoder, as `LLMInferenceService`s |
| Keycloak | — | Toolkit Manager (`keycloak` role) | Sign-in through its `platform` realm; this use case adds two clients and a group |

Deployment is Ansible (`playbooks/`) driving Helm and `kubectl`, run locally by `deploy.sh`
against the current kubeconfig. Each component has its own playbook and tag.

## Relationship to Toolkit Manager

The use case consumes the platform and never modifies its configuration.
Everything it needs is read from the running cluster by
`playbooks/tasks/discover-toolkit.yml` and `playbooks/deploy-all.yml`:

| Needed | Read from |
|---|---|
| Cluster host | `https-apex` listener of Gateway `envoy-gateway-system/eg-gateway` |
| LiteLLM master key | Secret `litellm/litellm-master-key` |
| Langfuse project keys | Secret `litellm/langfuse-api-keys` |
| Keycloak issuer | `spec.hostname` of the Keycloak resource `keycloak/keycloak`, realm `platform` |
| Hugging Face token | Secret `finetuning-models/hugging-face-token` |
| Image registry | `registry.kube-system.svc.cluster.local:5000` (Kubespray) |

The platform shapes three things here:

- **Envoy Gateway, deny by default.** Every route without its own
  `SecurityPolicy` needs a Keycloak JWT from the platform's `inference` realm.
  This use case's routes carry their own policies (below).
- **One sign-in for every platform UI.** People sign in through Keycloak's
  `platform` realm ([intel/enterprise-ai-solutions#61](https://github.com/intel/enterprise-ai-solutions/pull/61)),
  the way Headlamp, Grafana, Langfuse and LiteLLM do, so one login covers all
  of them. The gateway runs the login for this use case, as the platform does
  for Headlamp (`roles/gateway` `route_sso`).
- **Models are served by KServe.** Fine-tuned models run on the platform's
  KServe and its vLLM CPU image, with the platform's CPU placement (NRI
  balloons), next to the models Model Manager serves.
- **Istio ambient with STRICT mTLS.** Every namespace of this use case joins the
  mesh, and a NetworkPolicy must allow ztunnel's port 15008 and the probe source
  `169.254.7.127/32` next to the application port (`networkPolicy.ambientMesh`
  in the API chart).
- **One LiteLLM for the cluster.** Applying a semantic-routing change restarts
  the platform's `litellm/litellm` Deployment (see [Semantic routing](#semantic-routing)).

Nothing of the platform's is duplicated: no edge gateway, model server or
identity provider of its own (no extra component for OSPDT/BOM).

## Request path and authentication

The platform's Envoy Gateway owns ports 80/443 and terminates TLS. The chart
`helm-charts/gateway-routes` (release `finetuning-gateway-routes` in
`finetuning-api`) puts three HTTPRoutes on its `https-apex` listener; LiteLLM
is on its own host (`litellm.<domain>`), so nothing here can interfere with
model traffic.

| Route | Paths | Policy |
|---|---|---|
| `finetuning-as-a-service` | `/ui`, `/v1/fine_tuning`, `/v1/models`, `/v1/files`, `/v1/dataprep`, `/v1/langfuse` | sign-in (below) |
| `finetuning-as-a-service-jobs` | `GET`/`POST .../v1/files` with `Authorization: Bearer ftjob_…` | none: Data Prep checks the job token |
| `finetuning-as-a-service-public` | `/api/{health,liveness,readiness,docs,redoc,openapi.json}`, `/dataprep/{health,docs,redoc,openapi.json}` | none |

All paths are under `/finetuning-as-a-service` and rewritten to what each
service expects; the backends are in their own namespaces, reached through a
`ReferenceGrant` in each. Every rule removes `X-User`, `X-Userinfo` and
`X-Real-IP`; the job and public routes also remove `X-Forwarded-User`.

**One route, one policy, one session.** The UI, the API and Data Prep share one
HTTPRoute so they share one `SecurityPolicy` and its cookies: Envoy names the
session cookies per policy, so separate policies would sign the user in once
per service, and the UI's calls to the API would be sent to Keycloak instead.
The policy is the platform's SSO shape:

1. `oidc` against the `platform` realm: browsers are sent to Keycloak, tokens
   are exchanged in-cluster, kept in cookies (`finetuning-as-a-service-id`,
   `-at`) and refreshed, PKCE included. The callback and logout paths are under
   the UI (`/ui/oauth2/callback`, `/ui/logout`, which the UI's sign-out already
   uses), so they hit this route.
2. `denyRedirect` for `Sec-Fetch-Mode: cors`: the UI's background calls get a
   401 when the session has ended, rather than a redirect fetch() cannot follow.
3. `jwt` on the ID-token cookie, audience the client, with
   `claimToHeaders: preferred_username → X-Forwarded-User`. Envoy **replaces**
   any `X-Forwarded-User` a client sent (verified: a forged header comes out as
   the signed-in user, or the request is refused), and the API and Data Prep
   trust only that header.
4. `authorization`: deny by default, allow the `groups` claim values in
   `finetune_allowed_groups` (`platform-admins` and `finetuning-users`).

The UI turns `X-Forwarded-User` into its NextAuth session (provider
`oidc-proxy`); it has no password prompt. Sign-out goes to `/ui/logout`, where
Envoy clears its cookies and sends the browser to Keycloak's `end_session`.

**The training pod has no browser session.** It is handed only file IDs, so it
downloads its dataset and uploads the trained model through the Files API.
When a job is submitted, the API signs a per-job token (`ftjob_<JWT>`, HS256
with `files_job_token_secret`, which only the API and Data Prep hold) for the
user it verified at login, naming the job's training and validation files and
allowing the model upload. The engine hands it to the pod as `ft-api-key`. The
job route matches only that bearer prefix (a header match outranks the
signed-in route on the same path; Envoy matches the whole header value, hence
`^Bearer ftjob_.+$`), and Data Prep checks the
signature, the expiry and that the request is within the token's scope, so a
leaked token reads nothing else and is useless once it expires. The source
address is not trusted: the GPU cluster is shared, and Envoy does not pass the
client address on in a form a caller cannot forge.

## Secrets

`playbooks/tasks/resolve-secrets.yml` owns every password and shared secret.
Each is taken from, in order: the config file, the state Secret
`finetuning-api/finetuning-as-a-service-secrets`, or a new random value. The result is written back to the
state Secret. Postgres reads its credentials only when it first initialises a
volume, so a rotated value would lock the service out of its own data. (The S3
store takes its access key from a Secret on every start, but Data Prep and the
model init containers would still hold the old one.) That is why nothing is ever
regenerated once stored.

Secrets are never logged (`no_log`) and are fed to in-pod commands on stdin.
The clients and the group in the `platform` realm are reconciled through the
admin API using the admin credentials already in the Keycloak pod's
environment, so they never pass through Ansible. The browser client's secret is
also put, as key `client-secret`, into the Secret Envoy reads
(`finetuning-api/finetuning-as-a-service-sso`).

## Training

The engine is a plain HTTP service with no GPU. Each job becomes one Job on a
GPU node; all of them share one ReadWriteMany work volume for the base-model
cache, datasets and checkpoints. Where those nodes are is the training mode
(`finetune_train_mode`, resolved in `playbooks/deploy-finetuning-engine.yml`):

| | Local (GPU nodes in this cluster) | Remote (separate GPU cluster) |
|---|---|---|
| Chosen when | no kubeconfig, and nodes labelled `nvidia.com/gpu.present=true` exist | `finetune_train_kubeconfig` is set |
| Engine credentials | its own ServiceAccount; Role in the training namespace and a nodes ClusterRole, created by the deploy | the kubeconfig, from a Secret; RBAC granted by that cluster's administrator |
| Files API for the training pod | `http://data-prep-backend.dataprep.svc.cluster.local:8000` | `https://<host>/finetuning-as-a-service`, the host pinned to `finetune_files_api_ip` |
| Proxy for the training pod | the deploy shell's, unless `finetune_train_*_proxy` is set | `finetune_train_*_proxy` |

With neither a kubeconfig nor GPU nodes the deploy stops (`auto`); `local`
forces this cluster anyway, for a cluster whose GPU nodes have not joined yet.
The job token works the same in both modes: Data Prep verifies it whether the
request came through the gateway or straight to its Service. `status` stays coarse
(`PENDING/RUNNING/COMPLETED/FAILED/CANCELLED`); progress is in `current_phase`
(`downloading_data → preparing_environment → training → [merging →]
uploading_model`, null once terminal; `merging` only with
`DEFAULT_SAVE_METHOD=merged_16bit`).

A job hands back the **LoRA adapter only** by default (tens to hundreds of
MB): the GPU cluster neither merges nor uploads multi-GB weights. The trainer
points the adapter's `base_model_name_or_path` at the full-precision
checkpoint (dropping Unsloth's `-bnb-4bit` variants, which vLLM on CPU cannot
load), and serving downloads that base model and loads the adapter on top;
LiteLLM serves it under the adapter's name. Base models must therefore be
public on Hugging Face (no token is passed to model deployments); Unsloth's
mirrors, e.g. `unsloth/Llama-3.2-3B-Instruct`, are what the trainer resolves
gated Meta models to. The engine's `DEFAULT_SAVE_METHOD=merged_16bit` merges on
the GPU cluster and ships the full model instead; **Deploy Model** serves
adapters only, so a merged model is served with the platform's Model Manager.

`elapsed_seconds` is the GPU training loop only (`train_runtime`); the UI shows
it as *Training Time* next to *Time on Worker* (pick-up to finish, including
download, model load and upload). Only `training` has step counters,
which keep their final value afterwards, so the UI keys the progress bar off
the phase. A failed job keeps the phase it failed in. The API's reconciler
polls the engine every 15 s while any job is active, so the job list stays
current without a page open.

## Deploying a fine-tuned model

**Deploy Model** creates one KServe `LLMInferenceService` in
`finetuning-models` (`src/api/app/kserve_serving.py`), named after the job.
What it runs with is the serving profile, the ConfigMap
`finetuning-models/finetuning-serving` that `playbooks/deploy-serving.yml`
publishes: the vLLM image, flags and environment of the platform's own models
(Model Manager's defaults), the KV cache size, the NRI balloon
(`vllm-balloon-tp1`) and the model store, a PVC that keeps each base model
after its first download. Sizing comes from the parameter count read off the
model id: weights + `VLLM_CPU_KVCACHE_SPACE` + overhead, clamped to configured
bounds, as requests and limits. The API checks fit per node against
allocatable minus the sum of pod requests, and refuses with 409 when nothing
fits.

What the pod does, in order:

1. `fetch-adapter` (Data Prep image: Python and the `minio` client) downloads
   the job's archive from the object store and unpacks it with Python's `data`
   tar filter (no absolute paths, links out of the tree or devices), keeping
   the directory that holds `adapter_config.json`, into an `emptyDir`.
2. `fetch-base-model` (the vLLM image, which has `huggingface_hub`) reads the
   base model from that `adapter_config.json`, downloads it through the site
   proxy into the model store if it is not there yet, and links
   `deployments/<name>` to it.
3. KServe mounts `pvc://finetuning-model-store/deployments/<name>` at
   `/mnt/models` and runs `vllm serve /mnt/models` with the profile's flags plus
   `--enable-lora --lora-modules <served name>=/mnt/adapter`. vLLM also serves
   the base alone as `<served name>-base`; only the adapter is registered.

Three things are done by hand rather than with KServe's own fields, each found
by trying them on the platform's KServe (v0.18):

- `spec.model.lora.adapters` is accepted but ignored (the adapter is neither
  downloaded nor passed to vLLM), hence the init container and the flags.
- Its storage initializer takes no proxy settings from the resource (an init
  container of the same name is rejected, not merged), so it cannot fetch from
  Hugging Face behind a site proxy; the base model is fetched by our own init
  container into a `pvc://` store instead.
- The init containers mount KServe's own volume for that claim
  (`kserve-pvc-source`): a pod naming the same claim twice never finishes
  mounting it.

KServe starts vLLM through `eval "vllm serve ... $@"`, so an argument with
quotes in it (`--override-generation-config`) is shell-quoted once more.

Once the resource is Ready the API registers the model with LiteLLM under its
served name, with `api_base` the workload Service's full in-cluster name
(`<name>-kserve-workload-svc.finetuning-models.svc.cluster.local:8000/v1`:
LiteLLM runs with the site proxy, and its `no_proxy` matches `.svc` and
`.cluster.local`, not a bare `<service>.<namespace>`), and records that on the
resource (annotation `finetuning.intel.com/gateway-registered`). The status
read does it for a deployment someone is watching, the background loop for any
other. Undeploy removes the LiteLLM entry first, then the resource; the adapter
stays in the user's files. The highest LoRA rank served is
`finetune_serving_max_lora_rank` (64); the UI trains with 16.

## Semantic routing

LiteLLM's auto-router sends a prompt to the route whose utterances it most
resembles. The API mines utterances from the job's training data
(`POST /utterances`) and maintains one shared router (default name `smart`)
whose routes accumulate across deployments. Scoring is the **mean** similarity
of the nearest five utterances, not the best match. A route with fewer than
five utterances is therefore dragged down by its weakest one, and the default
threshold is 0.5 (measured: in-domain 0.57–0.62, out-of-domain 0.40–0.44 with
bge-base). The router caches its config per name, so applying a change restarts
the gateway Deployment, which on Toolkit Manager is the cluster's
shared `litellm/litellm`: every LiteLLM client sees the restart, and with one
replica the gateway is down for 1–2 minutes.

On start, LiteLLM embeds every route's utterances through the router's
embedding model. If that model does not answer through LiteLLM, the calls hang,
the liveness probe kills LiteLLM, and it never comes up. So before applying a
route the API embeds one string through LiteLLM and refuses the change when
that fails, leaving the running router alone.

The fallback ("everything else") can be any chat model LiteLLM serves:
`model_info.mode` of `chat`, or unset, LiteLLM's default, which is how the
platform registers its own models. Routers and embedding models are excluded.
The encoder is `BAAI/bge-base-en-v1.5`, the `LLMInferenceService`
`finetuning-models/finetuning-embedding` deployed by
`playbooks/deploy-embedding-model.yml` from the same model store; the API
registers it as `mode: embedding` with `encoding_format: float` (vLLM rejects
the `null` LiteLLM sends otherwise).

## Where state lives

| State | Store |
|---|---|
| Jobs, deployments, routes | Postgres `finetuning-api/finetuning-service-postgresql` |
| Engine job history and logs | Postgres + log volume in `finetuning-engine` |
| Datasets and trained models | SeaweedFS S3 `dataprep/dataprep-minio`, bucket `dataprep`, at `<user_id>/<file_id>` |
| File metadata, Q&A jobs | Postgres `dataprep/postgres` |
| Deployed models | `LLMInferenceService`s in `finetuning-models`, plus their LiteLLM registrations |
| Base models | PVC `finetuning-models/finetuning-model-store` |
| Generated secrets | Secret `finetuning-api/finetuning-as-a-service-secrets` |
| Users and federation | Realm `platform` in the shared Keycloak (group `finetuning-users`) |
