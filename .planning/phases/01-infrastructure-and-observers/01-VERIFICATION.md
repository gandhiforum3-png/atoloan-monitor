---
phase: 01-infrastructure-and-observers
verified: 2026-06-15T12:00:00Z
status: human_needed
score: 9/9 framework-refactor must-haves verified (sub-scope) — full ROADMAP Phase 1 goal NOT yet met (pre-documented, see Remaining Work)
overrides_applied: 0
human_verification:
  - test: "Live mode=diagnoser e2e: python scripts/run_local.py (minikube + Redis + real ANTHROPIC_API_KEY), inject a node-pressure event via scripts/simulate_node_pressure.py"
    expected: "Diagnosis + escalation/remediation flow completes identically to the pre-refactor pipeline (DomainConfig-driven path produces the same observable outcomes as the old hardcoded k8s_orchestrator path)"
    why_human: "Requires live K8s cluster, Redis, and a real ANTHROPIC_API_KEY — none available in this environment. Cannot be verified by static analysis or the pure-logic pytest suite."
  - test: "Live mode=agent e2e: python scripts/run_local.py --agent, inject the same event"
    expected: "Agentic tool-use loop (run_tool_loop via node_agent.run_incident) reaches finish_incident or human_escalator.escalate exactly as the pre-refactor node_agent.run_incident did, including node_deleted / NetworkUnavailable escalation"
    why_human: "Same as above — requires live K8s + Redis + ANTHROPIC_API_KEY. Explicitly deferred by operator decision in 01-06-SUMMARY.md, NOT approved."
---

# Phase 1: Infrastructure and Observers — Verification Report

**Phase Goal (full ROADMAP entry):** All 8 observers are running event-driven on live infrastructure, normalizing signals into the shared event bus, and building per-metric baselines during a mandatory 7-day learning period — the system is watching and learning but not yet alerting or acting.

**Sub-scope actually executed by 01-01..01-06 (per 01-CONTEXT.md and 01-06-PLAN.md `<requirement_coverage>`):** Generic-monitor framework refactor — generalize the existing K8s node monitor (OBS-02) into a registry-based observer/orchestrator/skill framework (D-01 through D-17), behavior-preserving, plus a documented recipe (`ADDING-A-MONITOR.md`, D-15) for the 7 deferred observers.

**Verified:** 2026-06-15
**Status:** human_needed (sub-scope structurally verified; one explicitly-deferred live e2e checkpoint remains open)
**Re-verification:** No — initial verification

---

## Part 1 — Did the framework-refactor sub-scope (D-01..D-17) actually achieve what it claimed?

### Observable Truths (sub-scope must-haves, derived from 01-CONTEXT.md decisions + 01-06-PLAN.md requirement_coverage)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `DiagnosisResult.action_type` is an open `str` (D-01/D-02); shared model schema has no enum on `action_type` | VERIFIED | `agent/shared/models.py:56` — `action_type: str = Field(description="Machine-readable action category")`, with explanatory comment block lines 48-55. `tests/test_models.py` passes (part of 72/72). |
| 2 | Per-domain enum re-injection: node_diagnoser re-injects its own `action_type` enum into its tool `input_schema` so its Claude call stays API-constrained | VERIFIED | `agent/skills/diagnosers/node_diagnoser.py:23,26-30,141` — `_K8S_ACTION_TYPES` list + `_tool_input_schema()` injects `schema["properties"]["action_type"]["enum"]`; `diagnose()` passes `input_schema=_tool_input_schema()` to `diagnose_with_claude`. `agent/shared/diagnoser_base.py:31,50-55,78-81` — `diagnose_with_claude` accepts optional `input_schema`, defaults to `result_model.model_json_schema()`. |
| 3 | `agent/shared/remediation.py` exists with `PreflightResult`, `THRESHOLDS` (domain-keyed `dict[str,dict[str,float]]`), `_meets_threshold(domain, action, confidence)` (two-level `.get` default 1.0), `_log_action`; ZERO `kubernetes_asyncio` import (D-03/D-04) | VERIFIED | `agent/shared/remediation.py` (67 lines) — full read confirms all 4 primitives present, `THRESHOLDS["k8s"]["pod_restart"]==0.80`, `_meets_threshold` at line 37-42 uses `.get(domain,{}).get(action_type,1.0)`. `grep -rn kubernetes_asyncio agent/shared/*.py` returns nothing (exit 1). |
| 4 | `node_remediator.py`/`node_agent.py` import these primitives from shared and reference domain-keyed `THRESHOLDS["k8s"][...]`; K8s-specific behavior (cooldown, PDB, replica floor, learning_mode gating) unchanged | VERIFIED | `agent/skills/remediators/node_remediator.py:25-31` imports `PreflightResult, THRESHOLDS, _meets_threshold, _log_action` from `agent.shared.remediation`; line 188 `THRESHOLDS['k8s'].get(action,1.0)`. `agent/skills/agents/node_agent.py:30,332,376` same pattern. Order safety_check→threshold→learning_mode→preflight→execute confirmed at `node_remediator.py:171-256`. |
| 5 | `agent/shared/agent_loop.py::run_tool_loop` exists, is the extracted iterate/dispatch/finish/auto-escalate loop, never inspects `ctx`, parameterized by `domain` (D-06/D-07); ZERO `kubernetes_asyncio` | VERIFIED | `agent/shared/agent_loop.py` (178 lines) — full read confirms loop body (iteration, `dispatch_tool(name, input, ctx)`, `finish_incident` handling, no-tool-call break, post-loop auto-escalation via `human_escalator.escalate(..., domain=domain, ...)`). `node_agent.py` retains `_SYSTEM_PROMPT`, `TOOLS`, `_format_bundle`, `_dispatch_tool`, thin `run_incident`. No `kubernetes_asyncio` in agent_loop.py (grep confirmed). |
| 6 | `agent/shared/diagnoser_base.py::diagnose_with_claude(client, system_prompt, user_text, result_model=DiagnosisResult, ...)` exists, is the extracted cached-prompt + forced-tool_choice + parse boilerplate (D-08/D-09); `node_diagnoser.py` keeps only `_SYSTEM_PROMPT` + `_format_bundle` + thin `diagnose` | VERIFIED | `agent/shared/diagnoser_base.py` (105 lines) — full read confirms `messages.create` with `cache_control`, forced `tool_choice`, token-usage logging via `getattr`, tool_use parse/`model_validate`. `node_diagnoser.py:116-142` is a thin delegate (`tool_choice={` count is 0 in node_diagnoser.py — confirmed by absence in the file). |
| 7 | `agent/registry.py::DomainConfig` dataclass + `REGISTRY` dict + `register()` exists (D-10); imports nothing from `agent.skills`/`agent.observers`, no K8s client | VERIFIED | `agent/registry.py` (59 lines) — `DomainConfig` has all fields from D-10 plus `observer` (the documented A2 addition) and optional `context_fetcher`. Imports limited to `dataclasses`/`typing`. `REGISTRY: dict[str, DomainConfig] = {}` + `register()`. |
| 8 | `agent/orchestrators/generic_orchestrator.py::run(redis, anthropic_client, config, *, debounce_seconds, learning_mode, mode)` drives the consume/debounce/bundle/dispatch loop for ANY `DomainConfig` (D-11); `k8s_orchestrator.py` shrunk to `_fetch_pods_on_nodes` + k8s `DomainConfig` registration (D-12); no K8s client in generic_orchestrator | VERIFIED | `agent/orchestrators/generic_orchestrator.py` (233 lines) — `_should_escalate`, `_decode_event`, `_handle_bundle`, `run()` all config-driven via `config.stream/consumer_group/domain/diagnose/remediate/run_incident/context_fetcher/escalate_below/urgency_map`. `k8s_orchestrator.py` (98 lines) is now `_fetch_pods_on_nodes` + a single `register(DomainConfig(domain="k8s", ...))` call. `grep kubernetes_asyncio agent/orchestrators/generic_orchestrator.py` → no match; `k8s_orchestrator.py` retains the import (expected, it's the K8s-specific wiring file). |
| 9 | `human_escalator._derive_urgency(signals, urgency_map)` / `escalate(..., urgency_map)` are per-domain (D-13); K8s urgency map copied verbatim (4 entries, `node_network_unavailable`/`node_deleted` intentionally absent → `p3_within_1h` fallback preserved) | VERIFIED | `agent/skills/remediators/human_escalator.py:24-39,42-49,73` — `_derive_urgency` takes `urgency_map` arg, falls through to `"p3_within_1h"`; `escalate` defaults `urgency_map or {}`. `agent/orchestrators/k8s_orchestrator.py:92-97` — `urgency_map` has exactly the 4 original entries (`node_not_ready`, `node_memory_pressure`, `node_disk_pressure`, `node_pid_pressure`); `node_network_unavailable`/`node_deleted` absent. `tests/test_registry.py::test_k8s_urgency_map_exact_with_gap_preserved` asserts `len(cfg.urgency_map) == 4` and both gaps. |
| 10 | `scripts/run_local.py` is registry-driven (D-14): iterates `agent.registry.REGISTRY`, starts one observer + one `generic_orchestrator.run` task per domain via `TaskGroup`; `--monitors` comma-list flag, validated, `sys.exit(1)` on unknown domain (T-01-17) | VERIFIED | `scripts/run_local.py:194-217` — `for domain in monitors: cfg = REGISTRY[domain]; tg.create_task(cfg.observer(...)); tg.create_task(run_orchestrator(..., cfg, ...))`. `parse_monitors()` (lines 69-86) validates against `REGISTRY.keys()`, `sys.exit(1)` with available-domains list on unknown. Live-tested: `parse_monitors(None) == ['k8s']`, `parse_monitors('k8s') == ['k8s']`, `parse_monitors('bogus')` → `SystemExit(1)` printing `available domains: k8s`. `--help` exits 0 and shows `--monitors`. |
| 11 | `.planning/ADDING-A-MONITOR.md` (D-15) is a concrete, copy-followable recipe naming real shared helpers, `DomainConfig` fields, EC2/Postgres examples, and the 7 deferred observers | VERIFIED | 262 lines. Full read confirms: per-domain file map (Step 1-6), real helper names (`event_bus.publish`, `shared/remediation.{THRESHOLDS,_meets_threshold,_log_action,PreflightResult}`, `diagnoser_base.diagnose_with_claude`, `agent_loop.run_tool_loop`, `safety.safety_check`), explicit `safety_check → _meets_threshold → learning_mode → preflight → execute` order, `THRESHOLDS["ec2"]={"reboot_instance":0.90}` / `THRESHOLDS["db"]={"kill_query":0.95}` acceptance examples, `InfraEvent.domain` Literal-widening callout, D-15 checklist, "what NOT to touch" section. Names all 7 deferred observers (OBS-01, OBS-03..08) by ID. Not aspirational — every named function/module exists in the codebase as verified above. |

**Score: 11/11 sub-scope truths VERIFIED.**

### Full Regression Suite

```
.venv/bin/python -m pytest tests/ -q
........................................................................ [100%]
72 passed in 0.32s
```

72/72 green, confirmed by direct execution (not taken from SUMMARY claims). Test growth across plans: 36 (01-01) → 39 (01-02) → 54 (01-03) → 60 (01-04) → 67 (01-05) → 72 (01-06), matching each SUMMARY's incremental count.

### `agent/shared/safety.py` Frozen Claim

Verified via git history: `agent/shared/safety.py` appears only in the initial commit `0945e19` (pre-refactor baseline). `git diff 0945e19 HEAD -- agent/shared/safety.py` produces **empty output** — byte-identical. The file is not in the 36-file changeset between baseline and HEAD. The "frozen throughout" claim is confirmed true.

### Diff Footprint Sanity Check

`git diff 0945e19 HEAD --stat` shows 36 files changed (+2498/-607). New shared modules (`registry.py`, `generic_orchestrator.py`, `agent_loop.py`, `diagnoser_base.py`, `remediation.py`) are pure additions; `models.py` changed only 12 lines (the `action_type` field + comment block); `node_diagnoser.py`/`node_agent.py`/`node_remediator.py`/`human_escalator.py`/`k8s_orchestrator.py` shrank as claimed; `agent/observers/k8s_node_observer.py` and `agent/shared/event_bus.py` are **not in the diff at all** (untouched, as claimed — `_backoff` extraction correctly deferred per D-16).

---

## Part 2 — Does the framework genuinely "enable" the deferred observers/domains?

`.planning/ADDING-A-MONITOR.md` was read in full (262 lines). It is a **real, concrete recipe**, not aspirational hand-waving:

- Names actual files that exist today (`agent/shared/event_bus.py`, `agent/shared/remediation.py`, `agent/shared/diagnoser_base.py`, `agent/shared/agent_loop.py`, `agent/shared/safety.py`, `agent/skills/remediators/human_escalator.py`) and the exact functions verified above.
- The EC2 `reboot_instance` / Postgres `kill_query` examples are not just narrative — they map directly onto verified mechanisms: `THRESHOLDS["ec2"] = {"reboot_instance": 0.90}` slots into the verified `THRESHOLDS: dict[str, dict[str, float]]` shape (#3 above); `_meets_threshold("ec2", "reboot_instance", conf)` would use the verified two-level `.get` default (#3); a new `DomainConfig(domain="ec2", ...)` would slot into the verified `REGISTRY` dict (#7) and be auto-started by the verified `run_local.py` loop (#10) with zero runner edits.
- The "framework acceptance test" stated in 01-CONTEXT.md `<specifics>` ("could an EC2 reboot_instance or Postgres kill_query action flow through DomainConfig + generic_orchestrator + shared/remediation + human_escalator without further core changes?") is answered structurally **yes** by the chain of verified truths #1, #3, #7, #8, #9, #10 — every piece a new domain needs already exists and is config-driven, not hardcoded to k8s.
- The recipe correctly documents the one schema edit a new domain MAY need (`InfraEvent.domain` Literal widening in `models.py:11`, currently `Literal["k8s","db","infra","app"]`) — this is accurate; that Literal was NOT touched by this refactor (only `DiagnosisResult.action_type` was opened per D-01), so a genuinely new domain string would need that one-line widening. The recipe flags this correctly rather than glossing over it.

**Conclusion: the "enables deferred observers" claim is substantiated**, not aspirational. No new observer code exists (correctly — none was claimed to exist), but the structural prerequisites are real and verified.

---

## Part 3 — Honest Gap Accounting Against the FULL ROADMAP Phase 1 Goal

**The full ROADMAP.md Phase 1 goal is NOT met by this work, and this is expected/pre-documented — not a defect of plans 01-01..01-06.**

ROADMAP.md Phase 1 success criteria (1-6) status:

| # | Success Criterion | Status | Notes |
|---|---|---|---|
| 1 | Monitoring agent on dedicated 4th EC2 instance, CloudWatch heartbeat, watchdog Lambda | NOT MET (out of scope) | INFRA-01/INFRA-06 — deployment/ops, explicitly deferred per 01-CONTEXT.md `<deferred>` |
| 2 | K8s pod observer 410 Gone reconnection | NOT MET (deferred) | OBS-01 not implemented — "ENABLED-FOR-LATER" via ADDING-A-MONITOR.md, ready as a mechanical addition |
| 3 | All 8 observer skills publishing `InfraEvent` to Redis Streams from all 4 layers | NOT MET (1 of 8 observers exist) | Only OBS-02 (K8s node) is implemented (migrated, behavior-preserving). OBS-01, OBS-03..OBS-08 (7 observers) not implemented |
| 4 | Postgres observer with connection pool ≤3, `application_name='atoloan-monitor'`, reserved superuser slot | NOT MET (deferred) | OBS-05 not implemented |
| 5 | Learning mode active, contamination-flag logging for rolling updates/evictions | PARTIALLY MET | `learning_mode` gating exists and is verified intact for the K8s domain (node_remediator/node_agent); but this is only 1 of 8 domains, and the contamination-flag logic for rolling updates/evictions is not implemented anywhere in this codebase |
| 6 | 7-day learning mode → EWMA baselines → Enforcement mode transition | NOT MET (out of scope) | No baseline/EWMA/SLO infrastructure exists yet — this is Phase 2 (DIAG/SLO) territory |

**This is the pre-documented honest-accounting outcome.** 01-06-PLAN.md's `<requirement_coverage>` table and 01-CONTEXT.md's `<deferred>` section both state this explicitly and in advance — this sub-scope never claimed to satisfy ROADMAP Phase 1's success criteria. The 6 plans delivered a **framework refactor**, and that refactor is verified complete and behavior-preserving (Part 1, 11/11).

### Remaining Work (tracked, not "gaps" of 01-01..01-06)

1. **OBS-01, OBS-03..OBS-08** (7 observers: K8s pod, EC2, FastAPI, Postgres, log analyzer, security-group auditor, secrets checker) + their REM-02..REM-08 remediators — not implemented. `ADDING-A-MONITOR.md` is the ready recipe (verified concrete in Part 2).
2. **INFRA-01..INFRA-06** (dedicated EC2 instance, Prometheus/Alertmanager, CloudWatch agent, dedicated monitoring Postgres, Redis deployment, heartbeat/watchdog Lambda) — deployment/ops setup, out of scope for a code refactor.
3. **Learning-mode contamination flagging** (rolling-update / node-eviction exclusion windows for baseline computation) and **EWMA baseline + Enforcement-mode transition** (ROADMAP SC 5-6) — these belong to Phase 2 (DIAG/SLO engine) territory and have no implementation yet.
4. **Open live e2e checkpoint** (01-06 Task 3, explicitly DEFERRED by operator decision, NOT approved): `mode="diagnoser"` and `mode="agent"` live runs against minikube + Redis + real `ANTHROPIC_API_KEY` to confirm the registry-driven pipeline produces identical observable outcomes to the pre-refactor pipeline for the K8s node monitor, including `node_deleted`/`NetworkUnavailable` escalation. The Wave 0 regression suite pins **pure-logic** behavior only (72/72 green); the **live integration path** through Redis/minikube/Claude has not been exercised post-refactor.

---

## Part 4 — Genuine Defects Found (vs. pre-documented/accepted deferrals)

No genuine defects were found in the framework-refactor sub-scope itself. Two minor documentation-lag items, neither of which affects the verified code:

1. **ROADMAP.md plan-checkbox / progress table not updated for 01-06.** `.planning/ROADMAP.md` line ~32 shows `- [ ] 01-06-PLAN.md — ...` (unchecked) and the Progress table (line ~80) shows "1. Infrastructure and Observers | 5/6 | In Progress". `.planning/STATE.md` (updated, line 27) correctly says "Complete (6/6 plans)". This is a stale-documentation artifact, not a code defect — the 01-06 commits (`bef56f1`, `194c15f`) exist and the 01-06-SUMMARY.md is complete. **Recommend**: update ROADMAP.md's checkbox and progress row to 6/6 as part of phase close-out.
2. **01-VALIDATION.md frontmatter `wave_0_complete: false`** is stale (set before Wave 0 ran); Wave 0 (01-01) is in fact complete per 01-01-SUMMARY.md and the 72-test suite. Cosmetic only.

Neither item is a `must_have` failure — both are bookkeeping fields that don't affect runtime behavior or the verified truths above.

---

## Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `agent/shared/models.py` | `action_type: str` (open), siblings unchanged | VERIFIED | Line 56; `InfraEvent.domain`/`severity`, `HumanEscalationPacket.urgency`, `estimated_blast_radius` remain `Literal` |
| `agent/shared/remediation.py` | `PreflightResult`, domain-keyed `THRESHOLDS`, `_meets_threshold`, `_log_action`, no K8s import | VERIFIED | 67 lines, full read, grep-clean |
| `agent/shared/agent_loop.py` | `run_tool_loop(...)`, domain-parameterized, no K8s import | VERIFIED | 178 lines, full read, grep-clean |
| `agent/shared/diagnoser_base.py` | `diagnose_with_claude(...)`, optional `input_schema`, no K8s import | VERIFIED | 105 lines, full read, grep-clean |
| `agent/registry.py` | `DomainConfig`, `REGISTRY`, `register()`, no skill/observer/K8s import | VERIFIED | 59 lines, full read |
| `agent/orchestrators/generic_orchestrator.py` | config-driven `run()`, `_should_escalate`, no K8s import | VERIFIED | 233 lines, full read, grep-clean |
| `agent/orchestrators/k8s_orchestrator.py` | shrunk to `_fetch_pods_on_nodes` + registration | VERIFIED | 98 lines, full read |
| `agent/skills/remediators/human_escalator.py` | per-domain `urgency_map` arg | VERIFIED | full read |
| `scripts/run_local.py` | registry-driven `TaskGroup`, `--monitors` flag, `parse_monitors()` | VERIFIED | full read + live exec |
| `.planning/ADDING-A-MONITOR.md` | concrete D-15 recipe | VERIFIED | 262 lines, full read |
| `agent/shared/safety.py` | byte-frozen | VERIFIED | empty diff vs. baseline commit |
| `agent/shared/backoff.py` | NOT created (D-16 deferred) | VERIFIED (absence expected) | `ls` confirms not present; `_backoff` remains in `k8s_node_observer.py:78` |

---

## Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `scripts/run_local.py` | `agent.registry.REGISTRY` | `cfg = REGISTRY[domain]` in TaskGroup loop | WIRED | Loop iterates `monitors`, looks up `REGISTRY[domain]`, starts `cfg.observer(...)` + `run_orchestrator(..., cfg, ...)` |
| `agent.orchestrators.k8s_orchestrator` (import side-effect) | `agent.registry.REGISTRY` | `register(DomainConfig(domain="k8s", ...))` at import time | WIRED | `run_local.py:50` imports the module solely for this side-effect; `REGISTRY["k8s"]` populated, confirmed by live exec |
| `generic_orchestrator.run` | `config.diagnose` / `config.remediate` / `config.run_incident` / `config.context_fetcher` | direct calls in `_handle_bundle` | WIRED | `generic_orchestrator.py:104-137`; for k8s these resolve to `node_diagnoser.diagnose`, `node_remediator.remediate`, `node_agent.run_incident`, `_fetch_pods_on_nodes` (confirmed via live `REGISTRY['k8s']` introspection) |
| `node_diagnoser.diagnose` | `diagnoser_base.diagnose_with_claude` | `return await diagnose_with_claude(..., input_schema=_tool_input_schema())` | WIRED | `node_diagnoser.py:136-142` |
| `node_agent.run_incident` | `agent_loop.run_tool_loop` | `return await run_tool_loop(..., domain="k8s", ...)` | WIRED | per 01-03-SUMMARY.md and confirmed by `node_agent.py` imports (`from agent.shared.agent_loop import run_tool_loop`) |
| `node_remediator.remediate` / `node_agent` tools | `shared/remediation.{THRESHOLDS,_meets_threshold,_log_action}` | imports + domain-keyed calls | WIRED | `node_remediator.py:25-31,188`; `node_agent.py:30,332,376` |
| `generic_orchestrator._should_escalate` / `human_escalator.escalate` | `config.escalate_below` / `config.urgency_map` | passed as args | WIRED | `generic_orchestrator.py:119,128`; `human_escalator.py:42-49,73` |

---

## Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full regression suite green | `.venv/bin/python -m pytest tests/ -q` | `72 passed in 0.32s` | PASS |
| `safety.py` byte-frozen since baseline | `git diff 0945e19 HEAD -- agent/shared/safety.py` | empty output | PASS |
| `--help` shows `--monitors`, exits 0 | `.venv/bin/python scripts/run_local.py --help` | exit 0, `--monitors MONITORS` shown | PASS |
| `parse_monitors` default → all registered | `parse_monitors(None)` | `['k8s']` | PASS |
| `parse_monitors` unknown domain → exit 1 with list | `parse_monitors('bogus')` | `SystemExit(1)`, prints `available domains: k8s` | PASS |
| Registry fully wired for k8s | `REGISTRY['k8s'].{observer,diagnose,remediate,run_incident,context_fetcher}` | all 5 resolve to real functions (`watch_nodes`, `node_diagnoser.diagnose`, `node_remediator.remediate`, `node_agent.run_incident`, `_fetch_pods_on_nodes`) | PASS |
| No `kubernetes_asyncio` in `agent/shared/*` | `grep -rn kubernetes_asyncio agent/shared/*.py` | no matches (exit 1) | PASS |

---

## Probe Execution

No `scripts/*/tests/probe-*.sh` files exist in this repo (Python project, pytest-based). No phase documentation references probe scripts. SKIPPED — not applicable to this phase.

---

## Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|---|---|---|---|---|
| OBS-02 | 01-01..01-06 | K8s node observer (node conditions) | SATISFIED | Migrated onto framework, behavior-preserving; `REQUIREMENTS.md` marked `[x]` and `Complete (01-01)` |
| OBS-09 | 01-01..01-06 | InfraEvent normalization | SATISFIED | `InfraEvent` schema unchanged; `event_bus.publish` unchanged; `REQUIREMENTS.md` marked `[x]` |
| OBS-01, OBS-03..OBS-08 | (declared deferred in 01-06-PLAN.md) | 7 remaining observers | NOT IMPLEMENTED (pre-documented deferral) | `ADDING-A-MONITOR.md` recipe verified concrete; `REQUIREMENTS.md` still `Pending` for all 7 |
| INFRA-01..INFRA-06 | (declared out-of-scope in 01-CONTEXT.md) | EC2/Prometheus/CloudWatch/monitoring-Postgres/Redis/heartbeat | NOT IMPLEMENTED (pre-documented out-of-scope) | `REQUIREMENTS.md` still `Pending` for all 6 |

**No orphaned requirements**: `.planning/REQUIREMENTS.md`'s Phase 1 rows (INFRA-01..06, OBS-01..09) all map either to a "Complete" status (OBS-02, OBS-09) or are explicitly named in 01-06-PLAN.md's `<requirement_coverage>` table as deferred/enabled-for-later/out-of-scope. Nothing is silently missing from the accounting.

---

## Anti-Patterns Found

None. Scanned all 12 phase-modified core files (`agent/registry.py`, `agent/orchestrators/{generic_orchestrator,k8s_orchestrator}.py`, `agent/shared/{remediation,agent_loop,diagnoser_base,models}.py`, `agent/skills/diagnosers/node_diagnoser.py`, `agent/skills/agents/node_agent.py`, `agent/skills/remediators/{node_remediator,human_escalator}.py`, `scripts/run_local.py`) for `TBD|FIXME|XXX|TODO|HACK|PLACEHOLDER|not yet implemented|not implemented|coming soon` (case-insensitive) — zero matches. No debt markers requiring the debt-marker gate.

---

## Human Verification Required

### 1. Live `mode=diagnoser` end-to-end run

**Test:** Run `python scripts/run_local.py` against minikube + Redis (`docker run -d --name atoloan-redis -p 6379:6379 redis:7-alpine`) with a real `ANTHROPIC_API_KEY` set; inject a node-pressure event via `scripts/simulate_node_pressure.py`.
**Expected:** The registry-driven pipeline (observer → `events:k8s` → `generic_orchestrator.run` → `node_diagnoser.diagnose` → escalate/remediate) produces the same observable diagnosis + remediation/escalation outcome as the pre-refactor hardcoded `k8s_orchestrator.run` pipeline.
**Why human:** Requires a live K8s cluster, Redis, and a real Anthropic API key — none available in this sandboxed environment. This is exactly 01-06 Task 3, which was explicitly DEFERRED by operator decision (not approved, not rejected) per 01-06-SUMMARY.md.

### 2. Live `mode=agent` end-to-end run, including `node_deleted`/`NetworkUnavailable` escalation

**Test:** Run `python scripts/run_local.py --agent` against the same live setup; inject the same event types, including a simulated `node_deleted` and `NetworkUnavailable` condition.
**Expected:** `run_tool_loop` (via `node_agent.run_incident`) reaches `finish_incident` or auto-escalates via `human_escalator.escalate(domain="k8s", ...)` exactly as the pre-refactor `node_agent.run_incident` did; `node_deleted`/`NetworkUnavailable` events fall through to `p3_within_1h` urgency as before (per the 4-entry `urgency_map` verified in this report).
**Why human:** Same infrastructure dependency as above. The Wave 0 regression suite (72/72 green) pins **pure-logic** behavior (threshold gating, routing, urgency derivation, safety floor, schema) — it does NOT exercise the live Claude API call, Redis Streams I/O, or K8s Watch API integration. This live integration path is the one piece of the "behavior-preserving" claim that remains genuinely unverified.

---

## Gaps Summary

**No gaps in the framework-refactor sub-scope** (Part 1: 11/11 truths verified, 72/72 tests green, safety.py byte-frozen, no anti-patterns). The sub-scope's own deliverables (D-01 through D-17) are real, substantive, and correctly wired — not stubs, not placeholders.

**The full ROADMAP Phase 1 goal is not met**, but this was pre-documented as out-of-scope for this 6-plan round before execution began (01-CONTEXT.md `<deferred>`, 01-RESEARCH.md "Requirement-coverage honesty note", 01-06-PLAN.md `<requirement_coverage>`). This is recorded here as **Remaining Work** (Part 3), not as a gap of plans 01-01..01-06.

**One item is genuinely open and requires a human decision**: the live `mode=diagnoser`/`mode=agent` e2e checkpoint (01-06 Task 3) was explicitly deferred by the operator due to missing `ANTHROPIC_API_KEY`/minikube, and is surfaced above as `human_verification`. This is why overall status is `human_needed` rather than `passed` — per the verification process, identified human-verification items take priority over an otherwise-clean score.

**Recommended next steps:**
1. Update `.planning/ROADMAP.md`'s 01-06 checkbox and the Phase 1 progress row to 6/6 (minor doc lag, Part 4 item 1).
2. When `ANTHROPIC_API_KEY` + minikube + Redis become available, execute the two human-verification items above to close the open e2e follow-up before claiming the K8s paths fully behavior-preserving in production.
3. Plan the next phase(s) of work for OBS-01/OBS-03..08 (using `ADDING-A-MONITOR.md` as the recipe) and for INFRA-01..06 (deployment/ops), as tracked in `.planning/REQUIREMENTS.md`.

---

*Verified: 2026-06-15*
*Verifier: Claude (gsd-verifier)*
