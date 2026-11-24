# Fine-Tuning API

The FastAPI service behind FineTuning-as-a-Service. It provides an
OpenAI-compatible fine-tuning jobs API, deploys fine-tuned models as vLLM
releases, and manages their semantic routes in the LiteLLM gateway. Training
itself runs in the Fine-Tuning Engine (`../engine`), which the API calls with
OAuth2 client credentials.

Deploy it as part of the use case, not on its own:

```bash
cd ../..                    # usecases/finetuning-as-a-service
./deploy.sh --tags api      # or --tags build,api after a code change
```

It is then served at `https://<cluster host>/finetuning-as-a-service/`, with
the interactive docs at `/finetuning-as-a-service/api/docs`.

## Endpoints

All under `/finetuning-as-a-service`.

| Method and path | Purpose |
|---|---|
| `POST /v1/fine_tuning/jobs` | Create a job (validates the training file through Data Prep first) |
| `GET /v1/fine_tuning/jobs`, `GET .../jobs/{id}` | List jobs; one job with live status from the engine |
| `GET .../jobs/{id}/events`, `/logs`, `/checkpoints` | Progress events, training logs, checkpoints |
| `POST .../jobs/{id}/cancel` | Cancel |
| `POST .../jobs/{id}/deploy`, `GET`/`DELETE .../jobs/{id}/deployment` | Deploy the fine-tuned model as a vLLM release, check it, remove it |
| `GET .../jobs/{id}/deployment-capacity`, `.../deployment/logs` | Whether the model fits on a node; deployment logs |
| `POST .../jobs/{id}/utterances` | Mine routing utterances from the training data |
| `GET`/`PUT`/`DELETE .../jobs/{id}/semantic-route`, `POST .../semantic-route/test`, `GET .../semantic-route/readiness` | The model's route in the gateway's semantic router |
| `GET /v1/models` | Base models available for fine-tuning |
| `GET /api/health`, `/api/liveness`, `/api/readiness` | Probes |

## Authentication

The platform gateway (Envoy Gateway) signs people in in front of this service
and passes the verified username in `X-Forwarded-User`, replacing any copy of
that header sent by the client. The API trusts that header and does not
validate tokens itself, so it must only be reachable through the gateway. Its
NetworkPolicy admits ingress from the gateway's Envoy pods only. See
[../../docs/implementation.md](../../docs/implementation.md#request-path-and-authentication).

## Configuration

Set by the Helm chart (`helm-charts/finetuning-api`) from the use case's
playbooks. The values you would touch are in
`../../vars/finetune-plugin-vars.yml`:

| Setting | Comes from |
|---|---|
| Engine URL, token URL, client id / secret | `finetune-config.cfg` (`nvidia_*`), secret generated |
| Database URL | generated password, `finetuning-service-postgresql` release |
| Gateway URL and master key | the toolkit's LiteLLM, Secret `genai-gateway-master-key` |
| Data Prep URL | in-cluster Service, used to validate training files |
| Model deployment | KServe `LLMInferenceService`s in `finetuning-models`, from the serving profile ConfigMap `finetuning-models/finetuning-serving` |

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # point DATABASE_URL at a reachable Postgres
uvicorn app.main:app --reload --port 8000
```

`app/sql/init-db.sql` creates the schema. Every column added later also has an
`ADD COLUMN IF NOT EXISTS` line there, so the file is safe to re-run against an
existing database.

## Layout

```
app/
├── main.py, config.py        entry point and settings
├── auth.py                   identity from X-Forwarded-User
├── routers/                  jobs, deployments, semantic_routes, models, health
├── adapters/                 engine client (resources/nvidia_adapter.py)
├── reconciler.py             background refresh of active jobs from the engine
├── capacity.py, sizing.py    node-fit check and model resource sizing
├── gateway.py, utterances.py LiteLLM admin calls; utterance mining and scoring
├── k8s.py                    Kubernetes API calls (jobs, pods, LLMInferenceServices)
├── kserve_serving.py         the LLMInferenceService a deployment is
└── sql/init-db.sql           schema
helm-charts/
├── finetuning-api/           the service, its RBAC and NetworkPolicy
└── postgresql/               its database (also used by the engine)
buildkit/                     in-cluster image build, run by ../../deploy.sh --tags build
```
