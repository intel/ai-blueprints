# FineTuning-as-a-Service — End-to-End Validation

What was checked before calling this use case working on a platform
installed by Toolkit Manager, how, and the last result. Re-run it after a platform upgrade or a
change to the training or serving path; [deployment.md](deployment.md) has the
commands that are not spelled out here.

## Environment of the last runs

Two runs. The latest one moved sign-in to the platform gateway and serving to
the platform's KServe, on a platform rebuilt from scratch. The training rows
are from the run before it.

| | Latest run (2026-10-26) | Training run (2026-10-21) |
|---|---|---|
| Control cluster | single-node Intel Xeon cluster, Toolkit Manager platform rebuilt from scratch from [intel/enterprise-ai-solutions#61](https://github.com/intel/enterprise-ai-solutions/pull/61) with latest `main` and the inference layer from [intel/enterprise-inference#39](https://github.com/intel/enterprise-inference/pull/39): `platform` realm, Headlamp, `auth_type: litellm_vkey`, Istio ambient mesh, site proxy | the same machine on the platform before #61; this use case had its own sign-in gateway and model chart |
| Training cluster | — | **remote** mode: a separate Kubernetes cluster with NVIDIA B300 GPUs, shared with other teams; namespace quota of one GPU; CephFS (`ceph-filesystem`) for the work volume |
| Models | `Llama-3.2-3B-Instruct` adapter from the training run, served on `unsloth/Llama-3.2-3B-Instruct`; `qwen2-5-coder-14b` deployed with the platform's Model Manager as the chat model | `meta-llama/Llama-3.2-3B-Instruct` (trained through Unsloth's public copy) |
| Install | the use case removed completely, then `./deploy.sh` onto the fresh platform (engine left out) | fresh clone, `./deploy-usecase.sh finetuning-as-a-service` |

## Results

Legend: ✅ passed · ⚠️ passed with a platform caveat · *21 Oct*: from the
training run

### Install and access

| # | Check | How | Result |
|---|---|---|---|
| 1 | Deploy onto a fresh platform | `./deploy.sh` after the platform was rebuilt | ✅ |
| 2 | Re-run changes nothing | `./deploy.sh` again; no secret rotated | ✅ |
| 3 | Unauthenticated access refused | UI → 302 to `realms/platform`; the UI's background calls (`Sec-Fetch-Mode: cors`), `/v1/files`, `/v1/fine_tuning/jobs` → 401 | ✅ |
| 3a | Forged identity refused | `X-Forwarded-User` sent without a session → 401; with a session, Envoy replaces it with the signed-in user | ✅ |
| 3b | Public pages stay open | `/api/health`, `/api/docs`, `/dataprep/health`, `/dataprep/docs` → 200 | ✅ |
| 4 | One sign-in for every platform UI | signed in to Headlamp as `platform-admin`, then opened the portal: no second prompt | ✅ |
| 4a | Only allowed groups admitted | the portal admits `platform-admins` and `finetuning-users` | ✅ (`platform-admin`) |
| 5 | Uninstall and install again | `./deploy.sh --uninstall`, then `./deploy.sh` | ✅ *21 Oct*; uninstall ✅ again before the platform rebuild |
| 5b | LiteLLM refuses this use case | Istio policy `litellm-ingress` without its namespaces → the deploy stops with its message; with them, the API and Data Prep reach LiteLLM | ✅ |

### Data

| # | Check | How | Result |
|---|---|---|---|
| 6 | Upload, list, download, delete a file | Files page | ✅ |
| 7 | Q&A generation from documents | Data Preparation with a two-page PDF: Docling → `qwen2-5-coder-14b` through LiteLLM → 20 pairs in a JSONL file the API's training validator accepts | ✅ (conversion under 1 s, generation 71 s) |
| 7a | Cancel a data preparation job | cancel while Docling converts a 288-page PDF: the running step stops within seconds, its temporary files are removed, the Q&A step never runs, the job reads `CANCELLED` | ✅ |
| 8 | Import from Langfuse, OpenAI chat format | Preview on a project with LiteLLM traces, then Import | ⚠️ *21 Oct*: works; Langfuse's ClickHouse was being OOM-killed on that platform, so some requests answered 502 until retried |

### Training on the GPU cluster

All *21 Oct*. The training path is the same since, apart from where the
training pod fetches its dataset (the same Data Prep route, now on the platform
gateway, matched on the job token).

| # | Check | How | Result |
|---|---|---|---|
| 9 | Engine reaches the GPU cluster and reads its quota | engine log: `1 concurrent slot(s) of NVIDIA B300 ...` with quota 1 | ✅ |
| 10 | Training pod downloads the dataset with its job token | through the platform gateway, no browser session | ✅ |
| 11 | Job token is scoped | forged, expired and out-of-scope tokens → 401/403; listing, deleting, presigned URLs refused | ✅ (forged token through the new job route → 401 from Data Prep, checked again) |
| 12 | Train and upload a LoRA adapter only | 43 s of training, an 88 MB adapter uploaded (no merge, no checkpoints) | ✅ |
| 13 | Training time shown is the GPU loop only | job page: Training Time 43 s, Time on Worker ≈ 3 min | ✅ |
| 13a | Finished jobs leave the active-jobs metric | `active_jobs_by_user` back to 0 once the job finished | ✅ |
| 15a | Training mode chosen correctly | `auto` with a kubeconfig → remote; no kubeconfig, no GPU node → the deploy stops; `local` → in-cluster Files API | ✅ |

### Serving and routing

| # | Check | How | Result |
|---|---|---|---|
| 16 | Deploy a LoRA adapter on the platform's KServe | Deploy Model on the trained job: `LLMInferenceService`, adapter fetched, base model downloaded into the model store, vLLM `--enable-lora` | ✅ ready in about 2 minutes with the base model in the store |
| 17 | Registered with LiteLLM | registered by the API once Ready; `api_base` the workload Service's full `*.svc.cluster.local` name | ✅ |
| 18 | Chat through LiteLLM | the fine-tuned model through LiteLLM | ✅ |
| 19 | Generation speed | 48–60 tokens, temperature 0 | ✅ ≈ 8 tokens/s for the 3B model on 12 cores (NRI balloon) |
| 19a | A vLLM start that fails is reported | a setting that does not fit (context longer than the KV cache) → Failed, retryable | ✅ |
| 20 | Semantic router with a fallback | `smart-router`: a matching prompt → fine-tuned model, an unrelated one → the platform's model | ✅ *21 Oct*; the router's encoder now runs on KServe and answers through LiteLLM (768-dimension embeddings) ✅ |
| 21 | A broken encoder cannot take LiteLLM down | applying a route first embeds a test string through LiteLLM and refuses on failure | ✅ *21 Oct* |
| 22 | Undeploy | removes the LiteLLM entry, then the `LLMInferenceService`; deploy again works | ✅ |

### Housekeeping

| # | Check | How | Result |
|---|---|---|---|
| 24 | Deleting data removes it everywhere | files via the Files API (database row and object), jobs, logs, model, finished GPU Job | ✅ *21 Oct* |

## Found during validation, and fixed

Each of these blocked a step above on the first try.

Latest run:

- KServe ignores `spec.model.lora.adapters`, and its storage initializer takes
  no proxy settings → the adapter and the base model are fetched by init
  containers of this use case, the base into a model store kept between
  deployments.
- A pod naming the same claim twice never finishes mounting it → the init
  containers mount KServe's own volume for the model store.
- vLLM refused to start: a model's full context (128K tokens) needs more KV
  cache than the platform's default 8 GiB → `--max-model-len 32768` by default.
- A vLLM start that crashed showed as loading forever → reported as failed.
- The platform's Istio policy admits only listed namespaces to LiteLLM → the
  deploy checks for this use case's two and says how to add them.
- Q&A generation sent its requests to the training engine, which serves no
  model → LiteLLM, with a chat model it serves.
- Docling could not cache its models (the worker has no home directory) → the
  models are part of the image.
- Data preparation wrote a JSON array into a `.jsonl` file with an ID training
  does not accept → one example per line, and a `file-…` ID.
- A cancelled or failed data preparation task left its temporary files → removed
  on every exit; cancelling stops the running step too.
- A next.js and a DOMPurify release with critical advisories → updated.

Training run:

- The training pod was admitted by source address, which the platform gateway
  cannot provide and a shared GPU cluster makes unsafe → per-job token.
- The API could not reach the Kubernetes API (NetworkPolicy allowed 443, not
  6443) → deployment status and Deploy Model work.
- Models were registered in LiteLLM by short name, which LiteLLM sent to the
  proxy → full in-cluster names.
- The trainer merged and uploaded the full model (5.2 GB) → adapter only.
- Langfuse import skipped every LiteLLM trace (`{"messages": [...]}` input) →
  converted.
- The platform's own LLM was not offered as a router fallback (no `mode`) →
  counted as chat.
- A storage-class check stopped the install on a GPU cluster whose account may
  not read storage classes → only a missing class stops it.

## Open on the platform side

Not fixable in this use case; see
[What the platform must get right](deployment.md#what-the-platform-must-get-right):

- LiteLLM's route needs a request timeout (15 s cut-off otherwise).
- Langfuse's ClickHouse needs more memory than the platform gave it.
- LiteLLM runs with one replica, so a route change is a short outage for every
  gateway client.
- Tearing the platform down hangs when a model deployed with Model Manager is
  still there ([intel/enterprise-ai-solutions#71](https://github.com/intel/enterprise-ai-solutions/issues/71)).
