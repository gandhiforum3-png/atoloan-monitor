---
phase: 01-infrastructure-and-observers
plan: 04
subsystem: infra
tags: [refactor, registry, orchestrator, domain-config, dataclass, dependency-injection]

# Dependency graph
requires:
  - phase: 01-01
    provides: Wave 0 baseline regression net (test_escalator, test_orchestrator_routing) pinning current urgency/routing behavior
  - phase: 01-03
    provides: agent/shared/diagnoser_base.py + agent/shared/agent_loop.py (kubernetes_asyncio-free shared Claude control flows) reused by the wired k8s callables
provides:
  - agent/registry.py — DomainConfig dataclass + REGISTRY dict + register(); the single wiring point for a monitor domain (no skill/observer imports, no K8s client)
  - agent/orchestrators/generic_orchestrator.py — run(redis, client, config, *, debounce_seconds, learning_mode, mode) drives the consume/debounce/bundle/dispatch loop for ANY DomainConfig; _should_escalate(diagnosis, escalate_below); no K8s client import
  - agent/orchestrators/k8s_orchestrator.py — shrunk to _fetch_pods_on_nodes (K8s-API context fetcher) + a single register(DomainConfig(domain="k8s", ...)); generic loop removed
  - human_escalator._derive_urgency(signals, urgency_map) + escalate(..., urgency_map) — urgency map is now a per-domain argument
  - tests/test_registry.py — registry construction + k8s wiring assertions
affects: [01-05, 01-06]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Adding a domain = one DomainConfig entry + register(); the generic orchestrator consumes config — no per-domain loop copy-paste"
    - "registry.py imports nothing from agent.skills/agent.observers (consumed BY orchestrators, not the reverse) to keep the dependency one-directional and avoid a circular import"
    - "Domain-specific context fetch (K8s pod listing) is injected via DomainConfig.context_fetcher so the generic loop stays K8s-client-free (grep -L gate)"
    - "Per-domain urgency_map and escalate_below travel on the DomainConfig; routing/urgency helpers take the map as an explicit arg with conservative defaults"
    - "escalate_below (routing floors, 1.1 sentinels) is kept as a SEPARATE dict from THRESHOLDS (execution gate, 0.00 sentinels) — intentionally not merged"

key-files:
  created:
    - agent/registry.py
    - agent/orchestrators/generic_orchestrator.py
    - tests/test_registry.py
  modified:
    - agent/orchestrators/k8s_orchestrator.py
    - agent/skills/remediators/human_escalator.py
    - tests/test_escalator.py
    - tests/test_orchestrator_routing.py

key-decisions:
  - "DomainConfig carries an observer field (research flag A2 / D-14) and an Optional context_fetcher; the k8s config wires observer=watch_nodes and context_fetcher=_fetch_pods_on_nodes"
  - "_derive_urgency / escalate default an absent urgency_map to {} -> everything falls through to p3_within_1h (safe conservative default), proven by a new empty-map test"
  - "Genericized the escalation summary string from 'N node signal(s)' to 'N {domain} signal(s)' (research Open Question 3, the one intentional cosmetic behavior change)"
  - "_should_escalate moved into generic_orchestrator and now takes escalate_below as an arg; tests pass REGISTRY['k8s'].escalate_below so routing is asserted against the real wired map"

patterns-established:
  - "DomainConfig registry: the only place a new monitor wires in (stream, group, observer, diagnose, remediate, run_incident, escalate_below, urgency_map, context_fetcher)"
  - "Generic orchestrator is fully config-driven and library-agnostic; domain modules contribute only their fetcher + a registration call at import time"

requirements-completed: [OBS-02, OBS-09]

# Metrics
duration: ~6min
completed: 2026-06-15
---

# Phase 1 Plan 04: Domain Registry + Generic Orchestrator Summary

**Built the DomainConfig registry and a fully config-driven generic_orchestrator.run that drives the consume/debounce/bundle/dispatch loop for any domain, shrinking k8s_orchestrator to its K8s pod-context fetcher plus a single register(DomainConfig(...)) and moving the urgency map onto the DomainConfig — k8s routing/urgency behavior identical, full suite green at 60 tests.**

## Performance

- **Duration:** ~6 min
- **Completed:** 2026-06-15
- **Tasks:** 2 (both TDD)
- **Files modified:** 7 (3 created, 4 modified)

## Accomplishments
- `agent/registry.py`: `@dataclass DomainConfig` (domain, stream, consumer_group, observer, diagnose, remediate, run_incident, escalate_below, urgency_map, optional context_fetcher) + `REGISTRY` dict + `register()`. Imports only dataclasses/typing — no agent.skills, no agent.observers, no K8s client (one-directional dependency, no circular import).
- `agent/orchestrators/generic_orchestrator.py`: `run(redis, anthropic_client, config, *, debounce_seconds, learning_mode, mode)` runs the consume/debounce loop for any DomainConfig — xgroup_create/XREADGROUP on `config.stream`/`config.consumer_group`, InfraEvent/SignalBundle built with `config.domain`, dispatch via `config.diagnose`/`remediate`/`run_incident`/`context_fetcher`, and `_should_escalate(diagnosis, config.escalate_below)`. No K8s client import (gated by `grep -L`).
- `agent/orchestrators/k8s_orchestrator.py` shrunk to `_fetch_pods_on_nodes` (the only K8s-API piece) + a single `register(DomainConfig(domain="k8s", stream="events:k8s", consumer_group="k8s-orchestrator", ...))` at import time. The consume/debounce loop, `_decode_event`, `_handle_bundle`, `_should_escalate`, and the module-level constants are gone (no duplicate).
- `human_escalator`: `_derive_urgency(signals, urgency_map)` takes the map as an explicit arg (module-level `_URGENCY_MAP` deleted); `escalate(..., urgency_map=None)` passes `urgency_map or {}`; summary genericized to `{domain} signal(s)`.
- `tests/test_registry.py`: asserts k8s registered, runtime keys preserved (events:k8s / k8s-orchestrator), escalate_below 1.1 sentinels distinct from THRESHOLDS, the exact 4-entry urgency map with the node_network_unavailable/node_deleted gap preserved (len == 4), and all five callables wired.

## Task Commits

Each task committed atomically (TDD for both):

1. **Task 1: registry + per-domain urgency** — `53953f4` (feat)
2. **Task 2: generic_orchestrator + shrunk k8s_orchestrator** — `933252d` (feat)

_Note: the RED gate for both tasks was the test edit landing in the same commit as the implementation; the tests fail against the pre-change signatures (extra urgency_map arg / moved _should_escalate import) and pass after the implementation, verified incrementally before each commit._

## Files Created/Modified
- `agent/registry.py` (created) — DomainConfig dataclass + REGISTRY + register(); no skill/observer/K8s imports.
- `agent/orchestrators/generic_orchestrator.py` (created) — config-driven consume/debounce/bundle/dispatch loop; `_should_escalate(diagnosis, escalate_below)`; no K8s client import.
- `agent/orchestrators/k8s_orchestrator.py` (modified) — shrunk to `_fetch_pods_on_nodes` + the k8s DomainConfig registration; generic loop removed.
- `agent/skills/remediators/human_escalator.py` (modified) — `_derive_urgency(signals, urgency_map)`; `_URGENCY_MAP` removed; `escalate(..., urgency_map)`; summary genericized.
- `tests/test_registry.py` (created) — registry construction + k8s wiring assertions.
- `tests/test_escalator.py` (modified) — pass the k8s map explicitly; new empty-map -> p3 default-safe assertion.
- `tests/test_orchestrator_routing.py` (modified) — import `_should_escalate` from generic_orchestrator; pass `REGISTRY["k8s"].escalate_below`.

## Decisions Made
- DomainConfig includes the `observer` field now (research flag A2 / D-14 needs it for the observer entrypoint) even though this plan does not consume it yet — the k8s config wires `observer=watch_nodes` so D-14 is mechanical.
- `_derive_urgency` and `escalate` default an absent map to `{}`, which makes every event fall through to `p3_within_1h` — the conservative default for any caller that forgets to supply a map. Added an explicit test for this.
- Moved `_should_escalate` into `generic_orchestrator` (not `registry`) — it is loop logic, not config; the registry stays import-light and skill-free.

## Deviations from Plan
None — plan executed exactly as written. Two acceptance greps initially failed for cosmetic reasons in my own output (the docstring contained the bare token `kubernetes_asyncio` in prose, tripping the `grep -L` gate; and `register(` / `DomainConfig(` were on separate lines so `register(DomainConfig` did not match). Both were reworded/reformatted before the Task 2 commit so all acceptance criteria pass — no behavior change, no scope change.

## Threat Mitigations Verified
- **T-01-09** (Tampering, merging escalate_below with THRESHOLDS): kept as a separate dict on DomainConfig; `test_registry.test_k8s_escalate_below_sentinels` asserts the 1.1 sentinels (distinct from THRESHOLDS 0.00).
- **T-01-10** (Tampering, urgency map "filled in" during the move): k8s `urgency_map` copied verbatim (4 entries); `test_registry` asserts `node_network_unavailable`/`node_deleted` NOT in the map and `len == 4`; `test_escalator` asserts both fall through to p3.
- **T-01-11** (Spoofing, wrong stream/group strands Redis offsets): `test_registry.test_k8s_runtime_keys_preserved` asserts `stream == "events:k8s"` and `consumer_group == "k8s-orchestrator"`.
- **T-01-12** (Elevation, K8s client leaks into the generic loop): context fetch injected via `config.context_fetcher`; `grep -L kubernetes_asyncio agent/orchestrators/generic_orchestrator.py` prints the filename (no K8s import, not even in prose after the reword).

## Issues Encountered
None blocking. The pre-existing unrelated working-tree changes to `.planning/STATE.md` and `.planning/config.json` were left out of all task commits as out of scope.

## User Setup Required
None — the suite runs offline with no Redis, minikube, or ANTHROPIC_API_KEY.

## Next Phase Readiness
- `agent/registry.py` is the single wiring point for new domains (01-05/01-06): a new monitor writes its observer + fetcher + skill callables and adds one `register(DomainConfig(...))` — no orchestrator copy-paste.
- `generic_orchestrator.run` is config-driven and K8s-client-free; `_should_escalate` takes the per-domain floor map. The `observer` field is already on the config for the D-14 observer entrypoint work.
- Baseline equivalence proof remains green: full suite 60 passed (54 prior + 5 registry assertions + 1 empty-map urgency test).

## Self-Check: PASSED

agent/registry.py, agent/orchestrators/generic_orchestrator.py, agent/orchestrators/k8s_orchestrator.py, and tests/test_registry.py all present on disk; commits 53953f4 and 933252d exist in git history; full suite reports 60 passed; no file deletions in either task commit.

---
*Phase: 01-infrastructure-and-observers*
*Completed: 2026-06-15*
