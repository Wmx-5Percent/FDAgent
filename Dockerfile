# Container image for the FDAgent serving layer (Path 1: FastAPI /ask + static UI).
#
# Design notes (this is a teaching-grade Dockerfile):
#   * Lean: installs ONLY the serving deps (requirements-serve.txt), not the full pipeline.
#   * Stateless & secret-free: the OpenAI key and the database URL are NOT baked in — they
#     are supplied at run time via env vars, so the same image is safe to publish anywhere.
#   * Cache-friendly: dependencies are installed in their own layer, before app code is
#     copied, so editing src/ does not re-run pip.
#   * Portable, with China-friendly overrides: defaults to Docker Hub + PyPI, but where those
#     are slow/blocked, build with mirror overrides:
#       docker build \
#         --build-arg REGISTRY=docker.m.daocloud.io \
#         --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#         -t fdagent .

# Base image registry (ARG before FROM parameterizes the base image). Default = Docker Hub.
ARG REGISTRY=docker.io
FROM ${REGISTRY}/library/python:3.13-slim

# Immediate, unbuffered logs; no stray .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 1) Dependencies first (their own layer). psycopg[binary] bundles libpq, so no apt needed.
#    PIP_INDEX_URL defaults to PyPI; override for a faster local mirror (see header).
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements-serve.txt .
RUN pip install --no-cache-dir --index-url="${PIP_INDEX_URL}" -r requirements-serve.txt

# 2) Then the application: the API/engine code and the static UI.
COPY src/ ./src/
COPY web/ ./web/

# The server listens on $PORT (default 8000). 0.0.0.0 = reachable from outside the container
# (NOT 127.0.0.1, which would only be reachable from inside it). Cloud hosts inject their own
# $PORT, so we read it at run time. JSON exec form + `exec` so SIGTERM (docker stop) reaches
# uvicorn directly for a graceful shutdown.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn src.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
