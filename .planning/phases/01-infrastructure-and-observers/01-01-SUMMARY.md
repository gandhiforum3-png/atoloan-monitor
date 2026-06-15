---
phase: 01-infrastructure-and-observers
plan: 01
subsystem: testing
tags: [pytest, pytest-asyncio, regression-testing, python, pydantic]

# Dependency graph
requires: []
provides:
  - pytest harness (pyproject.toml pythonpath=['.'], requirements-dev.txt) runnable with no Redis/minikube/API key
  - tests/conftest.py with make_diagnosis and make_event factory fixtures against agent.shared.models
  - 36-case baseline regression net pinning CURRENT pre-refactor behavior of threshold gating, escalation routing, urgency derivation, safety floor, and DiagnosisResult enum schema
affects: [01-02, 01-03, 01-04, 01-05, 01-06]

# Tech tracking
tech-stack:
  added: [pytest>=8.0, pytest-asyncio>=0.23]
  patterns:
    - "Test deps isolated to requirements-dev.txt; runtime requirements-monitor.txt stays test-free"
    - "agent/ imported via pythonpath (sys.path), NOT installed as a package — no [project]/[build-system] table in pyproject.toml"
    - "Factory fixtures (make_diagnosis, make_event) build valid CURRENT-schema objects for pure-function tests"

key-files:
  created:
    - pyproject.toml
    - requirements-dev.txt
    - tests/__init__.py
    - tests/conftest.py
    - tests/test_remediation.py
    - tests/test_orchestrator_routing.py
    - tests/test_escalator.py
    - tests/test_safety.py
    - tests/test_models.py
  modified: []

key-decisions:
  - "Pinned pytest>=8.0 / pytest-asyncio>=0.23 with version floors after pip index verification (both ubiquitous, threat T-01-01 accepted)"
  - "No fake-redis fixture — Wave 0 baseline tests exercise only pure sync functions; async redis paths out of scope"
  - "pyproject.toml kept minimal (only [tool.pytest.ini_options]) so agent/ stays sys.path-imported, not pip-installed"

patterns-established:
  - "Regression-net-first: capture current behavior before any extraction so each later move can be proven equivalent"
  - "Fall-through behaviors (node_network_unavailable / node_deleted -> p3_within_1h) are explicitly pinned with code comments referencing D-13"

requirements-completed: [OBS-02, OBS-09]

# Metrics
duration: 8min
completed: 2026-06-15
---

# Phase 1 Plan 01: Wave 0 Regression Net Bootstrap Summary

**pytest harness + 36-case baseline suite pinning the CURRENT threshold/routing/urgency/safety/schema behavior of the existing K8s agent code, green with no Redis, minikube, or API key**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-06-15T08:00:40Z
- **Completed:** 2026-06-15
- **Tasks:** 2
- **Files modified:** 9 (all created)

## Accomplishments
- Bootstrapped a zero-to-one pytest harness (no test infra existed before) configured so `import agent...` resolves from tests/ via `pythonpath = ["."]`
- Installed and pinned pytest>=8.0 / pytest-asyncio>=0.23 in requirements-dev.txt, keeping runtime requirements-monitor.txt test-free
- Wrote 5 baseline test files (36 cases) that pass green against the unmodified agent/ code, pinning: per-action confidence thresholds (0.80/0.85, unknown->1.0), `_should_escalate` routing contract, urgency p1/p2/p3 mapping including the two intentional p3 fall-throughs, the FORBIDDEN_OPERATIONS safety floor + SafetyViolation raises, and the CURRENT DiagnosisResult enum schema with confidence bounds
- Established the regression contract that protects OBS-02 (K8s node monitor) and OBS-09 (InfraEvent schema) through the upcoming extraction plans 01-02..01-06

## Task Commits

Each task was committed atomically:

1. **Task 1: Bootstrap pytest config and dev dependencies** - `5b469b7` (chore)
2. **Task 2: Capture baseline tests against CURRENT code** - `445acc6` (test)

## Files Created/Modified
- `pyproject.toml` - Minimal [tool.pytest.ini_options]: pythonpath, testpaths, asyncio_mode=auto, addopts=-q
- `requirements-dev.txt` - Pinned pytest>=8.0, pytest-asyncio>=0.23 (test deps isolated from runtime)
- `tests/__init__.py` - Empty package marker
- `tests/conftest.py` - make_diagnosis / make_event factory fixtures against agent.shared.models
- `tests/test_remediation.py` - Threshold matrix + ns/dep target parsing baseline (node_remediator)
- `tests/test_orchestrator_routing.py` - `_should_escalate` routing baseline (k8s_orchestrator)
- `tests/test_escalator.py` - `_derive_urgency` baseline incl. node_network_unavailable/node_deleted fall-throughs (human_escalator)
- `tests/test_safety.py` - FORBIDDEN_OPERATIONS floor + SafetyViolation raise baseline (safety, FROZEN)
- `tests/test_models.py` - DiagnosisResult enum schema + confidence-bound baseline (models)

## Decisions Made
- Pinned both test deps with `>=` floors after `pip index versions` verification; both are long-established, high-download PyPI packages (threat T-01-01 accepted, no [SUS]/[SLOP]).
- Omitted a fake-redis fixture: every Wave 0 baseline test calls pure sync functions, so the async `_log_action`/`escalate` redis paths are deliberately out of scope for the baseline.
- Kept pyproject.toml to a single table (no [project]/[build-system]) to preserve the existing sys.path import model for agent/.

## Deviations from Plan

None - plan executed exactly as written. No agent/ code was modified (this plan is regression-net-only); all changes are additive test infrastructure.

## Issues Encountered
- Acceptance criterion `pytest tests/ --co -q | grep -c "::test_" >= 20` does not match pytest 9.x output, which prints a per-file count summary (8+3+6+10+9 = 36) instead of `::test_` node IDs. The underlying intent — at least 20 collected baseline cases — is satisfied: the full run reports "collected 36 items, 36 passed". Only the grep pattern is version-specific; no behavior issue.

## User Setup Required
None - no external service configuration required. The suite runs offline with no Redis, minikube, or ANTHROPIC_API_KEY.

## Next Phase Readiness
- Regression net is in place and green. Plans 01-02 through 01-06 can now extract/refactor agent/ code and must keep all 36 cases green as the equivalence proof.
- The two intentional urgency fall-throughs (node_network_unavailable, node_deleted) are pinned with comments referencing D-13 so a later move cannot silently change them.

## Self-Check: PASSED

All 9 created files present on disk; both task commits (`5b469b7`, `445acc6`) exist in git history.

---
*Phase: 01-infrastructure-and-observers*
*Completed: 2026-06-15*
