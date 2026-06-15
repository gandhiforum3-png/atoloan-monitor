---
phase: 01-infrastructure-and-observers
plan: 05
subsystem: infra
tags: [pydantic, action_type, safety, diagnoser, threshold-gate, escalation]

# Dependency graph
requires:
  - phase: 01-04
    provides: DomainConfig registry + generic orchestrator (_should_escalate takes config.escalate_below) + per-domain urgency_map
provides:
  - "DiagnosisResult.action_type is an open str (D-01/D-02) — the structural prerequisite that makes deferred OBS-03..08 / REM-02..08 domains expressible"
  - "node_diagnoser re-injects a per-domain action_type enum into its own tool input_schema so its Claude call stays API-constrained without re-closing the shared model"
  - "diagnoser_base.diagnose_with_claude accepts an optional input_schema arg (defaults to result_model.model_json_schema())"
  - "Regression net proving the safety floor (FORBIDDEN_OPERATIONS), the threshold execution gate, and learning_mode all hold under the open field"
affects: [02-diagnosis-and-slo-engine, 03-safe-remediation, 01-06]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Open str at the shared model layer + per-domain enum re-injection at the tool-schema layer (belt-and-suspenders): the shared DiagnosisResult stays domain-agnostic while each diagnoser keeps its own Claude call API-constrained"
    - "Threshold execution gate (THRESHOLDS two-level .get default 1.0) is the hard backstop that guarantees an unknown action never executes — NOT the routing/escalation layer"

key-files:
  created: []
  modified:
    - agent/shared/models.py
    - agent/shared/diagnoser_base.py
    - agent/skills/diagnosers/node_diagnoser.py
    - tests/test_models.py
    - tests/test_orchestrator_routing.py
    - tests/test_safety.py

key-decisions:
  - "DiagnosisResult.action_type opened from a closed 4-value Literal to an open str (D-01/D-02); InfraEvent.domain/severity, HumanEscalationPacket.urgency, and estimated_blast_radius all stay Literal"
  - "node_diagnoser re-injects a per-domain action_type enum into its tool input_schema (via the new optional input_schema arg on diagnose_with_claude) so its Claude call stays constrained to the 4 k8s actions; _SYSTEM_PROMPT prose enumeration kept as a second layer"
  - "The threshold execution gate — not escalation routing — is the real backstop that guarantees a novel/unknown action never executes; a high-confidence novel action passes the routing floor but is stopped at _meets_threshold (default 1.0 -> never met)"

patterns-established:
  - "Per-domain enum re-injection: shared model carries open str; each domain re-injects its own enum into the tool schema it passes to Claude"
  - "Boundary-from-the-caller regression tests: pin the FORBIDDEN_OPERATIONS + threshold contract from the caller side while keeping safety.py byte-frozen"

requirements-completed: [OBS-02, OBS-09]

# Metrics
duration: ~6min
completed: 2026-06-15
---

# Phase 1 Plan 05: Open action_type to str Summary

**DiagnosisResult.action_type opened from a closed 4-value Literal to an open str (D-01/D-02), with the node diagnoser re-injecting a per-domain enum and a regression net proving the safety floor, threshold gate, and learning_mode all still contain the wider output.**

## Performance

- **Duration:** ~6 min
- **Started:** 2026-06-15T09:01:00Z
- **Completed:** 2026-06-15T09:08:00Z
- **Tasks:** 2 (1 auto, 1 blocking human-verify checkpoint — approved)
- **Files modified:** 6

## Accomplishments
- `DiagnosisResult.action_type` is now an open `str` — the JSON-schema enum that previously API-constrained Claude to 4 actions is gone from the shared model. This is the structural prerequisite that lets future domains (EC2 `reboot_instance`, Postgres `kill_query`) express valid actions the old Literal forbade.
- `node_diagnoser` re-injects a per-domain action enum into its own tool `input_schema` (via a new optional `input_schema` arg on `diagnose_with_claude`), so this domain's Claude call stays API-constrained without re-closing the shared model. The `_SYSTEM_PROMPT` prose enumeration of the 4 actions is kept as a second layer.
- Regression net proves the boundary contains the widening: forbidden ops (`drain_node`, `terminate_instance`, `drop_table`) still raise `SafetyViolation`; an unknown action (`reboot_node`) can never meet the execution threshold; and `agent/shared/safety.py` is byte-unchanged.
- Full suite green: **67 passed**.

## Task Commits

Each task was committed atomically:

1. **Task 1: Open action_type to str and add boundary regression tests** - `223eacc` (feat)
2. **Task 2: Blocking human-verify checkpoint** - no code commit; safety-floor gate reviewed and **approved** by operator.

**Plan metadata:** `docs(01-05): complete open-action-type-to-str plan`

_Note: This plan's TDD task produced a single feat commit (model change + diagnoser re-injection + the test updates landed together as one atomic boundary change)._

## Files Created/Modified
- `agent/shared/models.py` - `DiagnosisResult.action_type` Literal[4] → open `str`; InfraEvent.domain/severity, urgency, blast_radius stay Literal
- `agent/shared/diagnoser_base.py` - `diagnose_with_claude` gains an optional `input_schema` arg (defaults to `result_model.model_json_schema()`) so a domain can pass an enum-reinjected schema
- `agent/skills/diagnosers/node_diagnoser.py` - re-injects the per-domain action_type enum into its tool `input_schema`; prompt prose intact
- `tests/test_models.py` - flipped the "schema has enum" assertion to assert NO enum; novel action validates; confidence bounds still enforced
- `tests/test_orchestrator_routing.py` - added novel-action routing cases (below-floor escalates; high-confidence passes routing but is contained by the threshold gate)
- `tests/test_safety.py` - post-D-01 floor regressions (forbidden ops still raise; unknown action never meets threshold)

## Decisions Made
- **action_type opened to str (D-01/D-02), siblings untouched.** Only `action_type` changed; `InfraEvent.domain`/`severity`, `HumanEscalationPacket.urgency`, and `estimated_blast_radius` stay Literal. `safety.py` is FROZEN and byte-unchanged.
- **Per-domain enum re-injection in node_diagnoser.** The shared model loses its enum, so each diagnoser re-injects its own per-domain enum into the tool schema it hands to Claude — keeping the API constraint local to the domain instead of re-closing the shared model.
- **Threshold gate is the real backstop.** The hard guarantee that a novel/unknown action never executes comes from the execution threshold gate (`_meets_threshold` two-level `.get` default 1.0), not from escalation routing.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Corrected plan assertion] Routing-vs-threshold layer for high-confidence novel actions**
- **Found during:** Task 1 (boundary regression tests)
- **Issue:** The plan's `<behavior>` and a `key_link` asserted that `_should_escalate(reboot_node@0.99, REGISTRY["k8s"].escalate_below)` returns `True` (i.e., the routing/escalation layer contains a high-confidence novel action). That is not how the wiring actually behaves: an unregistered action has no `escalate_below` entry, so the default 0.80 floor applies — a **high-confidence (0.99)** novel action does NOT escalate at the routing layer.
- **Fix:** Tests assert the real contract instead of the plan's mistaken one. The actual backstop guaranteeing "novel action never executes" is the **execution threshold gate**: `_meets_threshold("k8s", "reboot_node", 0.99)` is `False` (THRESHOLDS two-level `.get` default 1.0), so `remediate()` returns `threshold_not_met` without acting. A below-floor novel action (`reboot_node@0.50`) still escalates via routing, which is also asserted. Net effect — "novel action never executes" — holds exactly as the plan intended; only the claim about *which layer* enforces it was corrected.
- **Files modified:** tests/test_orchestrator_routing.py, tests/test_safety.py
- **Verification:** Full suite green (67 passed); the corrected assertions document both the routing pass-through and the threshold-gate containment.
- **Committed in:** `223eacc` (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 corrected plan assertion).
**Impact on plan:** No scope change. The safety/escalation outcome the plan required ("novel action never executes") is fully preserved; only the documented mechanism was corrected to match the real wiring, and the tests now assert the genuine contract. Operator reviewed and approved this at the blocking checkpoint.

## Issues Encountered
None — the only surprise was the routing-vs-threshold layer detail above, handled as a Rule 1 corrected assertion.

## User Setup Required
None - no external service configuration required.

## TDD Gate Compliance
This plan's atomic boundary change (model + diagnoser + tests) landed as a single `feat` commit (`223eacc`) rather than separate `test`/`feat` commits — the test updates were inseparable from the schema flip (the prior "schema has enum" assertion had to be flipped in the same change that removed the enum). RED/GREEN were exercised locally; the committed state is GREEN (67 passed). No standalone RED `test(...)` commit exists for this plan.

## Next Phase Readiness
- D-01/D-02 complete: `action_type` is open, the boundary contains it, and the safety floor is proven intact under the open field.
- Ready for **01-06** (D-14/D-15/D-16: registry-driven runner + ADDING-A-MONITOR.md + e2e checkpoint), the final plan of Phase 1.

## Self-Check: PASSED

- FOUND: `.planning/phases/01-infrastructure-and-observers/01-05-SUMMARY.md`
- FOUND: Task 1 commit `223eacc`

---
*Phase: 01-infrastructure-and-observers*
*Completed: 2026-06-15*
