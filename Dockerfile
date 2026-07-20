# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY scripts/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Install kubectl (matches the cluster version you target; pin as needed).
# TARGETARCH is auto-populated by `docker buildx build --platform ...` — this
# makes the same Dockerfile produce a correct image for amd64 nodes (standard
# EKS/GKE/AKS) and arm64 nodes (Graviton, GKE ARM, Apple Silicon dev clusters)
# without any manual edits.
ARG KUBECTL_VERSION=v1.30.0
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
         -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl && \
    apt-get purge -y curl && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Non-root user for security
RUN useradd --system --uid 1001 --no-create-home observer
USER observer

WORKDIR /app
COPY scripts/ .

# When running in-cluster, kubectl automatically uses the mounted
# ServiceAccount token at /var/run/secrets/kubernetes.io/serviceaccount/
# No kubeconfig or KUBECONFIG env var needed.

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]