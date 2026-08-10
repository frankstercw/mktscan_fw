# MktScan dashboard (Streamlit web service).
#
# Uses requirements-railway.txt, which omits torch/transformers — see the notes
# in that file. Pass --build-arg REQUIREMENTS=requirements.txt to build the full
# FinBERT image instead.
FROM python:3.11-slim

ARG REQUIREMENTS=requirements-railway.txt

# libpq is needed by psycopg2-binary at runtime; gcc for any source builds.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements*.txt ./
RUN pip install --no-cache-dir --prefer-binary -r ${REQUIREMENTS}

COPY . .

RUN mkdir -p data logs && chmod +x scripts/*.sh

# Railway injects $PORT; the healthcheck must use it rather than a fixed port.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8501}/_stcore/health" || exit 1

CMD ["./scripts/start-dashboard.sh"]
