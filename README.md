# pod-observer — deploy anywhere

This turns the k8s-pod-observer L1 agent into something you can install on
any Kubernetes cluster with one command, regardless of cloud, node
architecture, or existing infra.

Three things make it portable:

1. **In-cluster auth only.** The service uses the ServiceAccount token
   Kubernetes mounts automatically — no kubeconfig, no cluster-specific
   credentials baked into the image. Same image works on EKS, GKE, AKS,
   k3s, kind, wherever.
2. **Multi-arch image.** The Dockerfile now builds for both `amd64` and
   `arm64` in one `buildx` call, so it runs on standard cloud nodes and on
   Graviton/ARM nodes without a separate build.
3. **One Helm chart, no manual YAML editing.** Everything that used to be a
   placeholder you'd hand-edit (`YOUR_REGISTRY/...`, namespace, RBAC scope)
   is now a `--set` flag with a sensible default.

## 1. Build and push the image (once per registry)

You need somewhere to host the image — your own registry, or a public one
like GitHub Container Registry (GHCR), which is free for public images and
needs no infra of its own.

```bash
# One-time: enable multi-platform builds
docker buildx create --use --name pod-observer-builder

# Build for both architectures and push in one step
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/YOUR_GH_USERNAME/k8s-pod-observer:1.0.0 \
  --push .
```

(`docker buildx build` requires `--push` — it can't `--load` a multi-arch
manifest into local Docker, only push it to a registry.)

## 2. Install on any cluster

Point `kubectl` at the target cluster (`kubectl config use-context ...`),
then:

```bash
helm install pod-observer ./helm/pod-observer \
  --set image.repository=ghcr.io/YOUR_GH_USERNAME/k8s-pod-observer \
  --set image.tag=1.0.0 \
  --set anthropic.apiKey=$ANTHROPIC_API_KEY
```

That's the entire install — creates the namespace, RBAC, Secret, Deployment,
Service, and HPA. Helm prints next steps (port-forward command, readiness
check) after install.

## Common overrides

| What | Flag | Default |
|---|---|---|
| Restrict to one namespace instead of cluster-wide | `--set rbac.scope=namespace --set namespace.name=my-ns` | `cluster` |
| Use a Secret you manage separately (External Secrets, Sealed Secrets, etc.) | `--set anthropic.existingSecret=my-secret --set anthropic.existingSecretKey=my-key` | chart creates its own |
| Pin resource limits for a small cluster | `--set resources.limits.memory=128Mi` | 256Mi |
| Disable autoscaling | `--set autoscaling.enabled=false` | enabled, 1–3 replicas |

Full list of overridable values: `helm/pod-observer/values.yaml`.

## Uninstall

```bash
helm uninstall pod-observer -n pod-observer
```

(Namespace itself isn't deleted automatically — `kubectl delete ns pod-observer` if you want it fully gone.)

## Upgrading to a new image version

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/YOUR_GH_USERNAME/k8s-pod-observer:1.1.0 --push .

helm upgrade pod-observer ./helm/pod-observer \
  --reuse-values \
  --set image.tag=1.1.0
```

## What's inside `scripts/`

Unchanged from the original k8s-pod-observer skill — `api.py` (FastAPI
service), `l1_runbook.py` (deterministic Observe→Classify→Remediate→Verify→
Resolve/Escalate decision tree), `pod_observer.py`, `remediators.py`,
`models.py`, `orchestrator.py`. The Helm chart and multi-arch Dockerfile are
purely a packaging layer on top — no application logic changed.
