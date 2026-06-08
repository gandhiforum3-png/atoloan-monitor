# Atoloan Troubleshooter Agent

AI-powered SRE troubleshooter for the Atoloan platform. Runs live `kubectl` and `aws` diagnostics,
feeds results to Claude for root cause analysis, and executes safe remediation actions from the dashboard.

## Architecture

```
dashboard/          ← React + Vite frontend (port 5173)
agent/              ← FastAPI backend (port 8000)
  main.py           ← Agent logic, kubectl/aws wrappers, Claude API integration
  requirements.txt
```

**How it works:**
1. You describe a problem (or pick a quick check)
2. The agent runs real `kubectl` + `aws` commands against your cluster
3. All output is sent to Claude (claude-sonnet-4-20250514) for analysis
4. Claude returns a structured diagnosis: severity, root cause, exact remediation commands
5. You click "Run this" to execute safe commands directly from the UI
6. Scale controls let you adjust replica counts with one click

## Prerequisites

- Python 3.11+
- Node 18+
- `kubectl` installed and in PATH
- `aws` CLI installed and configured (`aws configure`)
- Kubeconfig files in place:
  - `~/.kube/config-aws-dev`  (dev)
  - `~/.kube/config-aws-uat`  (uat)
  - `~/.kube/config-aws-prod` (prod)
  - Default minikube context (local)
- `ANTHROPIC_API_KEY` environment variable set

## Setup

### 1. Backend

```bash
cd agent/
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn main:app --reload --port 8000
```

The agent will be available at http://localhost:8000.
Test it: `curl http://localhost:8000/health`

### 2. Dashboard

```bash
cd dashboard/
npm install
npm run dev
```

Open http://localhost:5173.

## Usage

### Quick checks (left sidebar)
- **Pod health** — scan all pods for CrashLoopBackOff, Pending, OOMKill
- **Secret sync** — verify ExternalSecrets are synced from Secrets Manager
- **Restart / OOM** — identify pods with high restart counts
- **Ingress status** — check nginx ingress ADDRESS (common k3s issue)
- **Node resources** — check CPU/memory headroom
- **Postgres logs** — look for connection failures or slow queries
- **CoreDNS** — detect stale IPs after minikube restart

### Free-form diagnosis
Type any symptom in the input box:
- "my backend pod is crashlooping"
- "frontend returns 502"
- "ExternalSecret is not syncing"
- "ingress shows no ADDRESS"

### Layer filters (top bar)
Focus Claude on a specific layer: Kubernetes, EC2, Postgres, Secrets, or Ingress.

### Executing fixes
Safe commands have a **Run this** button — click to execute against your cluster.
Commands that need human judgment (anything that changes data or configuration) are flagged separately.

### Scaling
After diagnosis, affected deployments show a scale panel: click to set replica count (1–5).
Enforced minimum: 1 replica. Maximum: 5 replicas (safe for t3.small).

## Safety rules (enforced in code)

The agent will never execute or suggest:
- `delete` (pods, deployments, namespaces)
- `terminate` (EC2 instances)
- `destroy` (Terraform resources)
- `drop` or `truncate` (database tables)
- Scale to 0 replicas

These checks run at the Python function boundary in `main.py:safety_check()` — they cannot be
reasoned around by Claude's output.

## Adding a new deployment to the safe-to-scale list

Edit `SAFE_DEPLOYMENTS` in `agent/main.py`:

```python
SAFE_DEPLOYMENTS = {
    "atoloan-api", "atoloan-ui", "postgres",
    "coredns", "ingress-nginx-controller",
    "your-new-service",   # add here
}
```

## Running on the monitoring EC2

On the dedicated EC2 instance (when built):

```bash
# Use an IAM role — no credentials in env
# The instance profile handles auth for aws cli
export ANTHROPIC_API_KEY=<from secrets manager>

# Run behind nginx for HTTPS
uvicorn agent.main:app --host 0.0.0.0 --port 8000
```

For kubeconfig on the EC2 instance:
```bash
scp -i ~/Downloads/atoloan-dev.pem \
  ubuntu@3.135.161.145:/etc/rancher/k3s/k3s.yaml ~/.kube/config-aws-dev
sed -i 's/127.0.0.1/3.135.161.145/g' ~/.kube/config-aws-dev
```

## API reference

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Agent health check |
| `/environments` | GET | List environments and labels |
| `/status/{env}` | GET | Live pod + node status snapshot |
| `/diagnose` | POST | Diagnose an issue (non-streaming) |
| `/diagnose/stream` | POST | Diagnose with streaming tokens (used by dashboard) |
| `/action/run` | POST | Execute a safe kubectl/aws command |
| `/action/scale` | POST | Scale a deployment (safety-checked) |

## Known issues

- The `backend-ingress.yaml` in `k8s/aws-dev/` has `rewrite-target: /` which breaks all API routes
  except `/`. The agent knows this and will flag it during ingress diagnosis.
- On k3s EC2, nginx LoadBalancer won't get an external IP without the `externalIPs` patch.
  Quick check "Ingress status" detects this automatically.
- CoreDNS IPs change on every minikube restart. Quick check "CoreDNS" detects stale IPs.
