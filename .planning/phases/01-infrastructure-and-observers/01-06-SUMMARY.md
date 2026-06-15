---
phase: 01-infrastructure-and-observers
plan: 06
subsystem: infra
tags: [registry, runner, cli, documentation, framework-refactor]

# Dependency graph
requires:
  - phase: 01-05
    provides: "DiagnosisResult.action_type open str (D-01/D-02); DomainConfig registry + generic_orchestrator + per-domain urgency_map (01-04); full Wave 0 regression net green"
provides:
  - "registry-driven scripts/run_local.py: iterates agent.registry.REGISTRY to start one observer + one generic_orchestrator task per selected domain (D-14)"
  - "--monitors comma-list CLI flag with parse_monitors() validating requested domains against REGISTRY, sys.exit(1) on unknown domain (T-01-17)"
  - ".planning/ADDING-A-MONITOR.md (262 lines): the copy-followable add-a-domain recipe naming real shared helpers, DomainConfig fields, EC2/Postgres acceptance examples, and the 7 deferred observers (D-15)"
  - ".planning/REMEDIATION-APPROACHES.md module references corrected for the post-refactor layout (generic_orchestrator, agent/shared/*, k8s_orchestrator fetcher-only)"
affects: [02-diagnosis-and-slo-engine, 03-safe-remediation]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Registry-driven launcher: run_local.py no longer hardcodes a domain — it loops `for domain in monitors: cfg = REGISTRY[domain]` and starts cfg.observer + generic_orchestrator.run from the same DomainConfig, so observer and orchestrator always agree on stream/consumer_group (T-01-18)"
    - "CLI validation against a live registry: parse_monitors() resolves --monitors against REGISTRY.keys() at startup and fails loud (sys.exit(1) with the available-domains list) rather than silently no-oping on a typo (T-01-17)"
    - "Add-a-monitor is documented as a mechanical recipe (ADDING-A-MONITOR.md): 1 observer + 1 diagnoser + 1 remediator + optional 1 agent + 1 DomainConfig registration, reusing event_bus.publish, shared/remediation, diagnoser_base.diagnose_with_claude, agent_loop.run_tool_loop, safety.safety_check"

key-files:
  created:
    - .planning/ADDING-A-MONITOR.md
  modified:
    - scripts/run_local.py
    - tests/test_registry.py
    - .planning/REMEDIATION-APPROACHES.md

key-decisions:
  - "D-16 (backoff extraction to agent/shared/backoff.py) deferred — no second observer is added this phase, so the extraction has no consumer yet; _backoff stays in agent/observers/k8s_node_observer.py unchanged. agent/shared/backoff.py was NOT created."
  - "k8s dev tailers (_tail_events/_tail_escalations/_tail_actions) left hardcoded to events:k8s/escalations:k8s — k8s remains the only registered domain this phase, so generalizing the tailers has no observable benefit yet; commented as k8s-scoped dev tailers."
  - "Task 3 (blocking human-verify checkpoint: live mode=diagnoser and mode=agent e2e against minikube + Redis + real ANTHROPIC_API_KEY) was DEFERRED by explicit operator decision, not approved. Automated structural verification (imports, registry wiring via tests/test_registry.py, --help showing --monitors, full pytest suite green, ADDING-A-MONITOR.md recipe complete) was accepted as sufficient evidence for Phase 1 framework-refactor sign-off. The live e2e run remains an open follow-up."

patterns-established:
  - "Registry-driven runner pattern: any future domain registered via DomainConfig auto-starts in run_local.py with zero runner code changes — adding a monitor means adding a DomainConfig + importing its wiring module, not editing scripts/run_local.py"
  - "parse_monitors()-style validation: CLI flags that select from a dynamic registry should resolve and validate against that registry at parse time, failing loud with the valid-options list on mismatch"

requirements-completed: [OBS-02, OBS-09]

# Metrics
duration: ~20min
completed: 2026-06-15
---

# Phase 1 Plan 06: Registry-Driven Runner + Adding-a-Monitor Recipe Summary

**scripts/run_local.py is now registry-driven with a validated --monitors flag, and .planning/ADDING-A-MONITOR.md (262 lines) documents the mechanical recipe for the 7 deferred observers — completing the D-14/D-15 framework-refactor deliverables of Phase 1's 6-plan sub-scope, with the live mode=diagnoser/mode=agent e2e checkpoint deferred (not approved) pending an ANTHROPIC_API_KEY.**

## Performance

- **Duration:** ~20 min (Task 1: 09:33:47Z, Task 2: 09:53:01Z, plus finalization)
- **Started:** 2026-06-15T09:08:00Z (handoff from 01-05)
- **Completed:** 2026-06-15T10:10:00Z (approx, finalization)
- **Tasks:** 3 (2 auto, 1 blocking human-verify checkpoint — DEFERRED, not approved)
- **Files modified:** 4 (scripts/run_local.py, tests/test_registry.py, .planning/ADDING-A-MONITOR.md [created], .planning/REMEDIATION-APPROACHES.md)

## Accomplishments

- `scripts/run_local.py` no longer hardcodes the K8s domain. It imports `agent.registry.REGISTRY` and `agent.orchestrators.generic_orchestrator.run`, and the `agent.orchestrators.k8s_orchestrator` import now exists solely to trigger `register()` of the k8s `DomainConfig`. The `TaskGroup` loop iterates the selected domains, starting `cfg.observer(redis, verbose=verbose)` and `run_orchestrator(redis, anthropic_client, cfg, debounce_seconds=..., learning_mode=..., mode=...)` per domain — proving the framework acceptance criterion that a new domain auto-wires from one `DomainConfig`.
- New `--monitors` CLI flag (comma-separated domain list, default = all registered domains) is validated via a `parse_monitors()` helper against `REGISTRY.keys()`; an unknown domain prints the available-domains list and exits 1 (T-01-17). `tests/test_registry.py` gained unit tests for `parse_monitors("k8s") -> ["k8s"]` and `parse_monitors(None) -> list(REGISTRY.keys())`.
- All existing flags (`--verbose/-v`, `--debounce/-d`, `--fix`, `--agent`) preserved with identical semantics (`learning_mode = not args.fix`, `mode = "agent" if args.agent else "diagnoser"`). `--help` exits 0 and shows `--monitors`.
- `.planning/ADDING-A-MONITOR.md` (262 lines) created — a concrete, copy-followable recipe: Overview table of the 5 pieces of a monitor, the shared helpers a new domain reuses (`event_bus.publish`, `shared/remediation.{THRESHOLDS,_meets_threshold,_log_action,PreflightResult}`, `diagnoser_base.diagnose_with_claude`, `agent_loop.run_tool_loop`, `safety.safety_check`), step-by-step Observer/Diagnoser/Remediator/Agent/Registration/Wiring sections with real file paths and `DomainConfig` field names, the EC2 `reboot_instance` / Postgres `kill_query` framework-acceptance examples, the `InfraEvent.domain` Literal-widening callout, a D-15 checklist, and a "what NOT to touch" note (`agent/shared/safety.py` frozen; `THRESHOLDS` vs `escalate_below` stay separate). Ties directly to the 7 deferred observers (OBS-01, OBS-03..08) by name.
- `.planning/REMEDIATION-APPROACHES.md` module references corrected for the post-refactor layout: the generic consume/debounce loop now lives in `agent/orchestrators/generic_orchestrator.py`; `k8s_orchestrator.py` keeps only `_fetch_pods_on_nodes` + the k8s `DomainConfig` registration; shared primitives moved to `agent/shared/{remediation,diagnoser_base,agent_loop}.py`. `mode="diagnoser"` and `mode="agent"` descriptions remain accurate (conceptual content unchanged, only paths corrected).
- Full Wave 0 regression suite remains green: `.venv/bin/python -m pytest tests/ -q` → **100% pass** (re-run at finalization, confirms no regression from this plan's changes).

## Task Commits

Each task was committed atomically:

1. **Task 1: Make run_local.py registry-driven with a --monitors flag (D-14); D-16 deferred** - `bef56f1` (feat)
2. **Task 2: Write ADDING-A-MONITOR.md (D-15) and update REMEDIATION-APPROACHES.md module references** - `194c15f` (docs)
3. **Task 3: Blocking human-verify checkpoint (live mode=diagnoser/mode=agent e2e)** - no code commit; **DEFERRED by operator decision** (see Deviations/Issues below) — not approved, not rejected, recorded as an open follow-up.

**Plan metadata:** finalization commit (this SUMMARY + STATE.md update) — `docs(01-06): ...`

## Files Created/Modified

- `scripts/run_local.py` - registry-driven launcher: `REGISTRY`-iterating `TaskGroup` loop starting `cfg.observer` + `generic_orchestrator.run` per domain; new `parse_monitors()` + `--monitors` flag; old hardcoded `watch_nodes`/`k8s_orchestrator.run` imports removed; docstring updated to "one observer + one generic-orchestrator per registered domain"; k8s dev tailers commented as k8s-scoped
- `tests/test_registry.py` - added `parse_monitors()` unit tests (single domain, default-to-all, unknown-domain rejection)
- `.planning/ADDING-A-MONITOR.md` - **(new, 262 lines)** the D-15 add-a-domain recipe: shared helpers, DomainConfig shape, Observer/Diagnoser/Remediator/Agent/Registration/Wiring steps, EC2/Postgres acceptance examples, checklist, "what NOT to touch", ties to OBS-01/OBS-03..08
- `.planning/REMEDIATION-APPROACHES.md` - module/path references corrected for the post-refactor layout (generic_orchestrator, k8s_orchestrator fetcher-only, agent/shared/{remediation,diagnoser_base,agent_loop})

**Note:** `agent/shared/backoff.py` was listed in the plan's `files_modified` as a D-16-conditional path but was **not created** — D-16 was deferred (see Decisions). `agent/observers/k8s_node_observer.py` (also listed conditionally) was **not modified**.

## Decisions Made

- **D-16 backoff extraction deferred.** The plan made this explicitly optional ("do only if cheap... since no second observer is added this phase, DEFER by default"). With only the k8s domain registered, extracting `_backoff` to `agent/shared/backoff.py` has no second consumer and no observable benefit this phase. `_backoff` remains in `agent/observers/k8s_node_observer.py` unchanged. `agent/shared/backoff.py` was not created (avoiding an empty/unused module per the plan's guidance).
- **k8s dev tailers stay k8s-scoped.** `_tail_events`/`_tail_escalations`/`_tail_actions` keep their hardcoded `events:k8s`/`escalations:k8s` stream names — k8s is still the only registered domain, so generalizing the tailers now would add complexity with no current benefit. A one-line comment marks them as k8s-scoped dev conveniences, per the plan's explicit allowance.
- **Task 3 live e2e checkpoint: DEFERRED by operator decision, not approved.** `ANTHROPIC_API_KEY` is not set in this environment, so the live `mode=diagnoser` / `mode=agent` pipeline against minikube + Redis + a real API key could not be exercised. The operator was presented with this blocker and explicitly chose "Defer live e2e, accept structural verification": the automated structural checks already completed (valid syntax, `REGISTRY`/`generic_orchestrator`/`--monitors`/`cfg.observer` all present, old k8s_orchestrator import removed, `--help` shows `--monitors`, `tests/test_registry.py` passes, full pytest suite green at 100%, `ADDING-A-MONITOR.md` recipe complete and >=60 lines) are accepted as sufficient evidence to sign off this plan and the 6-plan framework-refactor sub-scope. **This is NOT a claim that the live K8s paths (mode=diagnoser, mode=agent, node_deleted/NetworkUnavailable escalation) were verified to behave identically post-refactor** — that assertion is explicitly left open. The pre-refactor behavior of those paths remains pinned by the Wave 0 regression suite (pure-logic level only); the live integration run through Redis/minikube/Claude is an open follow-up for whenever `ANTHROPIC_API_KEY` is available.

## Deviations from Plan

### Auto-fixed Issues

None — Tasks 1 and 2 executed as written with no Rule 1-3 auto-fixes required.

---

**Total deviations:** 0 auto-fixed.
**Impact on plan:** No scope change to Tasks 1-2. Task 3 (the blocking checkpoint) was resolved via an operator decision to defer rather than execute — documented above and in Issues Encountered.

## Issues Encountered

- **Task 3 checkpoint blocked by missing `ANTHROPIC_API_KEY`.** The plan's Task 3 (`checkpoint:human-verify`, `gate="blocking"`) requires a live end-to-end run of `scripts/run_local.py` (mode=diagnoser and mode=agent) against minikube + Redis + a real Anthropic API key, to confirm the refactored pipeline behaves identically to pre-refactor. This environment has no `ANTHROPIC_API_KEY` configured, so the live run could not be executed or automated around.
  - **Resolution:** Operator was asked how to proceed and explicitly chose "Defer live e2e, accept structural verification" — i.e., accept the already-completed automated structural checks (imports/wiring/registry tests/`--help`/full pytest suite/`ADDING-A-MONITOR.md` completeness) as sufficient for Phase 1 framework-refactor sign-off, and record the live e2e (mode=diagnoser, mode=agent, node_deleted/NetworkUnavailable escalation, all against minikube+Redis+real API key) as a **documented open follow-up** — not as "approved" and not as a passed/failed checkpoint. No claim is made that the live K8s integration paths behave identically post-refactor; only that (a) pre-refactor behavior is pinned by the Wave 0 regression suite at the pure-logic level, and (b) the structural/wiring checks for the registry-driven runner pass.

## User Setup Required

**External service configuration is the open follow-up, not a setup step for this plan.** To close the deferred Task 3 checkpoint in the future, a future session needs:
- minikube running
- Redis (`docker run -d --name atoloan-redis -p 6379:6379 redis:7-alpine`)
- `export ANTHROPIC_API_KEY=sk-ant-...`
- `pip install -r requirements-monitor.txt`
- Then run the plan's Task 3 `<how-to-verify>` steps 1-4 (mode=diagnoser, mode=agent, `--monitors k8s`, node_deleted/NetworkUnavailable escalation)

No setup is required to consider Tasks 1-2 of this plan complete — they are fully delivered and verified structurally.

## Next Phase Readiness

- D-14 (registry-driven runner with `--monitors`) and D-15 (`ADDING-A-MONITOR.md`) are complete. D-16 (backoff extraction) is explicitly deferred with no negative impact (no second observer exists yet to need it).
- The framework acceptance criterion from 01-CONTEXT.md `<specifics>` — "could an EC2 `reboot_instance` or Postgres `kill_query` action flow through DomainConfig + generic_orchestrator + shared/remediation + human_escalator without further core changes?" — is answered **yes** by the combination of D-01, D-04, D-10..D-13, and D-14, and `ADDING-A-MONITOR.md` documents exactly that path for the 7 deferred observers (OBS-01, OBS-03..08).
- **This plan completes the 6-plan framework-refactor sub-scope of Phase 1** (per the `<requirement_coverage>` table in 01-06-PLAN.md): OBS-02 and OBS-09 are DELIVERED on the new framework; OBS-01/OBS-03..08 are ENABLED-FOR-LATER via the registry + `ADDING-A-MONITOR.md` (not implemented); INFRA-01..06 remain DEFERRED/out-of-scope (deployment/ops, not code refactor).
- **The full ROADMAP.md Phase 1 goal ("8 observers in learning mode") is NOT met by this plan** — only the framework-refactor sub-scope is complete. Whether the ROADMAP Phase 1 checkbox should be marked is a decision for the upcoming `gsd-verifier` pass, not this finalization.
- **Open follow-up before claiming K8s paths are behavior-preserving in production:** the live `mode=diagnoser` and `mode=agent` e2e run against minikube + Redis + a real `ANTHROPIC_API_KEY` (plan Task 3's four verification steps) has not been executed. The Wave 0 regression suite pins pure-logic behavior; the live integration path remains unverified.
- Ready for Phase 1 verification (`gsd-verifier`) to assess the framework-refactor sub-scope against the honest requirement-coverage table, and to determine next steps for the deferred observers and the open e2e follow-up.

## Self-Check: PASSED

- FOUND: `.planning/phases/01-infrastructure-and-observers/01-06-SUMMARY.md`
- FOUND: Task 1 commit `bef56f1`
- FOUND: Task 2 commit `194c15f`

---
*Phase: 01-infrastructure-and-observers*
*Completed: 2026-06-15*
