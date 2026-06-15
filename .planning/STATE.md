---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: Executing Phase 01
last_updated: "2026-06-15T09:00:00.000Z"
progress:
  total_phases: 4
  completed_phases: 0
  total_plans: 6
  completed_plans: 4
---

# Project State — Atoloan Monitor

## Project Reference

See: .planning/PROJECT.md (updated 2026-06-03)

**Core value:** The agent detects, diagnoses, and resolves infrastructure incidents automatically — 24/7 — so engineers are only involved when human judgment is genuinely required.
**Current focus:** Phase 01 — infrastructure-and-observers

## Phase Status

| # | Phase | Status |
|---|-------|--------|
| 1 | Infrastructure and Observers | In Progress (4/6 plans) |
| 2 | Diagnosis and SLO Engine | Not Started |
| 3 | Safe Remediation | Not Started |
| 4 | Dashboard and Incident Docs | Not Started |

## Active Phase

**Phase 1 — Infrastructure and Observers** (4/6 plans complete)

- Current plan: 01-05 (next — D-01/D-02: open action_type to str + safety-floor regression, checkpoint)
- Just completed: 01-04 built the DomainConfig registry (agent/registry.py: dataclass + REGISTRY + register(), no skill/observer/K8s imports) and a fully config-driven agent/orchestrators/generic_orchestrator.py (run drives consume/debounce/bundle/dispatch for any DomainConfig; _should_escalate takes config.escalate_below; no K8s client import). k8s_orchestrator shrank to _fetch_pods_on_nodes + a single register(DomainConfig(domain="k8s", ...)). human_escalator._derive_urgency/escalate now take a per-domain urgency_map (k8s map verbatim, p3 fall-through + node_network_unavailable gap preserved). Added tests/test_registry.py; full suite green (60 passed)

## Decisions

- Wave 0 regression-net-first: capture current behavior before any extraction so each later move (D-03..D-16) can be proven equivalent.
- Test deps isolated to requirements-dev.txt; runtime requirements-monitor.txt stays test-free.
- pyproject.toml kept minimal (only [tool.pytest.ini_options]) so agent/ stays sys.path-imported, not pip-installed.
- No fake-redis fixture in Wave 0 — baseline tests exercise only pure sync functions.
- shared/ modules carry NO domain-specific (K8s API) imports — enforced by a `grep -L kubernetes_asyncio` gate on agent/shared/remediation.py.
- THRESHOLDS reshaped to per-domain nested dict; `_meets_threshold(domain, action, confidence)` two-level `.get` default keeps unknown domain/action -> never met (T-01-03).
- Claude control flow split into two shared modules: diagnose_with_claude (one-shot) + run_tool_loop (agentic). run_tool_loop never inspects ctx (only threads it into dispatch_tool) so K8s clients stay out of shared (T-01-06); auto-escalation parameterized by domain, not hardcoded k8s (T-01-07).
- diagnoser_base.diagnose_with_claude logs result fields via getattr so the generic call works for any result_model, not only DiagnosisResult.
- DomainConfig registry is the single wiring point for a domain; registry.py imports nothing from agent.skills/agent.observers (consumed BY orchestrators) to keep the dependency one-directional and avoid a circular import.
- Generic orchestrator is config-driven and K8s-client-free; domain-specific context fetch (K8s pod listing) injected via DomainConfig.context_fetcher (T-01-12). DomainConfig carries an observer field now for the D-14 runner even though 01-04 doesn't consume it.
- escalate_below (routing floors, 1.1 sentinels) kept SEPARATE from THRESHOLDS (execution gate, 0.00 sentinels) on DomainConfig — intentionally not merged (T-01-09). Per-domain urgency_map travels on the config; _derive_urgency/escalate default an absent map to {} -> p3_within_1h.

## Performance Metrics

| Phase | Plan | Duration | Tasks | Files |
|-------|------|----------|-------|-------|
| 01 | 01 | ~8min | 2 | 9 |
| 01 | 02 | ~3min | 2 | 4 |
| 01 | 03 | ~5min | 2 | 5 |
| 01 | 04 | ~6min | 2 | 7 |

## Completed Phases

None yet.

## Last Session

- Stopped at: Completed 01-04-PLAN.md
- Resume file: None
- Timestamp: 2026-06-15T09:00:00Z

## Notes

- SDK note: the `gsd-sdk` binary on PATH is an unrelated lifecycle tool with no `query` state/roadmap handlers; STATE.md and ROADMAP.md were updated directly to match their existing structure.
