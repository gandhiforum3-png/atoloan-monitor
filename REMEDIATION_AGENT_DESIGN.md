# Remediation Agent — Design

Standalone pipeline: `orchestrator.py` (Find) → `remediation_agent.py` (Fix) → CLI summary.
No integration with `l1_runbook.py` / `watcher.py` — this is a separate, on-demand tool invoked
as `python remediate.py <pod> [namespace] [flags]`.

## 1. CLI

```
python remediate.py <pod> [namespace] [--dry-run] [--auto-approve] [--issue "optional symptom text"]
```

- `pod` / `namespace` — positional, same convention as the existing `l1_runbook.py` / `orchestrator.py` CLIs.
- `--dry-run` — every remediator call goes through with `dry_run=True` (already supported by every
  function in `remediators.py`).
- `--auto-approve` — without it, Tier-2 actions are printed as a proposal and not executed; with it,
  Tier-2 also runs.
- `--issue` — optional human hint passed into the orchestrator's investigation (same as `diagnose_pod`'s
  `issue_description` today).

Three phases, one process:

```
FIND      → orchestrator investigates the pod, returns a structured IssueReport
FIX       → remediation_agent takes the IssueReport, re-validates it, acts (or doesn't), verifies
SUMMARIZE → prints one structured report of what was found and what was done
```

Exit codes: `0` resolved, `1` escalated/proposed/diagnose-only, `2` error.

## 2. FIND — extending `orchestrator.py`

The orchestrator becomes the **sole classifier** (no deterministic fallback ladder behind it), so its
job gets broader, not smaller:

- Broaden `SYSTEM_PROMPT` so it actively looks for and names every class in the Action Catalog below,
  not just the 5 `l1_runbook.py` knows.
- Add a machine-readable tail to the required output format:
  ```
  CLASSIFICATION: <open-vocabulary failure class>
  CONFIDENCE: <high|medium|low>
  DEPLOYMENT: <owning deployment name, or "none">
  ```
- Add a new function `find_issue(pod, namespace, issue_description="") -> IssueReport`. Leave
  `diagnose_pod()` untouched — nothing else depends on it, and a new function does the job cleanly
  without changing an existing public contract.
- Resolve the owning Deployment via `resolve_owning_deployment()` (promoted out of `watcher.py`'s
  private `_resolve_deployment`) before the agent loop starts, so `IssueReport.deployment` is always
  populated.
- Give the orchestrator the new read-only observer tools (section 4) so it reasons from real data,
  not prose guesses.

**Contract:** the Remediation Agent never executes a Tier-1 action on the orchestrator's word alone.
It independently re-derives the evidence using deterministic observer calls and only proceeds if that
re-check corroborates the LLM's classification. If it doesn't, the entry is downgraded to Tier 3
regardless of stated confidence. The LLM proposes, the deterministic code disposes.

## 3. Action Catalog

Tier legend: **1** = auto-execute after confirm · **2** = execute only with `--auto-approve` ·
**3** = never executes, always escalates.

### I. Crash / restart-class

| Classification | Confirm check | Remediator | Tier |
|---|---|---|---|
| OOMKilled | `diagnose_crash()` → `OOM_KILLED` + memory limit present | `patch_memory_limit` → verify | **1** |
| CrashLoopBackOff, exit 1 (generic app error) | exit_code not in (126,127) | `restart_pod` → verify | **1** |
| CrashLoopBackOff, exit 127 (binary not found) | exit_code == 127 | none — image broken | 3 |
| CrashLoopBackOff, exit 126 (permission denied) | exit_code == 126 | none — image entrypoint perms | 3 |
| Init container crash, transient | `diagnose_init_container_crash()` exit_code not in (126,127) | `restart_pod` → verify | **1** |
| Init container crash, exit 126/127 | exit_code in (126,127) | none | 3 |

### II. Image-class

| Classification | Confirm check | Remediator | Tier |
|---|---|---|---|
| ImagePullBackOff/ErrImagePull, transient | no "manifest unknown/not found/invalid reference" in event msg | `force_image_repull` → verify | **1** |
| ImagePullBackOff, bad tag | bad-tag keyword match in event msg | none | 3 |
| InvalidImageName | `waiting.reason == InvalidImageName` | none | 3 |
| ErrImageNeverPull | `waiting.reason == ErrImageNeverPull` | none | 3 |

### III. Config/spec-class

| Classification | Confirm check | Remediator | Tier |
|---|---|---|---|
| CreateContainerConfigError, refs all exist (race) | `diagnose_container_config_error()` — all referenced ConfigMaps/Secrets present | `restart_pod` → verify | **1** |
| CreateContainerConfigError, ref(s) missing | ≥1 referenced object missing | none — name the missing object | 3 |
| CreateContainerError | `waiting.reason == CreateContainerError` | none — bad command/cgroup | 3 |

### IV. Scheduling/node-class

| Classification | Confirm check | Remediator | Tier |
|---|---|---|---|
| ContainerCreating, under threshold | `diagnose_stuck_container_creating()` duration < 5m, no adverse events | `wait_and_reverify` | **1** |
| ContainerCreating, stuck past threshold | duration ≥ 5m or FailedMount/FailedAttachVolume/CNI event present | none — storage/CNI infra | 3 |
| FailedScheduling, insufficient resources | `diagnose_pending()` shows Insufficient cpu/memory | none | 3 |
| FailedScheduling, taint/toleration mismatch | event message pattern "untolerated taint" | none | 3 |
| FailedScheduling, unbound PVC | `diagnose_pending().unbound_pvcs` non-empty | none | 3 |
| Node pressure, Running pod | `check_pod_node_pressure()` shows Memory/Disk/PIDPressure=True | none auto (`cordon`/`drain` forbidden); may propose `scale_replicas` elsewhere | 3 (2 if proposing scale) |
| Evicted | `status.reason == "Evicted"` | none — verify controller already recreated replacement | 1 (verify-only, no write) |
| VolumeMountError | `waiting.reason` contains "Mount" or `FailedMount` event | none | 3 |

### V. Networking/service-class

| Classification | Confirm check | Remediator | Tier |
|---|---|---|---|
| Probe failure, transient | wait 30s, recheck Ready | `wait_and_reverify` | **1** |
| Probe failure, persistent | still not Ready after wait | none | 3 |
| Endpoint/label mismatch | `diagnose_endpoint_mismatch()` — pod labels don't satisfy Service selector | none | 3 |

### VI. Controller-level class

| Classification | Confirm check | Remediator | Tier |
|---|---|---|---|
| Rollout stuck (ProgressDeadlineExceeded) | `get_deployment_conditions()` — Progressing=False, reason=ProgressDeadlineExceeded | `rollback_deployment` → verify | **1** |
| Quota exceeded | `get_resourcequota_status()` + `FailedCreate` event mentions quota | none | 3 |
| PDB blocked | `get_pdb_status().disruptions_allowed == 0` + block event | none | 3 |
| HPA degraded | `get_hpa_status()` — AbleToScale=False | none (may propose manual `scale_replicas`) | 3 (2 if proposing) |

### VII. Terminal / catch-all

| Classification | Confirm check | Remediator | Tier |
|---|---|---|---|
| Stuck Terminating | `waiting_reason == "Terminating"` past threshold | `delete_stuck_pod(force=True)` | **2** (explicit opt-in, matches existing design) |
| Healthy | phase Running+Ready or Succeeded | none — no-op | resolved |
| Unclassified/Unknown | nothing matched, or confirm check failed to corroborate orchestrator's classification | none | 3 — escalate with full evidence + orchestrator narrative |

The "not confirmed" path is the safety backstop: whatever tier the orchestrator's classification would
imply, if the deterministic confirm function doesn't corroborate it, the entry is treated as row 31
(Unknown) — Tier 3, no execution.

## 4. New functionality required

### New observer functions (`pod_observer.py`) — all read-only

| Function | Purpose | Feeds classification |
|---|---|---|
| `resolve_owning_deployment(pod, namespace)` | Promote `watcher.py`'s private `_resolve_deployment` to a shared, public observer | needed by almost every remediator |
| `diagnose_container_config_error(pod, namespace)` | Parses pod spec's `envFrom`/`valueFrom`/volumes for ConfigMap/Secret refs, checks each exists | `CreateContainerConfigError` |
| `diagnose_init_container_crash(pod, namespace)` | Mirrors `diagnose_crash()` but scoped to `init_container_statuses` + that container's logs | init container failure |
| `diagnose_stuck_container_creating(pod, namespace)` | How long in `ContainerCreating`, plus `FailedMount`/`FailedAttachVolume`/CNI events | stuck `ContainerCreating` |
| `check_pod_node_pressure(pod, namespace)` | Resolves pod's node, filters `get_node_conditions()` (exists, never called outside Pending) to that node | node pressure on Running pods |
| `diagnose_endpoint_mismatch(pod, namespace)` | Cross-checks pod labels against a Service's selector using `get_endpoints()` (exists, never called) | Service/endpoint connectivity |
| `get_deployment_conditions(deployment, namespace)` | Reads `.status.conditions`, flags `ProgressDeadlineExceeded` | rollout stuck |
| `get_resourcequota_status(namespace)` | Reads ResourceQuota objects + usage | quota rejection |
| `get_pdb_status(namespace)` | Reads PDB `.status.disruptionsAllowed` | PDB blocking |
| `get_hpa_status(namespace, name)` | Reads HPA `.status.conditions` | HPA degraded |

### New remediator function (`remediators.py`)

Most of the newly-covered classes are legitimately **diagnose-only** — there is no safe kubectl write
for a bad image tag, a missing Secret, RBAC, NetworkPolicy, or a namespace quota, and inventing one
would break the safety model `FORBIDDEN_OPERATIONS` enforces. The only genuinely new write-adjacent
function needed:

- `wait_and_reverify(pod, namespace, wait_s, timeout_s)` — no kubectl write, just a timed re-observe
  (same pattern as today's probe-failure handling). Everything else Tier-1-actionable reuses
  `restart_pod`, `patch_memory_limit`, `force_image_repull`, or `rollback_deployment`, which already exist.

### New models (`models.py`)

- Extend `FailureClass` with the new members implied by the catalog (`CREATE_CONTAINER_CONFIG_ERROR`,
  `INVALID_IMAGE_NAME`, `ERR_IMAGE_NEVER_PULL`, `CONTAINER_CREATING_STUCK`, `NODE_PRESSURE`,
  `ENDPOINT_MISMATCH`, `ROLLOUT_STUCK`, `QUOTA_EXCEEDED`, `PDB_BLOCKED`, `TAINT_MISMATCH`,
  `HPA_DEGRADED`) — and finally put the already-defined-but-unused `VOLUME_MOUNT`, `EVICTED`,
  `INIT_FAILURE` to use.
- `ActionTier` enum: `AUTO`, `NEEDS_APPROVAL`, `DIAGNOSE_ONLY`.
- Small result models for the new observers: `ConfigRefCheck`, `DeploymentConditionStatus`,
  `QuotaStatus`, `PDBStatus`, `HPAStatus`.
- `IssueReport` (extends today's `DiagnosisResult` shape): `pod`, `namespace`, `deployment`,
  `classification`, `confidence`, `evidence: dict`, plus the existing narrative fields.
- `RemediationSummary`: `pod`, `namespace`, `classification`, `confidence`, `tier`, `confirmed: bool`,
  `action_taken: Optional[RemediationResult]`, `verified_healthy: Optional[bool]`,
  `resolution: resolved|escalated|proposed|diagnose_only`, `root_cause`, `next_steps: list[str]`.

### Explicitly out of scope

DNS/CoreDNS, RBAC denials, NetworkPolicy blocks, admission webhook rejections, cert expiry,
control-plane/etcd health — no deterministic observer here can confirm these, and the orchestrator can
at best guess from app log text ("connection refused", "no such host", "forbidden"). Those guesses
always land in Tier 3 regardless of stated confidence — an LLM guess about RBAC/NetworkPolicy must
never drive an execution decision. These belong to a separate cluster-health checker with a different
trigger surface, not this agent.

## 5. Build plan

Dependency order — each phase only needs what came before it.

### Phase 0 — shared foundations

**`models.py`**
- Extend `FailureClass` with the new members listed in section 4; activate `VOLUME_MOUNT`/`EVICTED`/`INIT_FAILURE`.
- Add `ActionTier` enum.
- Add the new observer result models: `ConfigRefCheck`, `DeploymentConditionStatus`, `QuotaStatus`, `PDBStatus`, `HPAStatus`.
- Add `IssueReport` and `RemediationSummary`.

**`pod_observer.py`** — implement the ten new read-only functions in section 4.

**`remediators.py`** — add `wait_and_reverify(pod, namespace, wait_s, timeout_s)`. No kubectl write;
reuses the polling pattern from `verify_pod_healthy`. `FORBIDDEN_OPERATIONS` untouched.

### Phase 1 — Find stage (`orchestrator.py` + `k8s_tools.py`)

- `k8s_tools.py`: register the Phase 0 observer functions as additional tool schemas + `execute_tool`
  entries. Still read-only — no remediator ever gets exposed as an LLM tool.
- `orchestrator.py`:
  - Broaden `SYSTEM_PROMPT` to cover every classification in the Action Catalog.
  - Extend the structured-answer format with `CLASSIFICATION` / `CONFIDENCE` / `DEPLOYMENT` lines;
    add a parser for them.
  - Add `find_issue(pod, namespace, issue_description="") -> IssueReport`. Leave `diagnose_pod()`
    unchanged.
  - Resolve the deployment via `resolve_owning_deployment` at the start of `find_issue`, before the
    agent loop, so it's always in `IssueReport.deployment`.

### Phase 2 — Fix stage (`remediation_agent.py`, new file)

- `ACTION_CATALOG`: the table in section 3 encoded as
  `dict[str, CatalogEntry(confirm_fn, remediator_fn, tier, wait_before_verify_s)]`.
- One confirm function per row family (several rows share one — e.g. all crash-loop variants reuse a
  single confirm wrapping `diagnose_crash` / `diagnose_init_container_crash`).
- `dispatch(issue_report, dry_run: bool, auto_approve: bool) -> RemediationSummary`:
  1. look up classification (fallback to Unknown entry if missing from catalog)
  2. run confirm function against fresh evidence
  3. not confirmed → build Tier-3 summary immediately, regardless of what tier the classification would
     normally get
  4. confirmed + Tier 1 → execute remediator → verify → summary
  5. confirmed + Tier 2 → execute only if `auto_approve`, else propose and stop
  6. confirmed + Tier 3 → summary with evidence, no execution
- Narrative/next-steps generation: reuse the *pattern* from `l1_runbook._generate_l2_actions` (one
  small Claude call to phrase a numbered action list) but re-implement it locally in
  `remediation_agent.py` rather than importing from `l1_runbook.py` — keeps the "no runbook
  integration" boundary clean.

### Phase 3 — CLI glue (`remediate.py`, new file)

- argparse: `pod` (positional), `namespace` (positional, default `"default"`), `--dry-run`,
  `--auto-approve`, `--issue`.
- `main()`: `issue_report = await orchestrator.find_issue(...)` →
  `summary = await remediation_agent.dispatch(issue_report, ...)` → deterministic formatted print
  (FOUND / DID / RESULT / NEXT STEPS — same visual style as `l1_runbook.py`'s existing CLI output).
- Exit codes: `0` resolved, `1` escalated/proposed/diagnose-only, `2` error.

### Phase 4 — validation

- Reproduce each catalog row against a real/kind cluster (bad image, missing ConfigMap, OOM, etc.) and
  confirm the right row fires.
- Adversarial test: force a wrong `CLASSIFICATION` line from the orchestrator and confirm `dispatch()`
  downgrades to Tier 3 instead of executing — proves the re-validation safety gate actually works, not
  just the happy path.
- Confirm `FORBIDDEN_OPERATIONS` still blocks even under a deliberately misconfigured catalog entry
  (defense in depth).

Build order top-to-bottom: **Phase 0 → 1 → 2 → 3**, Phase 4 continuously as each catalog row lands.
