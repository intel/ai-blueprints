# FineTuning-as-a-Service — User Guide

This guide is for people who use the portal: you bring data, train a model on
it, and serve the result. Installing the service is covered in
[deployment.md](deployment.md).

The portal is at `https://<cluster host>/finetuning-as-a-service/ui`. You sign
in with your platform account; ask your administrator for the host name and to
add you to the `finetuning-users` group.

- [The flow](#the-flow)
- [1. Sign in](#1-sign-in)
- [2. Get a dataset](#2-get-a-dataset)
- [3. Train](#3-train)
- [4. Deploy the model](#4-deploy-the-model)
- [5. Route prompts to it (optional)](#5-route-prompts-to-it-optional)
- [6. Use the model](#6-use-the-model)
- [Limits worth knowing](#limits-worth-knowing)

## The flow

```
 dataset ──► fine-tuning job ──► LoRA adapter ──► deployment ──► LiteLLM ──► your app
 (upload, documents,  (GPU cluster)              (served on its            (optionally behind
  or Langfuse traces)                             base model)               a semantic router)
```

A job trains a **LoRA adapter**: a small set of weights (tens to hundreds of
MB) that changes how a base model answers. Deploying it serves the base model
with your adapter on top, under its own model name, through the same LiteLLM
gateway as the other models on the platform.

## 1. Sign in

Opening the portal sends you to the platform's sign-in page, the same one as
for the platform's other tools (Headlamp, Grafana, Langfuse, LiteLLM): signed
in to one, you are signed in to all. Your session covers every page and API
under `/finetuning-as-a-service`. "Access denied" after signing in means your
account is not in the `finetuning-users` group yet. Everything you upload,
train and deploy is yours: other users do not see your files or jobs.

## 2. Get a dataset

Training data is a **JSONL** file: one example per line, in any of these
shapes.

```json
{"messages": [{"role": "system", "content": "You are a support agent."},
              {"role": "user", "content": "How do I reset my password?"},
              {"role": "assistant", "content": "Open Settings, then Security, then Reset."}]}
{"prompt": "How do I reset my password?", "completion": "Open Settings, then Security, then Reset."}
{"instruction": "Translate to French", "input": "Good morning", "output": "Bonjour"}
```

Each line needs a user turn and an assistant answer. Lines that look like
prompt injection are rejected. Ten examples is the minimum that makes sense;
a few hundred to a few thousand is typical.

There are three ways to get one:

| Page | Use it when | You get |
|---|---|---|
| **Files** | you already have a JSONL file | the file, ready to train on |
| **Data Preparation** | you have documents (PDF, DOCX, PPTX, TXT) | question–answer pairs generated from them by an LLM, saved as JSONL |
| **Import from Langfuse** | your application already sends traffic through LiteLLM | the real conversations, saved as JSONL |

**Import from Langfuse**: pick the project and a time range, optionally filter
by model, tags, user, or a score that reviewers gave in Langfuse, and choose a
format:

- **OpenAI chat**: one training example per conversation that has both a user
  prompt and an assistant reply. Traces without either are skipped and
  counted, so "Scanned 9, skipped 9" means none was usable, not that the
  import failed.
- **Raw**: the traces as Langfuse stores them, for your own processing.
- **Custom**: only the fields you pick.

*Preview* shows exactly what *Import* will write. If the page answers
"Langfuse is failing", Langfuse itself is down; try again later and tell your
administrator.

## 3. Train

**Fine-Tuning → Create Fine-Tuning Job**: pick a base model, your training file,
optionally a validation file, and the hyperparameters (the defaults suit a
first run). Submit.

The job page shows where the run is:

| Phase | What happens |
|---|---|
| queued | waiting for a free GPU; the GPU cluster may be shared |
| downloading data | the training pod fetches your file |
| preparing | the base model loads, the data is checked |
| training | steps, loss and progress are live |
| uploading | the adapter is stored next to your files |

Three times are shown separately:

- **Training Time**: the GPU training loop only.
- **Time on Worker**: from pick-up to finish, including the data download, the
  model load and the upload. On a small dataset this is several times the
  training time.
- **Queue Wait**: before a GPU was free.

The result appears under **Files** as `finetuned_models#model.tar.gz`, and the
job's log as `finetuning_logs#...`.

## 4. Deploy the model

**Deploy Model** on a finished job (or on the **Deployments** page) starts a
model server for it. The CPU, memory and context size are suggested from the
base model's size; the deployment is refused if no node has room.

The first deployment of a base model takes a few minutes: it is downloaded
once, then kept for later deployments, and your adapter is loaded on top. When
the status is *Serving*, the model is
registered with LiteLLM under the name shown on the page, for example
`Llama-3.2-3B-Instruct-finetuned-20261021103709`.

**Undeploy** stops the server and removes the model from LiteLLM. The trained
adapter stays in your files, so you can deploy it again later.

## 5. Route prompts to it (optional)

A **semantic router** is a single model name, for example `smart-router`, that
looks at each prompt and sends it to the model whose examples it resembles
most. Prompts that match nothing go to a fallback model.

On the deployment page, under semantic routing:

1. **Utterances**: example prompts this model should answer. The portal
   suggests some from your training data; edit them. Five or more works best.
2. **Score threshold**: how close a prompt must be to count as a match. Raise
   it if unrelated prompts reach your model.
3. **Everything else goes to**: the fallback, any other chat model on the
   platform.
4. **Apply.**

Applying a route **restarts the LiteLLM gateway**, so every model on the
platform is unavailable for a minute or two. Make route changes when that is
acceptable. The test box on the panel shows which model a prompt would go to.

## 6. Use the model

Call it like any other model through LiteLLM, with a LiteLLM key from your
administrator:

```bash
curl https://litellm.<cluster host>/v1/chat/completions \
  -H "Authorization: Bearer <your LiteLLM key>" \
  -H 'Content-Type: application/json' \
  -d '{"model": "<your model or router name>",
       "messages": [{"role": "user", "content": "Hello"}]}'
```

The LiteLLM admin UI (`https://litellm.<cluster host>/ui`) has a playground,
and its *Logs* page lists each completed request with the model that answered
it, which for a router is the model it chose.

## Limits worth knowing

- **One job at a time per free GPU.** Jobs beyond the GPU quota wait in the
  queue rather than fail.
- **Base models must be public on Hugging Face.** The serving side downloads
  the base model without a token.
- **Models are served on CPU**, by the platform's KServe. Expect about 8 tokens
  per second for a 3B model on 12 cores; long answers take a while.
- **Route changes restart the gateway** (see above).
