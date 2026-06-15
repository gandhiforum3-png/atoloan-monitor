---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: Executing Phase 01
last_updated: "2026-06-15T10:10:00.000Z"
progress:
  total_phases: 4
  completed_phases: 0
  total_plans: 6
  completed_plans: 6
---

# Project State — Atoloan Monitor

## Project Reference

See: .planning/PROJECT.md (updated 2026-06-03)

**Core value:** The agent detects, diagnoses, and resolves infrastructure incidents automatically — 24/7 — so engineers are only involved when human judgment is genuinely required.
**Current focus:** Phase 01 — infrastructure-and-observers

## Phase Status

| # | Phase | Status |
|---|-------|--------|
| 1 | Infrastructure and Observers | Complete (6/6 plans) — framework refactor sub-scope |
| 2 | Diagnosis and SLO Engine | Not Started |
| 3 | Safe Remediation | Not Started |
| 4 | Dashboard and Incident Docs | Not Started |

## Active Phase

**Phase 1 — Infrastructure and Observers** (6/6 plans complete — framework refactor sub-scope)

- Current plan: 01-06 complete. This is the last plan of the 6-plan framework-refactor sub-scope; the full ROADMAP Phase 1 goal ("8 observers in learning mode") is a separate, larger scope explicitly deferred per 01-06-PLAN.md's requirement_coverage table. Next step: Phase 1 verification (gsd-verifier).
- Just completed: 01-06 made scripts/run_local.py registry-driven (D-14) — it now iterates agent.registry.REGISTRY, starting one observer + one generic_orchestrator.run task per selected domain via a new parse_monitors()-validated --monitors flag (default: all registered domains; unknown domain -> sys.exit(1) with available list, T-01-17). All existing flags (--verbose/--debounce/--fix/--agent) preserved. D-16 (backoff extraction to agent/shared/backoff.py) deferred — no second observer added this phase, no consumer for the extraction yet. .planning/ADDING-A-MONITOR.md (262 lines, D-15) created — the copy-followable add-a-domain recipe naming real shared helpers, DomainConfig fields, EC2 reboot_instance / Postgres kill_query acceptance examples, and the 7 deferred observers (OBS-01, OBS-03..08). .planning/REMEDIATION-APPROACHES.md module references corrected for the post-refactor layout. Full suite green (100% pass). Task 3's blocking live-e2e checkpoint (mode=diagnoser/mode=agent against minikube+Redis+real ANTHROPIC_API_KEY) was DEFERRED by explicit operator decision — automated structural verification accepted as sufficient for this plan's sign-off; the live e2e remains an open follow-up, NOT approved.

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
- DiagnosisResult.action_type opened from a closed 4-value Literal to an open str (D-01/D-02) — removes the JSON-schema enum from the shared model so future domains can express new actions; InfraEvent.domain/severity, HumanEscalationPacket.urgency, and estimated_blast_radius all stay Literal; safety.py frozen.
- Per-domain enum re-injection in node_diagnoser: the shared DiagnosisResult carries an open str, but each diagnoser re-injects its own per-domain action enum into the tool input_schema it hands to Claude (via the new optional input_schema arg on diagnose_with_claude) — keeps the API constraint local to the domain instead of re-closing the shared model. _SYSTEM_PROMPT prose is a second layer.
- The execution threshold gate — NOT escalation routing — is the hard backstop guaranteeing a novel/unknown action never executes: _meets_threshold two-level .get default 1.0 means an unknown action can never meet the threshold, so remediate() returns threshold_not_met without acting. A high-confidence novel action passes the routing floor (no escalate_below entry -> 0.80 default), which is precisely why the threshold gate must be the backstop. (Corrected a plan assertion that claimed routing contained high-confidence novel actions; the "never executes" outcome holds, only the enforcing layer was corrected.)
- D-17: scripts/run_local.py is now registry-driven (D-14) — iterates agent.registry.REGISTRY via a TaskGroup loop, starting cfg.observer + generic_orchestrator.run per domain; new parse_monitors()-validated --monitors flag (default: all registered domains, unknown domain -> sys.exit(1) with available list, T-01-17). .planning/ADDING-A-MONITOR.md (262 lines, D-15) documents the mechanical add-a-domain recipe for the 7 deferred observers. D-16 (extract _backoff to agent/shared/backoff.py) deferred — no second observer exists yet to consume the extraction; _backoff stays in agent/observers/k8s_node_observer.py. Task 3's blocking live-e2e checkpoint (mode=diagnoser and mode=agent against minikube + Redis + a real ANTHROPIC_API_KEY) was DEFERRED by explicit operator decision, NOT approved: automated structural verification (registry wiring, --monitors plumbing, full pytest suite green, ADDING-A-MONITOR.md complete) was accepted as sufficient for this plan's sign-off, but the live mode=diagnoser/mode=agent run against minikube remains an open follow-up before the K8s paths can be claimed behavior-preserving in production.

## Performance Metrics

| Phase | Plan | Duration | Tasks | Files |
|-------|------|----------|-------|-------|
| 01 | 01 | ~8min | 2 | 9 |
| 01 | 02 | ~3min | 2 | 4 |
| 01 | 03 | ~5min | 2 | 5 |
| 01 | 04 | ~6min | 2 | 7 |
| 01 | 05 | ~6min | 2 | 6 |
| 01 | 06 | ~20min | 3 | 4 |

## Completed Phases

None yet.

## Last Session

- Stopped at: Completed 01-06-PLAN.md (final plan of the 6-plan framework-refactor sub-scope; Task 3 live-e2e checkpoint deferred by operator decision, not approved)
- Resume file: None
- Next step: Phase 1 verification (gsd-verifier)
- Timestamp: 2026-06-15T10:10:00Z

## Notes

- SDK note: the `gsd-sdk` binary on PATH is an unrelated lifecycle tool with no `query` state/roadmap handlers; STATE.md and ROADMAP.md were updated directly to match their existing structure.
