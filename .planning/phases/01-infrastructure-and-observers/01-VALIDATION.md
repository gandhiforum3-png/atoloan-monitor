---
phase: 01
slug: infrastructure-and-observers
status: approved
nyquist_compliant: true
wave_0_complete: false
created: 2026-06-15
---

# Phase 01 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
>
> Scope note: this phase plan covers the generic-monitor framework refactor
> (CONTEXT.md D-01..D-16), not the implementation of the remaining 7
> observers. "Requirements" below are behavior-preservation invariants for
> the existing K8s node monitor (`mode="diagnoser"` and `mode="agent"`).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x [ASSUMED — verify installed version in Wave 0] |
| **Config file** | none — Wave 0 creates `pyproject.toml [tool.pytest.ini_options]` (or `pytest.ini`) with `pythonpath = ["."]` |
| **Quick run command** | `python -m pytest tests/ -x -q` |
| **Full suite command** | `python -m pytest tests/ -v` |
| **Estimated runtime** | ~5 seconds (pure-Python unit tests, no Redis/minikube/API key) |

---

## Sampling Rate

- **After every task commit:** Run `python -m pytest tests/ -x -q`
- **After every plan wave:** Run `python -m pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green, plus a manual `python scripts/run_local.py` (diagnoser) and `python scripts/run_local.py --agent` smoke (checkpoint:human-verify — needs live Redis/minikube/API key)
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| TBD | TBD | 0 | Baseline (capture-first) | unit | `pytest tests/test_remediation.py tests/test_orchestrator_routing.py tests/test_escalator.py -x` | ❌ Wave 0 | ⬜ pending |
| TBD | TBD | 0 | Test harness bootstrap (pytest config, conftest) | infra | `python -m pytest tests/ -q` (collects with 0 errors) | ❌ Wave 0 | ⬜ pending |
| TBD | TBD | 1+ | D-01 — open `action_type` to `str`, schema has no enum | unit | `pytest tests/test_models.py -x` | ❌ Wave 0 | ⬜ pending |
| TBD | TBD | 1+ | D-03/D-04/D-05 — `PreflightResult`/`THRESHOLDS`/`_meets_threshold`/`_log_action` moved to `agent/shared/remediation.py`, per-domain threshold matrix | unit | `pytest tests/test_remediation.py -x` | ❌ Wave 0 | ⬜ pending |
| TBD | TBD | 1+ | D-06/D-07 — generic `run_tool_loop` extracted, `node_agent.py` retains domain-specific pieces | unit | `pytest tests/test_imports.py -x` (no `kubernetes_asyncio` in `agent/shared/*`) | ❌ Wave 0 | ⬜ pending |
| TBD | TBD | 1+ | D-08/D-09 — `diagnose_with_claude` extracted, `node_diagnoser.py` retains prompt + formatter | unit | `pytest tests/test_imports.py -x` | ❌ Wave 0 | ⬜ pending |
| TBD | TBD | 1+ | D-10/D-11/D-12 — `DomainConfig`, `generic_orchestrator.run`, k8s registry entry | unit | `pytest tests/test_registry.py -x` | ❌ Wave 0 | ⬜ pending |
| TBD | TBD | 1+ | D-01/D-11 — escalation routing preserved (human_escalate / low-confidence / unknown action_type → escalate; valid high-confidence → remediate) | unit | `pytest tests/test_orchestrator_routing.py -x` | ❌ Wave 0 | ⬜ pending |
| TBD | TBD | 1+ | D-13 — `_URGENCY_MAP` per-domain via `DomainConfig.urgency_map`, fallback to `p3_within_1h` preserved | unit | `pytest tests/test_escalator.py -x` | ❌ Wave 0 | ⬜ pending |
| TBD | TBD | 1+ | D-14 — `scripts/run_local.py` iterates registry, `--monitors` flag | unit | `pytest tests/test_registry.py -x` (registry has ≥1 domain with observer/orchestrator wired) | ❌ Wave 0 | ⬜ pending |
| TBD | TBD | 1+ | Safety floor unchanged — `FORBIDDEN_OPERATIONS`/`safety_check` still blocks regardless of open `action_type`; `learning_mode` still blocks execution | unit | `pytest tests/test_safety.py -x` | ❌ Wave 0 | ⬜ pending |
| TBD | TBD | final | End-to-end: K8s node monitor `mode="diagnoser"` and `mode="agent"` paths unchanged | manual | `python scripts/run_local.py` and `python scripts/run_local.py --agent` | n/a | ⬜ pending (checkpoint:human-verify) |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

*Task IDs are TBD — the planner assigns concrete `{phase}-{plan}-{task}` IDs; this table's rows must be re-keyed to those IDs as plans are written, preserving the requirement/test mapping above.*

---

## Wave 0 Requirements

- [ ] `requirements-dev.txt` (or add to `requirements-monitor.txt`) — pin `pytest` + `pytest-asyncio` (verify versions against what's installed)
- [ ] `pyproject.toml [tool.pytest.ini_options]` (or `pytest.ini`) with `pythonpath = ["."]` so `import agent...` works from `tests/`
- [ ] `tests/conftest.py` — shared fixtures (fake redis async stub, sample `SignalBundle`/`DiagnosisResult` fixtures)
- [ ] `tests/test_remediation.py` — threshold gating + target parsing baseline (capture against CURRENT code first)
- [ ] `tests/test_orchestrator_routing.py` — escalation routing baseline
- [ ] `tests/test_escalator.py` — urgency derivation baseline
- [ ] `tests/test_models.py` — `DiagnosisResult`/`action_type` schema baseline
- [ ] `tests/test_registry.py` — registry/`DomainConfig` construction (post-refactor)
- [ ] `tests/test_imports.py` — import integrity, no `kubernetes_asyncio` leakage into `agent/shared/*`
- [ ] `tests/test_safety.py` — `FORBIDDEN_OPERATIONS`/`safety_check` floor + `learning_mode` gating order (safety → threshold → learning_mode → preflight → execute)

**Capture baseline FIRST:** write the routing/threshold/urgency/safety tests against the *current* (pre-refactor) code and get them green before any extraction, so they prove behavioral equivalence after each move.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| End-to-end K8s node monitor, `mode="diagnoser"` | OBS-02 (preserved, not re-implemented) | Needs live Redis + minikube/K8s cluster + Anthropic API key | Run `python scripts/run_local.py`; inject a node-pressure event via `scripts/simulate_node_pressure.py`; confirm diagnosis + remediation/escalation flow completes as before the refactor |
| End-to-end K8s node monitor, `mode="agent"` | OBS-02 (preserved, not re-implemented) | Needs live Redis + minikube/K8s cluster + Anthropic API key | Run `python scripts/run_local.py --agent`; inject the same event; confirm the agentic tool-use loop reaches `finish_incident` or `human_escalator.escalate` as before |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 5s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-06-15 (via /gsd:plan-phase 1 plan-checker verification)
