# Deploying FineTuning-as-a-Service

This guide installs FineTuning-as-a-Service on a cluster where Toolkit Manager
has already installed the `inference` layer (Intel® Inference Toolkit).

- [1. Check the platform](#1-check-the-platform)
- [2. GPUs for training](#2-gpus-for-training)
  - [2a. GPU nodes in this cluster](#2a-gpu-nodes-in-this-cluster)
  - [2b. Separate GPU cluster](#2b-separate-gpu-cluster)
- [3. Configure](#3-configure)
- [4. Deploy](#4-deploy)
- [5. Verify](#5-verify)
- [Troubleshooting](#troubleshooting)

## 1. Check the platform

Install the `inference` layer with Toolkit Manager and LiteLLM
turned on, by setting this in `env/<name>/config.inference.yaml` before
`./toolkit_manager.sh install inference --env <name>` (the `agent-toolkit`
layer is not needed). The shared `platform` realm people sign in with came with
[intel/enterprise-ai-solutions#61](https://github.com/intel/enterprise-ai-solutions/pull/61)
and [intel/enterprise-inference#39](https://github.com/intel/enterprise-inference/pull/39),
merged after Toolkit Manager release-1.2 (Inference Toolkit release-1.1);
install from `main` until the next release. Older checkouts call the
installer `./es_auto_installer.sh`; it takes the same commands. For training
on this cluster, it needs GPU nodes with the NVIDIA GPU Operator installed.

```yaml
auth_type: "litellm_vkey"     # Keycloak is always deployed; this adds LiteLLM + Langfuse

# Who may call LiteLLM in-cluster (Istio policy litellm-ingress). The first two
# are the platform's defaults; the API registers models and routers through it,
# Data Prep generates Q&A through it.
litellm_allowed_namespaces:
  - envoy-gateway-system
  - litellm
  - finetuning-api
  - dataprep
```

On an installed platform, add the two entries and re-run
`./toolkit_manager.sh install litellm --env <name>`; the deploy stops with a
message if they are missing.

Then, from the machine you will deploy from:

```bash
kubectl -n envoy-gateway-system get gateway eg-gateway   # the cluster host (https-apex listener)
kubectl -n keycloak get statefulset keycloak             # Keycloak
kubectl -n keycloak get keycloakrealmimport platform-realm  # the platform realm (#61)
kubectl get crd llminferenceservices.serving.kserve.io   # KServe, which serves the models
kubectl -n litellm get deploy litellm                    # LiteLLM
kubectl -n langfuse get deploy langfuse-web              # Langfuse
kubectl get storageclass local-path
kubectl -n kube-system get svc registry                  # in-cluster registry
```

FineTuning-as-a-Service needs no model of its own to install: its embedding
model is deployed by the run. Base models for training are pulled by the GPU
cluster, and for serving into the model store (`finetuning-model-store`,
200 GiB by default) the first time each is deployed.

If the registry is missing, set `finetune_registry` to a registry the cluster
can pull from and that the in-cluster BuildKit jobs can push to.

Keep the gateway in its default **subdomain** routing mode. In path mode
(`routing_mode: path`) the apex listener admits only namespaces with registered
path grants, which this use case does not register yet; the deploy stops with a
message if it finds path mode.

### What the platform must get right

FineTuning-as-a-Service uses the platform's LiteLLM, Langfuse and Envoy Gateway
as they are. Some of their settings decide whether it works, and they are the
platform's to change:

| Setting | Why it matters | Check |
|---|---|---|
| A request timeout on LiteLLM's route | A `BackendTrafficPolicy` attached to the `litellm` HTTPRoute *replaces* the gateway-wide one rather than merging with it. Without its own `timeout.http.requestTimeout`, the route falls back to Envoy's 15 s default and every chat reply longer than that is cut off. | `kubectl -n litellm get backendtrafficpolicy -o yaml \| grep -A2 timeout` |
| Langfuse's ClickHouse memory | When ClickHouse is OOM-killed, every trace query fails and the *Import from Langfuse* page answers 502 ("Langfuse is failing"). | `kubectl -n langfuse get pods` shows no restarts |
| LiteLLM replicas | Applying a semantic route restarts LiteLLM (see [implementation](implementation.md#semantic-routing)); with one replica the gateway is down for every client for 1–2 minutes. | `kubectl -n litellm get deploy litellm` |

Models the platform registers in LiteLLM without a `mode` count as chat models,
so they can be a router's fallback.

## 2. GPUs for training

The engine has no GPU itself: it turns each training request into one
Kubernetes Job on a GPU node. `finetune_train_mode: auto` (the default) uses a
separate GPU cluster when `finetune_train_kubeconfig` is set, and this cluster's
GPU nodes otherwise. With neither, the deploy stops and says why;
`finetune_engine_enabled: false` installs everything except training.

### 2a. GPU nodes in this cluster

Nothing to prepare beyond GPU nodes with the NVIDIA GPU Operator. The deploy:

- finds the GPU nodes by the GPU Operator's label `nvidia.com/gpu.present=true`;
- creates the training namespace (`finetune_train_namespace`, default `ftaas`)
  in the mesh with Pod Security `baseline`, and the engine's Role there plus a
  ClusterRole to read nodes;
- creates the shared work volume on `ceph-filesystem`
  (`finetune_train_pvc_storage_class`), and stops if that class is missing;
- points the training pod at Data Prep's in-cluster Service and gives it the
  proxy of the shell running the deploy.

To cap how many GPUs training may take, add a ResourceQuota to the namespace
(example below); the engine reads it and never runs more jobs at once than it
allows.

### 2b. Separate GPU cluster

For testing, or when the GPUs live in another cluster. The engine creates the
Jobs there with a kubeconfig, and the training pod comes back to this cluster's
Files API through the ingress. The GPU cluster's administrator prepares:

1. **A namespace** (default `ftaas`) with a GPU quota. The engine reads the
   quota and runs no more jobs at once than it allows, so a shared cluster with
   one free GPU gets one job at a time instead of jobs stuck `Pending`:

   ```yaml
   apiVersion: v1
   kind: ResourceQuota
   metadata: {name: ftaas-quota, namespace: ftaas}
   spec:
     hard: {requests.nvidia.com/gpu: "1", requests.cpu: "32", requests.memory: 256Gi, pods: "20"}
   ```

   Each training pod asks for 1 GPU, 8–16 CPU and 48–96 GiB of memory
   (`finetune_engine_train_*` in `vars/finetune-plugin-vars.yml`); a LimitRange
   there must not cap pods below that. Training pods need nothing privileged,
   so the namespace can enforce Pod Security `baseline`
   (`pod-security.kubernetes.io/enforce=baseline`); `restricted` does not work,
   the training image runs as root. GPU nodes need the label
   `nvidia.com/gpu.present=true` (the NVIDIA GPU Operator sets it).
2. **A kubeconfig** for a ServiceAccount there with exactly these rights. Keep
   it mode 0600; it is copied into a Secret and never passed on a command line.
   Its `server:` must be reachable from this cluster's pods.

   ```yaml
   apiVersion: rbac.authorization.k8s.io/v1
   kind: Role
   metadata: {name: ftaas-engine, namespace: ftaas}
   rules:
   - apiGroups: ["batch"]
     resources: ["jobs"]
     verbs: ["create", "get", "list", "watch", "delete"]
   - apiGroups: [""]
     resources: ["pods", "pods/log"]
     verbs: ["get", "list", "watch"]
   - apiGroups: [""]
     resources: ["pods/exec"]          # cancelling a job, reading its result file
     verbs: ["create", "get"]          # get: exec over websockets (recent Kubernetes)
   - apiGroups: [""]
     resources: ["configmaps", "secrets"]
     verbs: ["create", "get", "list", "delete"]
   - apiGroups: [""]
     resources: ["persistentvolumeclaims"]
     verbs: ["create", "get"]
   - apiGroups: [""]
     resources: ["resourcequotas", "limitranges", "events"]
     verbs: ["get", "list"]
   ---
   apiVersion: rbac.authorization.k8s.io/v1
   kind: ClusterRole
   metadata: {name: ftaas-engine-nodes}
   rules:
   - apiGroups: [""]
     resources: ["nodes"]               # GPU model and memory, from the node labels
     verbs: ["get", "list"]
   ```

   Bind both to the ServiceAccount. Check with
   `kubectl --kubeconfig <file> -n ftaas auth can-i create pods --subresource=exec`
   (`auth can-i create pods/exec` tests a pod *named* `exec` and says no).
3. **A ReadWriteMany storage class** for the shared work volume (default
   `ceph-filesystem`; `finetune_train_pvc_storage_class`). It holds the
   Hugging Face cache, so later runs skip the base-model download.
4. **Network, in both directions:**
   - The training pod fetches its dataset from, and uploads the model to, this
     cluster's Files API at `https://<cluster host>/finetuning-as-a-service/v1/files`.
     Those requests carry no browser session. The pod presents a per-job token
     the Fine-Tuning API signed for the job's owner, good only for that job's
     files and its model upload; nothing about its address needs configuring.
   - The GPU cluster's DNS has no record for this cluster's host, so the pod
     resolves it to `finetune_files_api_ip` itself. Use the address the ingress
     is reachable on from there. On a single-node cluster that is the node's own
     address, and it changes if the node is rebuilt.
   - GPU nodes usually reach Hugging Face through a proxy:
     `finetune_train_https_proxy`, and `finetune_train_no_proxy` **must** list
     the cluster host and `finetune_files_api_ip` by name and address. A CIDR
     alone does not cover the host name the pod connects to.
   - Test from a pod on a GPU node, with the host name rather than the bare
     address: the gateway picks its certificate by name (SNI) and drops a TLS
     connection that names none, which looks like a firewall but is not.

     ```bash
     curl -k --noproxy '*' --resolve <cluster host>:443:<finetune_files_api_ip> \
       https://<cluster host>/finetuning-as-a-service/dataprep/health      # {"status":"healthy"}
     ```
   - The training image is pinned by digest (`finetune_engine_train_image`,
     about 14 GB). Pre-pull it on the GPU nodes, or the first job spends its
     time downloading it.
5. **TLS:** the ingress certificate is typically self-signed, so
   `finetune_files_api_tls_verify` defaults to `"false"`. Set it to a CA bundle
   path inside the training image once the certificate comes from a CA.

## 3. Configure

```bash
cd usecases/finetuning-as-a-service
cp finetune-config.cfg.example finetune-config.cfg
```

Fill in the training block (step 2) if it applies. Every
other key can stay as shipped. Keep values quoted: an unquoted empty value
becomes YAML null.

To use an external identity provider instead of the shared Keycloak, set
`keycloak_enabled: false`, `oidc_discovery` to the provider's in-cluster-reachable
`.well-known` URL, and `oidc_client_id` / `oidc_client_secret` to a confidential
client with the standard flow and redirect URI
`https://<cluster host>/finetuning-as-a-service/ui/*`.

## 4. Deploy

```bash
./deploy.sh --dry-run    # validates the config and playbooks, changes nothing
./deploy.sh
```

The run, in order:

1. Reads the platform's settings from the cluster and resolves the secrets
   (generated on the first run, then reused).
2. Lets containerd on this node pull from the in-cluster registry over HTTP.
   This writes one file under `/etc/containerd/certs.d` with sudo, once; pass
   `-K` if sudo asks for a password.
3. Builds the five images in-cluster with BuildKit.
4. Publishes the serving profile and creates the model store in
   `finetuning-models`.
5. Reconciles its two clients and the group `finetuning-users` in the
   `platform` realm of the shared Keycloak that Toolkit Manager
   installed. The run stops if that Keycloak or the realm is missing; it never
   installs either.
6. Deploys Data Prep, the embedding model (a KServe `LLMInferenceService`), the
   engine, the API and the UI.
7. Publishes its routes on the platform gateway, with the sign-in policy, and
   checks that an unsigned-in browser is sent to the `platform` realm.

A first install takes 20–40 minutes, mostly image builds and the embedding
model download. A full re-run rebuilds the images (fast, from the BuildKit
cache) and upgrades every release in place; `--tags <component>` redeploys just
one, and `--skip-tags build` skips the builds.

From the repository root, `./deploy-usecase.sh finetuning-as-a-service` runs the
same script.

## 5. Verify

```bash
H=$(kubectl -n envoy-gateway-system get gateway eg-gateway \
  -o 'jsonpath={.spec.listeners[?(@.name=="https-apex")].hostname}')

# Unauthenticated: the UI redirects to the login, the APIs refuse.
curl -sk -o /dev/null -w '%{http_code}\n' --noproxy '*' "https://$H/finetuning-as-a-service/ui"        # 302
curl -sk -o /dev/null -w '%{http_code}\n' --noproxy '*' "https://$H/finetuning-as-a-service/v1/models"  # 401

# The engine answers and the API can get a token for it (engine enabled only).
kubectl -n finetuning-api exec deploy/finetuning-service -- \
  sh -c 'curl -s -o /dev/null -w "%{http_code}\n" "$NVIDIA_API_URL/docs"'                  # 200

# The routes and their sign-in policy are accepted by the gateway.
kubectl -n finetuning-api get httproute,securitypolicy

# The serving profile is published, and the embedding model is Ready (the API
# then registers it with LiteLLM).
kubectl -n finetuning-models get configmap finetuning-serving
kubectl -n finetuning-models get llminferenceservice
```

Then sign in at `https://<cluster host>/finetuning-as-a-service/ui`, upload a small JSONL
dataset (`{"messages":[{"role":"user",...},{"role":"assistant",...}]}` per
line), start a job on a small base model, and deploy the result. The job page
shows each phase (downloading, preparing, training with step counts,
uploading the LoRA adapter), and the deployed model appears in LiteLLM's model
list once it answers. The first deployment of a base model downloads it from
Hugging Face through the site proxy into the model store; later ones start from
there.

## Upgrading an install that used APISIX

Earlier versions signed people in with an APISIX gateway of their own, against
a realm `finetuning`, and served models with a vLLM chart of their own. A run
of this version moves the install over and removes those:

- The routes move to the platform gateway (`--tags routes`), and the APISIX
  release and namespace `auth-apisix` are removed at the end of the run.
- People sign in through the `platform` realm. Their accounts in the realm
  `finetuning` do not move with them: add each person to the group
  `finetuning-users` in the `platform` realm (or federate the directory there).
  Jobs, files and models stay with the user name they were created under, so
  use the same user names. The realm `finetuning` is left alone; delete it in
  the Keycloak admin console once nobody needs it.
- The router's encoder becomes a KServe `LLMInferenceService`, and the old
  release `vllm-bge-embedding` and its volume are removed.
- Models deployed from the UI by the old version are Helm releases; remove them
  with Undeploy before the upgrade, or with `helm uninstall <release> -n
  finetuning-models` after it, and deploy them again.

## Troubleshooting

**The run stops at "no GPU nodes to train on"** — this cluster has no node
labelled `nvidia.com/gpu.present=true` and `finetune_train_kubeconfig` is empty.
With GPU nodes in this cluster, check `kubectl get nodes -L nvidia.com/gpu.present` and the GPU
Operator (`kubectl -n gpu-operator get pods`). Otherwise use
[2b](#2b-separate-gpu-cluster), or set `finetune_engine_enabled: false`.

**The run stops at path routing mode** — see [step 1](#1-check-the-platform).

**The run stops at `Validate Required Configuration`** — one of the engine URL,
token URL or client id is blank. The shipped values point at the bundled engine
and the shared Keycloak; restore them unless you use an external engine.

**`containerd` task fails with `sudo: a password is required`** — re-run with `-K`.

**Training jobs fail at file download or upload.** The pod reaches the Files API
through this cluster's ingress, so check:

- **Wrong address.** A stale `finetune_files_api_ip` and an untrusted
  certificate raise the same `CERTIFICATE_VERIFY_FAILED`, because the old host
  usually still answers with some other certificate. Look at the subject:
  `openssl s_client -connect <ip>:443 -servername <host> </dev/null | openssl x509 -noout -subject`.
  Anything other than the platform's certificate for the base domain means
  wrong address.
- **401.** Data Prep rejected the job token: it expired (the job outlived
  `DATAPREP_JOB_TOKEN_TTL`, default 8 days), or the API and Data Prep hold
  different `files_job_token_secret` values. `kubectl logs -n dataprep
  deploy/data-prep-backend | grep "job token"` says which.
- **403.** The token is valid but the request is outside the job's scope, e.g.
  a file that is not the job's training or validation file.
- **403 on upload only.** The upload went through the proxy: add the host and
  address to `finetune_train_no_proxy`.

**The engine is unreachable from the API.** `kubectl logs -n finetuning-api
deploy/finetuning-service` and the curl in [Verify](#5-verify). The config keys
map to the pod's `NVIDIA_API_URL`, `NVIDIA_KEYCLOAK_TOKEN_URL`,
`NVIDIA_KEYCLOAK_CLIENT_ID` and `NVIDIA_KEYCLOAK_CLIENT_SECRET`.

**Training jobs stay `pending` or the engine logs `Forbidden`.** The GPU
cluster's kubeconfig lacks a right from [step 2b](#2b-separate-gpu-cluster).
Without `resourcequotas` the engine assumes the node's full GPU count and jobs
wait `Pending` for GPUs the quota does not grant; without `pods/exec`, cancel
does nothing.

**The deployment page says "Could Not Read Deployment Status" (timeout).** The
API reaches the Kubernetes API server on its real port, 6443 by default, not
the `kubernetes` Service's 443: NetworkPolicies see the endpoint after the
Service address is translated. Set `networkPolicy.egress.apiServerPorts` in
the API chart if your API server listens elsewhere.

**A model deployment fails pulling its init image.** The adapter is fetched
with the Data Prep image from `finetune_registry` (`fetch.image` in the serving
profile), so the inference namespace must be able to pull from that registry.

**A model deployment stops at "Download the base model".** The
`fetch-base-model` init container downloads it from Hugging Face through the
proxy of the shell that ran the deploy (`proxy` in the serving profile). Its
log says why: `kubectl -n finetuning-models logs <pod> -c fetch-base-model`.
`Disk quota exceeded` or `No space left` means the model store is full; delete
base models nobody serves from it, or give `finetune_model_store_size` more.

**A deployed model is slow (about 2 tokens/s).** vLLM binds its CPU threads to
the cores the NRI balloon gives the pod (`finetune_serving_balloon`, the same
policy as the platform's models). Without the platform's NRI balloons
DaemonSet the pod has no cores of its own and vLLM's automatic binding can end
up on none: check `kubectl -n kube-system get ds nri-resource-policy-balloons`
and the `OpenMP thread binding` lines of the model's log. A 3B model on 12
cores gives roughly 8 tokens/s.

**Chat replies stop after about 15 seconds, and those requests are missing from
LiteLLM's Logs.** The platform's LiteLLM route has no request timeout, see
[What the platform must get right](#what-the-platform-must-get-right). Envoy's
access log shows `UT` / `response_timeout` at 15000 ms. LiteLLM logs a streamed
reply only once it completes, which a cut stream never does.

**LiteLLM restarts every few minutes after a semantic route was applied.** The
router embeds its utterances when LiteLLM starts. If the embedding model does
not answer through LiteLLM, those calls hang and the liveness probe kills the
pod. The API now checks the encoder before it applies a route; for an older
setup, check that the embedding model's `api_base` in LiteLLM is a full
`*.svc.cluster.local` name (a short `<service>.<namespace>` name goes to the
proxy).

**"No other chat model is deployed" on the routing panel.** A router needs a
second chat model to send unmatched prompts to. Any LiteLLM model whose
`model_info.mode` is `chat` or unset counts; embedding models and routers do
not.

**Import from Langfuse answers 502.** Langfuse itself failed; the message says
"Langfuse is failing". Check `kubectl -n langfuse get pods`, usually its
ClickHouse.

**A deployed model cannot be removed** (stuck `Removing`) —
`kubectl -n finetuning-models get llminferenceservice`; its Deployment and
Service go with it. If LiteLLM still lists the model, delete it in LiteLLM's UI.

**PVCs `Pending`** — `kubectl get sc` should list `local-path`; check
`kubectl get pods -n local-path-storage`.

**Proxy errors from your own curl** — quote it: `--noproxy '*'`. Unquoted, the
shell expands the `*` and the request goes to the proxy.
