# MktScan — one image, two roles.
#
# Both Railway services build this same Dockerfile. Which one a container becomes
# is decided at runtime by MKTSCAN_ROLE (see scripts/start.sh):
#
#     MKTSCAN_ROLE=scheduler   → background worker, owns the schema
#     MKTSCAN_ROLE=dashboard   → Streamlit web UI (default)
#
# This replaces the previous two-Dockerfile + railway.toml-per-service setup,
# which required entering an exact absolute config path in the Railway UI and
# silently fell back to the wrong service type if that path was off by a
# character.
#
# Uses requirements-railway.txt (no torch/transformers — ~2.5GB and ~2GB RAM
# that Railway will not thank you for). Build with
#   --build-arg REQUIREMENTS=requirements.txt
# for the full FinBERT image.
FROM python:3.11-slim

ARG REQUIREMENTS=requirements-railway.txt

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MKTSCAN_ROLE=dashboard

COPY requirements*.txt ./
RUN pip install --no-cache-dir --prefer-binary -r ${REQUIREMENTS}

COPY . .

# Git does not always preserve the executable bit, and Railway may invoke these
# directly, so set it here rather than relying on the checkout.
RUN mkdir -p data logs && chmod +x scripts/*.sh

CMD ["./scripts/start.sh"]
