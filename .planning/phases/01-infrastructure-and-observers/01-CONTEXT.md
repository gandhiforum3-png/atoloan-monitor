# Phase 1: Infrastructure and Observers - Context

**Gathered:** 2026-06-15
**Status:** Ready for planning
**Source:** In-conversation discussion (architecture generalization request)

<domain>
## Phase Boundary

Phase 1 ("Infrastructure and Observers") already has a working K8s node monitor
built ahead of formal tracking: `k8s_node_observer.py` (OBS-02, including
NetworkUnavailable + node_deleted coverage), `k8s_orchestrator.py`, and two
parallel remediation architectures (`node_diagnoser.py` + `node_remediator.py`
for `mode="diagnoser"`, and `node_agent.py` for `mode="agent"`).

**This sub-scope of Phase 1 is a refactor, not new monitoring capability**:
generalize the existing K8s-node-only implementation into a reusable
observer/orchestrator/skill framework so the remaining observers and
remediators required by REQUIREMENTS.md (OBS-01, OBS-03 through OBS-08,
REM-01 through REM-08 — K8s pods, EC2, FastAPI, Postgres, log analysis,
security groups, secrets) can each be added as a small, mostly-mechanical
addition (new observer file + new diagnoser/remediator/agent files +
one registry entry) rather than a copy-paste of the full pipeline.

Building the other 7 observers/remediators themselves is **out of scope**
for this refactor — this phase only builds the generic framework and
migrates the existing K8s node monitor onto it, proving the framework works
by keeping the K8s node monitor's behavior unchanged (both `mode="diagnoser"`
and `mode="agent"` paths must continue to work exactly as before, including
the recent NetworkUnavailable/node_deleted additions).

</domain>

<decisions>
## Implementation Decisions

### Open action_type (prerequisite for any non-K8s domain)
- **D-01:** `DiagnosisResult.action_type` (`agent/shared/models.py`) changes
  from the closed `Literal["pod_restart", "deployment_scale_down",
  "human_escalate", "observe_only"]` to an open `str`. Validity is enforced
  at the orchestrator/remediation boundary against that domain's registered
  threshold keys, with `agent/shared/safety.py::safety_check()` /
  `FORBIDDEN_OPERATIONS` remaining the absolute, domain-agnostic floor
  (unchanged — still blocks `terminate_instance`, `drop_table`, `drain_node`,
  `cordon_node`, etc. regardless of domain).
- **D-02:** This is required so future domains (EC2 `reboot_instance`,
  Postgres `kill_query`) can express valid actions the current Literal
  doesn't allow — without it, the registry pattern is structurally complete
  but those domains could never produce a legal `DiagnosisResult`.

### Shared remediation primitives (extract, no behavior change)
- **D-03:** Move `PreflightResult` (dataclass), `_meets_threshold`,
  `_log_action`, and the `THRESHOLDS` registry from
  `agent/skills/remediators/node_remediator.py` into a new
  `agent/shared/remediation.py`.
- **D-04:** `THRESHOLDS` becomes `dict[str, dict[str, float]]` keyed by
  `domain` then `action_type` (e.g. `THRESHOLDS["k8s"]["pod_restart"] = 0.80`),
  so each new domain registers its own action/threshold pairs without
  touching shared code.
- **D-05:** `node_remediator.py` and `node_agent.py` update their imports to
  pull these from `agent/shared/remediation.py`; their own behavior must not
  change (cooldown, PDB checks, replica floor, learning_mode gating all
  identical to today).

### Generic agentic tool-use loop
- **D-06:** Extract the iteration loop from `node_agent.py::run_incident`
  (message accumulation, `MAX_ITERATIONS` cap, `finish_incident` handling,
  no-tool-call break, max-iterations auto-escalation) into
  `agent/shared/agent_loop.py::run_tool_loop(anthropic_client, redis, bundle,
  incident_id, *, system_prompt, tools, dispatch_tool, format_bundle,
  learning_mode, max_iterations)`.
- **D-07:** `node_agent.py` keeps only its domain-specific pieces:
  `_SYSTEM_PROMPT`, `TOOLS`, `_format_bundle`, `_dispatch_tool`, and a thin
  `run_incident` that sets up the K8s API clients (`core_v1`/`apps_v1`/
  `policy_v1`) as `ctx` and calls the shared loop.

### Generic diagnoser call
- **D-08:** Extract the "call Claude with cached system prompt + forced
  `tool_choice` to `submit_diagnosis`, parse `DiagnosisResult`" boilerplate
  from `node_diagnoser.py::diagnose` into
  `agent/shared/diagnoser_base.py::diagnose_with_claude(client, system_prompt,
  user_text, result_model=DiagnosisResult)`.
- **D-09:** `node_diagnoser.py` keeps only `_SYSTEM_PROMPT` and
  `_format_bundle`, calling the shared helper.

### Domain registry + generic orchestrator
- **D-10:** Define a `DomainConfig` dataclass (new `agent/registry.py`):
  `domain`, `stream`, `consumer_group`, `context_fetcher`, `diagnose`,
  `remediate`, `run_incident`, `escalate_below` (threshold dict reference),
  `urgency_map`.
- **D-11:** Extract the debounce/consumer-group loop from
  `k8s_orchestrator.py::run` into
  `agent/orchestrators/generic_orchestrator.py::run(redis, anthropic_client,
  config: DomainConfig, *, debounce_seconds, learning_mode, mode)`.
- **D-12:** `k8s_orchestrator.py` shrinks to its k8s-specific
  `_fetch_pods_on_nodes` context fetcher plus its `DomainConfig` registration
  in `agent/registry.py`. Claude's discretion on whether the file is renamed,
  merged into `agent/skills/diagnosers/node_diagnoser.py`, or kept as-is —
  as long as the generic loop itself is not duplicated per domain.
- **D-13:** `agent/skills/remediators/human_escalator.py::_URGENCY_MAP`
  becomes per-domain, looked up via the domain's `DomainConfig.urgency_map`
  (registry-driven) instead of a single flat k8s-only dict — but keep the
  existing fallback to `"p3_within_1h"` when an event_type has no mapping.

### Runner generalization + documentation
- **D-14:** `scripts/run_local.py` iterates registered domains from
  `agent/registry.py` to start one observer task + one
  `generic_orchestrator.run(...)` task per domain, replacing the current
  hardcoded two-task `TaskGroup`. Add a `--monitors k8s,...` flag (comma list)
  defaulting to all registered domains.
- **D-15:** Write `.planning/ADDING-A-MONITOR.md` — the concrete recipe for
  adding a new domain: new observer file (publishes `InfraEvent` via
  `agent/shared/event_bus.py::publish`, reusing `agent/shared/backoff.py` if
  extracted), new diagnoser file (system prompt + `_format_bundle` +
  `diagnose_with_claude`), new remediator file (domain `THRESHOLDS` entries +
  preflight/execute functions + `remediate()`), optional agent file (system
  prompt + `TOOLS` + `_dispatch_tool` + `run_tool_loop`), and one
  `DomainConfig` registration in `agent/registry.py`. This doc is the
  checklist for the 7 remaining observers in ROADMAP.md/REQUIREMENTS.md.

### Backoff helper
- **D-16:** `_backoff` (exponential backoff with jitter, currently in
  `k8s_node_observer.py`) is generic and reusable — extract to
  `agent/shared/backoff.py` if a second observer in this phase would
  otherwise duplicate it. If no second observer is being written in this
  phase, this extraction is optional/Claude's discretion (low priority,
  do only if cheap).

### Claude's Discretion
- Exact internal structure/field types of `DomainConfig` beyond what D-10
  specifies.
- Whether `k8s_orchestrator.py` is kept, renamed, or merged (D-12).
- File/module naming for the new `agent/shared/*.py` helper modules beyond
  what's specified above (`remediation.py`, `agent_loop.py`,
  `diagnoser_base.py`, optionally `backoff.py`).
- Whether to do the `_backoff` extraction now (D-16) or defer it.
- Ordering/wave structure of tasks, as long as the K8s node monitor
  (`mode="diagnoser"` and `mode="agent"`) keeps working at every step.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Current K8s node monitor implementation (the thing being generalized)
- `agent/observers/k8s_node_observer.py` — observer pattern to generalize
  (watch loop, `_backoff`, `_emit`/`_emit_node_deleted`, condition maps)
- `agent/orchestrators/k8s_orchestrator.py` — debounce/consumer-group loop to
  extract into the generic orchestrator; `_fetch_pods_on_nodes` context
  fetcher pattern
- `agent/skills/diagnosers/node_diagnoser.py` — diagnoser pattern to
  generalize (`_SYSTEM_PROMPT`, `_format_bundle`, `diagnose`)
- `agent/skills/remediators/node_remediator.py` — remediator pattern;
  source of `PreflightResult`, `THRESHOLDS`, `_meets_threshold`, `_log_action`
  to extract
- `agent/skills/agents/node_agent.py` — agent-loop pattern to generalize
  (`_SYSTEM_PROMPT`, `TOOLS`, `_format_bundle`, `_dispatch_tool`,
  `run_incident`)
- `agent/skills/remediators/human_escalator.py` — `_URGENCY_MAP` to make
  per-domain
- `agent/shared/models.py` — `InfraEvent`, `SignalBundle`, `DiagnosisResult`
  (action_type change, D-01/D-02), `HumanEscalationPacket`
- `agent/shared/safety.py` — `FORBIDDEN_OPERATIONS`/`safety_check()`, the
  unchanged global safety floor
- `agent/shared/event_bus.py` — `publish`/`tail`, already domain-agnostic
- `scripts/run_local.py` — runner to generalize (D-14)

### Requirements driving the framework shape
- `.planning/REQUIREMENTS.md` — OBS-01, OBS-03–OBS-08 and REM-01–REM-08
  define the observers/remediators this framework must be able to
  accommodate (K8s pods, EC2, FastAPI, Postgres, log analyzer, security
  group auditor, secrets checker)
- `.planning/REMEDIATION-APPROACHES.md` — documents both existing K8s node
  remediation architectures (`mode="diagnoser"` and `mode="agent"`) end to
  end; both must continue to work identically after this refactor and this
  doc's descriptions should remain accurate (update if file/module names
  referenced in it change)

</canonical_refs>

<specifics>
## Specific Ideas

- The motivating question was: "will it work for k8s pod, ec2, or other
  services?" — the answer (yes, with the open `action_type`) is the
  validation criterion for this refactor. A good acceptance test is: could
  an EC2 `reboot_instance` action or a Postgres `kill_query` action flow
  through `DomainConfig` + `generic_orchestrator` + `agent/shared/
  remediation.py` + `human_escalator` without further core changes, even
  though this phase doesn't implement those observers yet?
- K8s pod-level observer (OBS-01) was identified as the "cheapest" proof
  case for the framework since it can reuse the *same* `domain="k8s"`
  registry entry, diagnoser, remediator, and agent as the node observer
  (just a new observer file feeding `events:k8s`) — useful as a sanity
  check of the registry design, but its implementation is still out of
  scope per the Phase Boundary above.

</specifics>

<code_context>
## Existing Code Insights

### Reusable Assets
- `agent/shared/event_bus.py::publish`/`tail` — already fully domain-agnostic,
  no changes needed
- `agent/shared/safety.py::FORBIDDEN_OPERATIONS`/`safety_check` — already
  fully domain-agnostic, no changes needed (remains the hard floor under the
  new open `action_type`)
- `node_remediator.py`'s `PreflightResult`/cooldown/PDB pattern — the shape
  (not the K8s-specific bodies) generalizes to other domains' preflight
  functions

### Established Patterns
- Every observer: watch/poll loop → build a problem dict → `_emit()` helper
  → `publish(redis, InfraEvent)` to `events:<domain>`
- Every diagnoser: cached system prompt + forced `tool_choice` →
  `DiagnosisResult` via `submit_diagnosis` tool
- Every agent: tool-use loop bounded by `MAX_ITERATIONS`, ending in
  `finish_incident`, falling back to `human_escalator.escalate` on timeout
- Orchestrator: consumer group on `events:<domain>` → debounce window →
  `SignalBundle` → diagnoser+remediator OR agent loop

### Integration Points
- `agent/registry.py` (new) is the single place new domains get wired in —
  observers, diagnoser/remediator/agent modules, and thresholds/urgency maps
  all referenced from `DomainConfig` entries
- `scripts/run_local.py` reads the registry to start tasks — this is the
  local dev integration point that proves a new domain is fully wired

</code_context>

<deferred>
## Deferred Ideas

- Actual implementation of OBS-01 (K8s pod observer), OBS-03–OBS-08 (EC2,
  FastAPI, Postgres, log analyzer, security group auditor, secrets checker)
  and their corresponding REM-02–REM-08 remediators — this phase builds the
  framework and `.planning/ADDING-A-MONITOR.md` recipe only; the 7 remaining
  monitors are future phase work (tracked under existing OBS-*/REM-* IDs).
- INFRA-01–INFRA-06 (dedicated EC2 instance, Prometheus/Alertmanager,
  CloudWatch agent, dedicated monitoring Postgres, Redis deployment,
  heartbeat/watchdog) — these are deployment/ops setup tasks, not part of
  this code-architecture refactor.

</deferred>

---

*Phase: 01-infrastructure-and-observers*
*Context gathered: 2026-06-15*
