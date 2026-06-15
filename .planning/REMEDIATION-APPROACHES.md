# K8s Node Remediation — Two Approaches

> For new engineers. Both approaches are implemented and runnable today via
> `scripts/run_local.py`. Every component referenced here is real — sourced
> directly from the codebase.

There are two ways the orchestrator can turn a node-condition signal into a
diagnosis and (optionally) an action. They share the same front half of the
pipeline and diverge at the diagnosis/remediation stage.

> **Post-refactor module layout (Phase 1, D-01..D-16).** The generic
> consume/debounce/bundle/dispatch loop now lives in
> `agent/orchestrators/generic_orchestrator.py::run(redis, client, config, *,
> debounce_seconds, learning_mode, mode)` and is driven by a `DomainConfig` from
> `agent/registry.py`. `agent/orchestrators/k8s_orchestrator.py` now keeps only
> `_fetch_pods_on_nodes` (the k8s context fetcher) + the single k8s `DomainConfig`
> registration. The shared primitives moved to
> `agent/shared/remediation.py` (`THRESHOLDS`, `_meets_threshold`, `_log_action`,
> `PreflightResult`), `agent/shared/diagnoser_base.py` (`diagnose_with_claude`),
> and `agent/shared/agent_loop.py` (`run_tool_loop`). Both `mode="diagnoser"`
> (default) and `mode="agent"` are selected the same way as before.

Switch between them with `mode="diagnoser"` (default) or `mode="agent"` passed to
`agent/orchestrators/generic_orchestrator.py::run()` (via the k8s `DomainConfig`),
or via `scripts/run_local.py --agent`. The runner is registry-driven: it iterates
`agent.registry.REGISTRY` and starts one observer + one generic-orchestrator per
domain (use `--monitors k8s` to run only the k8s domain).

---

## Shared Front Half (identical for both)

```
┌───────────────────────────┐
│ agent/observers/          │   Watches K8s node conditions via the
│ k8s_node_observer.py      │   K8s watch API (MemoryPressure,
│ watch_nodes()             │   DiskPressure, PIDPressure,
│                           │   NetworkUnavailable, Ready). Also
│                           │   watches for DELETED events (node
│                           │   removed from the API entirely).
└─────────────┬─────────────┘
              │ condition becomes "True" (a problem),
              │ or node disappears from the API
              ▼
┌───────────────────────────┐
│ agent/shared/event_bus.py │   publish(InfraEvent) → Redis Stream
│ publish()                 │   "events:k8s"
└─────────────┬─────────────┘
              ▼
┌───────────────────────────┐
│ agent/orchestrators/      │   Consumer group reads events:k8s,
│ generic_orchestrator.py   │   collects them for `debounce_seconds`
│ run(config) → debounce    │   (5s locally, 30s prod) into a
│ loop                      │   SignalBundle. Driven by the k8s
└─────────────┬─────────────┘   DomainConfig from agent/registry.py.
              │ window elapses
              ▼
     _handle_bundle(bundle, mode=...)
              │
     ┌────────┴────────┐
     │                 │
mode="diagnoser"   mode="agent"
  (default)           (new)
```

At this point, both approaches have a `SignalBundle` — a list of
`InfraEvent`s (e.g. "node `minikube` has `MemoryPressure=True`, reason
`KubeletHasInsufficientMemory`") collected over the debounce window.

---

## Node Failure Coverage (recent additions)

The observer and both system prompts now also cover two cases that have
**no pod-level fix** — they exist purely so Claude routes them straight to
`human_escalate` / `escalate_human` instead of guessing at a `pod_restart` or
`deployment_scale_down`:

- **`NetworkUnavailable=True`** — added to `_TRUE_MEANS_PROBLEM`,
  `_CONDITION_EVENT_TYPE` (`node_network_unavailable`), and
  `_CONDITION_SEVERITY` (`critical`) in `k8s_node_observer.py`. The node's
  network isn't configured correctly; restarting or scaling a deployment on
  that node won't help.
- **`node_deleted`** — emitted by the new `_emit_node_deleted()` when the
  watch API reports a `DELETED` event for a node (the node object vanished
  from the K8s API: terminated, crashed, or deregistered). The synthetic
  `raw_payload` uses `condition_type="NodeDeleted"`, `status="True"`,
  `reason="NodeRemovedFromAPI"` — same shape both `_format_bundle()` helpers
  already expect, so no changes were needed there.

Both `node_diagnoser.py` and `node_agent.py` system prompts now spell out in
their "## Node Conditions Reference" section that:
- `NetworkUnavailable=True` has no pod-level fix — escalate.
- `node_deleted` is **not** a `pod_restart`/`scale_down` situation. Since
  drain/cordon/replace are forbidden operations (see
  `agent/shared/safety.py::FORBIDDEN_OPERATIONS`), the only correct response
  is human escalation with `estimated_blast_radius >= "cluster"`.

This does **not** add new remediation actions — it closes a gap where these
two signals previously had no detection (`NetworkUnavailable`) or were
silently dropped (`DELETED` watch events), which could leave Claude diagnosing
a node failure from incomplete signals or never being told about it at all.

---

## Approach 1: Diagnoser + Remediator (`mode="diagnoser"`)

**Mental model:** Claude is consulted once, like a doctor giving a diagnosis
on paper. Python then carries out (or doesn't carry out) the prescription.

```
1. _fetch_pods_on_nodes(node_names)
   → Python queries the K8s API directly: "what pods are running on
     this node, and which deployment do they belong to?"
   → Result: a list like
     [{namespace: "atoloan-backend-prod", deployment: "atoloan-api",
       pod_name: "...", phase: "Running", memory_request: "512Mi"}, ...]

2. node_diagnoser.diagnose(client, bundle, pods)
   → ONE Claude API call. The prompt contains:
       - Static system prompt: Atoloan's infra topology, what each
         node condition means, confidence calibration rules
         (cached via prompt caching — cheap on repeat calls)
       - User message: the signals from step 0 + the pod list from
         step 1, formatted as text
   → Claude is forced (tool_choice) to call `submit_diagnosis` with a
     structured DiagnosisResult:
       {
         root_cause: "...",
         affected_components: ["atoloan-backend-prod/atoloan-api"],
         confidence: 0.87,
         action_type: "pod_restart",   # or scale_down / escalate / observe
         recommended_action: "...",
         requires_human_review: false,
         estimated_blast_radius: "service"
       }
   → Claude does NOT get to look at anything else. Whatever it sees
     in this one message is all it ever sees.

3. _should_escalate(diagnosis)
   → Pure Python decision, no Claude involved:
       - action_type == "human_escalate"?        → escalate
       - requires_human_review == true?           → escalate
       - confidence < threshold for this action?  → escalate
         (0.80 for pod_restart, 0.85 for scale_down)
       - otherwise                                 → proceed to remediator

4a. ESCALATE → human_escalator.escalate()
    → Publishes a HumanEscalationPacket to "escalations:k8s"
    → Also logs to "actions:log" with status="escalated"
    → A human sees it on the dashboard / tail output

4b. PROCEED → node_remediator.remediate()
    → safety_check(action, target)   — hard stop, cannot be bypassed
    → if action == "observe_only"    — log and stop
    → if confidence below threshold  — log "threshold_not_met", stop
    → if learning_mode=True (default)
          → log "WOULD EXECUTE ..." to actions:log, DOES NOT touch K8s
    → if learning_mode=False (--fix)
          → _parse_target("atoloan-backend-prod/atoloan-api")
            → (namespace="atoloan-backend-prod", deployment="atoloan-api")
          → pre-flight checks:
              pod_restart: 300s cooldown check, available_replicas >= 1,
                           PodDisruptionBudget check
              scale_down:  replicas >= 2, PodDisruptionBudget check
          → if pre-flight fails → log "preflight_failed", stop
          → if pre-flight passes → EXECUTE:
              pod_restart  → patch deployment's pod template annotation
                             `kubectl.kubernetes.io/restartedAt` (rolling
                             restart — never delete_pod, which is forbidden)
              scale_down   → patch spec.replicas to max(1, current-1)
          → log "executed" to actions:log
```

**Total Claude calls per incident: 1.** Cheap, fast, predictable. But Claude
is "blind" after that one call — if its diagnosis was based on stale or
incomplete pod data, there's no way for it to double-check or adjust.

---

## Approach 2: Agentic Tool-Use Loop (`mode="agent"`)

**Mental model:** Claude is the on-call engineer with terminal access. It
SSHes in, looks around, tries something, checks if it worked, and either
reports "fixed" or pages a human — all in one sitting.

Implemented in `agent/skills/agents/node_agent.py`.

```
1. node_agent.run_incident(bundle, incident_id, learning_mode)
   → Skips _fetch_pods_on_nodes and node_diagnoser entirely.
   → Opens ONE K8s API client for the whole incident (core_v1, apps_v1,
     policy_v1) — shared across every tool call below.
   → Initial message to Claude = _format_bundle(bundle):
       just the raw signals (node, condition, reason, message text).
       NO pod context is pre-fetched — Claude has to go get it.

2. LOOP (up to MAX_ITERATIONS=6 rounds). Each round:
   → Send conversation history + the 7 available tools to Claude
     (tools described below)
   → Claude responds with some combination of:
       - text (its reasoning, shown in the transcript)
       - one or more tool_use calls

3. Available tools Claude can call, in any order it chooses:

   ┌─ INVESTIGATE ──────────────────────────────────────────────┐
   │ get_node_status(node_name)                                 │
   │   → live re-read of node conditions from the K8s API       │
   │     (not just what the observer reported — the CURRENT     │
   │     state, which may have already changed)                 │
   │                                                            │
   │ get_pods_on_node(node_name)                                │
   │   → list of pods + their namespace/deployment/phase/       │
   │     memory_request on that node                            │
   └────────────────────────────────────────────────────────────┘

   ┌─ ACT ─────────────────────────────────────────────────────────┐
   │ restart_deployment(namespace, deployment, confidence,         │
   │                     reasoning)                                │
   │ scale_down_deployment(namespace, deployment, confidence,      │
   │                        reasoning)                             │
   │   → Claude supplies its OWN confidence + reasoning for THIS   │
   │     specific action (not one global confidence for the        │
   │     whole incident)                                           │
   │   → Each call independently runs:                             │
   │       safety_check() → confidence >= threshold (0.80/0.85)?   │
   │       → learning_mode check → pre-flight (cooldown/PDB/       │
   │         replicas) → execute → log to actions:log              │
   │   → Returns a result dict to Claude, e.g.                     │
   │       {"status": "preflight_failed",                          │
   │        "detail": "cooldown active (45s < 300s)"}              │
   │     Claude SEES this and can change its plan (e.g. escalate   │
   │     instead, since restart is on cooldown)                    │
   └───────────────────────────────────────────────────────────────┘

   ┌─ VERIFY ──────────────────────────────────────────────────────┐
   │ verify_pod_healthy(namespace, deployment)                     │
   │   → re-reads the deployment after an action: ready vs.        │
   │     desired replicas, each pod's phase and restart count      │
   │   → lets Claude confirm "did that actually fix it?" before    │
   │     declaring success                                         │
   └───────────────────────────────────────────────────────────────┘

   ┌─ ESCALATE / FINISH ───────────────────────────────────────────┐
   │ escalate_human(root_cause, confidence, recommended_action,    │
   │                 why_escalated, ...)                           │
   │   → builds a DiagnosisResult and calls the SAME               │
   │     human_escalator.escalate() used by Approach 1             │
   │                                                               │
   │ finish_incident(outcome, summary)                             │
   │   → MUST be called last. outcome ∈ {resolved, escalated,      │
   │     no_action_needed, unresolved}. Ends the loop.             │
   └───────────────────────────────────────────────────────────────┘

4. Every tool call (investigate, act, verify, escalate) is logged to
   "actions:log" as it happens — so you get a full trace of Claude's
   investigation, not just a final verdict.

5. Loop exit conditions:
   a. Claude calls finish_incident → return its `outcome`
   b. Claude responds with no tool call at all (just text, forgot to
      finish) → loop breaks, falls through to (c)
   c. MAX_ITERATIONS rounds pass without finish_incident → Python
      auto-escalates via human_escalator (same packet format as
      Approach 1), returns "max_iterations_reached"
```

**Total Claude calls per incident: variable (1–6).** More expensive and
slower, but Claude can react to what it finds — e.g. discover the restart is
on cooldown and escalate instead, or restart and then verify the pod actually
came back healthy before calling it done.

---

## Example Walkthrough — Same Incident, Both Ways

Signal: `node=minikube  MemoryPressure=True  reason=KubeletHasInsufficientMemory`

**Diagnoser path:**
1. Python fetches pods on `minikube` → finds `atoloan-test/test-api` (2 replicas)
2. One Claude call → `{action_type: "deployment_scale_down", confidence: 0.88, affected_components: ["atoloan-test/test-api"]}`
3. 0.88 ≥ 0.85 → goes to remediator
4. Remediator: preflight passes (2 replicas, no PDB) → scales to 1 → logs "executed"

**Agent path:**
1. Claude calls `get_node_status("minikube")` → confirms MemoryPressure=True
2. Claude calls `get_pods_on_node("minikube")` → sees `atoloan-test/test-api` using the most memory
3. Claude calls `scale_down_deployment("atoloan-test", "test-api", confidence=0.88, reasoning="...")` → preflight passes → scales 2→1 → returns `{"status": "executed", "replicas": 1}`
4. Claude calls `verify_pod_healthy("atoloan-test", "test-api")` → `{ready_replicas: 1, desired_replicas: 1, healthy: true}`
5. Claude calls `finish_incident(outcome="resolved", summary="Scaled test-api 2→1, node memory pressure should clear, pod is healthy")`

Same end result here — but if step 3 had returned `preflight_failed` (e.g.
cooldown active), the agent could have pivoted to `escalate_human(...)` in
the same incident, whereas the diagnoser path would have just logged
`preflight_failed` and stopped (no chance to try something else).

---

## Side-by-Side Summary

| | Diagnoser (`mode="diagnoser"`) | Agent (`mode="agent"`) |
|---|---|---|
| Claude calls per incident | 1 (fixed) | 1–6 (variable) |
| Context Claude sees | Pre-fetched by Python, fixed snapshot | Claude fetches it live, can re-check |
| Decision point | One confidence score for the whole incident | Per-action confidence, can adapt mid-incident |
| Can verify its own fix | No | Yes (`verify_pod_healthy`) |
| Can recover from a failed pre-flight | No — logs and stops | Yes — can try a different action or escalate |
| Safety checks | `safety_check` + thresholds + pre-flight in `node_remediator.remediate()` | Identical checks, same imported functions, run inside each tool |
| Cost/latency | Lower, predictable | Higher, variable |
| Audit trail | Final diagnosis + final action | Every tool call logged as it happens |

---

## Try It Yourself

Both are runnable today via `scripts/run_local.py`:

```bash
# Diagnoser (default), learning mode — logs "would execute" only
.venv/bin/python3 scripts/run_local.py

# Agent loop, learning mode
.venv/bin/python3 scripts/run_local.py --agent

# Agent loop, real execution against minikube
.venv/bin/python3 scripts/run_local.py --agent --fix

# In another terminal, trigger a node condition:
.venv/bin/python3 scripts/simulate_node_pressure.py cycle
```

Compare the `actions:log` output between runs to see the difference in
investigation depth and the number/type of log entries each approach
produces for the same trigger.
