FROM python:3.11-slim

# Proxy build args (set only when behind a proxy; passed via --opt=build-arg at build time)
ARG http_proxy
ARG https_proxy
ARG no_proxy
ENV http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    no_proxy=${no_proxy}

WORKDIR /app

# Install system dependencies required for Docling PDF processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 \
    libxrender1 \
    libxext6 \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements-celery.txt .
RUN pip install --no-cache-dir -r requirements-celery.txt

# llama-index fetches NLTK data on first import: stopwords, and a tokenizer it
# checks for as "punkt" but downloads as "punkt_tab" (what sent_tokenize loads),
# so without both it retries on every start. NLTK >= 3.10 refuses that fetch
# through a proxy (SSRF guard), so bake all three in here, where NLTK_DATA points
# both libraries; the opt-in is scoped to this build step and the running worker
# never downloads anything.
ENV NLTK_DATA=/usr/local/share/nltk_data
RUN NLTK_ALLOW_PROXIED_URLOPEN=1 python -m nltk.downloader -d "$NLTK_DATA" stopwords punkt punkt_tab \
 && python -c "import llama_index.core.utils as u; u.globals_helper.stopwords; import nltk; nltk.data.find('tokenizers/punkt'); nltk.data.find('tokenizers/punkt_tab')"

# Docling's layout and table models, baked in like the NLTK data above: the
# worker has no home directory to cache them in, and must not depend on
# reaching Hugging Face at run time. HOME and HF_HOME point anything else that
# wants a cache at a writable place instead of the missing home directory.
ENV DOCLING_ARTIFACTS_PATH=/usr/local/share/docling/models \
    HOME=/tmp \
    HF_HOME=/tmp/hf
RUN docling-tools models download -o "$DOCLING_ARTIFACTS_PATH" layout tableformer \
 && chmod -R a+rX "$DOCLING_ARTIFACTS_PATH"

# Copy application source (separate layer so requirements cache survives source changes)
COPY . .

# Create non-root user to avoid running as root (DS-0002 / CIS Docker Benchmark)
RUN groupadd --gid 1000 appuser \
 && useradd --uid 1000 --gid 1000 --no-create-home --shell /bin/bash appuser \
 && chown -R appuser:appuser /app

USER appuser

# Run Celery worker
CMD ["celery", "-A", "core.celery.celery_config.celery_app", "worker", "--loglevel=info", "--concurrency=4", "--max-tasks-per-child=10", "--prefetch-multiplier=1", "-Q", "dataprep_queue"]
