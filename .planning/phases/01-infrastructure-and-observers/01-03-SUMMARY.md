---
phase: 01-infrastructure-and-observers
plan: 03
subsystem: agent-framework
tags: [refactor, extraction, claude-control-flow, agentic-loop, prompt-caching, tdd]

# Dependency graph
requires:
  - 01-01 (Wave 0 baseline regression net pinning current behavior)
  - 01-02 (agent/shared/remediation.py — domain-keyed THRESHOLDS, _log_action, _meets_threshold)
provides:
  - agent/shared/diagnoser_base.py — diagnose_with_claude(client, system_prompt, user_text, *, result_model=DiagnosisResult, ...) generic one-shot Claude diagnosis (cached prompt + forced tool_choice + parse), ZERO kubernetes_asyncio
  - agent/shared/agent_loop.py — run_tool_loop(..., dispatch_tool, format_bundle, ctx, domain, ...) generic agentic iterate/dispatch/finish/auto-escalate loop, domain-parameterized escalation, ZERO kubernetes_asyncio
  - tests/test_imports.py — import-integrity gate + glob assertion that no agent/shared/*.py references kubernetes_asyncio
affects: [01-04, 01-05, 01-06]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Claude one-shot diagnosis mechanics live in agent/shared/diagnoser_base.py; a new domain supplies only a system prompt + user_text formatter"
    - "Agentic tool-use loop lives in agent/shared/agent_loop.py; a new domain supplies system prompt + TOOLS + dispatch_tool callable + ctx, never touching loop mechanics"
    - "run_tool_loop never inspects ctx — it only threads ctx into dispatch_tool(name,input,ctx), keeping all domain clients (K8s API) out of shared"
    - "Auto-escalation on max-iterations is parameterized by domain, not hardcoded, so future domains escalate to the correct stream"
    - "Tool input_schema generated from result_model.model_json_schema() so result-model changes (e.g. enum tightening) flow through automatically"

key-files:
  created:
    - agent/shared/diagnoser_base.py
    - agent/shared/agent_loop.py
    - tests/test_imports.py
  modified:
    - agent/skills/diagnosers/node_diagnoser.py
    - agent/skills/agents/node_agent.py

key-decisions:
  - "diagnose_with_claude reads result attributes (confidence/action_type/blast_radius) via getattr(...) for the completion log so the generic call works for any result_model, not just DiagnosisResult"
  - "node_diagnoser keeps DiagnosisResult import (used by the diagnose return annotation) even though the parse moved to shared — removing it would break the type hint"
  - "Removed the now-unused json import from node_agent (json.dumps was only inside the extracted loop)"

requirements-completed: [OBS-02]

# Metrics
duration: ~5min
completed: 2026-06-15
---

# Phase 1 Plan 03: Extract Claude Control Flows into Shared Modules Summary

**Moved the two Claude-driving control flows — the one-shot cached-prompt/forced-tool_choice/parse boilerplate and the agentic iterate/dispatch/finish/auto-escalate loop — out of node_diagnoser and node_agent into two new kubernetes_asyncio-free shared modules (diagnoser_base.py, agent_loop.py), leaving the K8s consumers as thin delegates that keep only their domain pieces, with the full baseline suite (now 54 tests) staying green.**

## Performance

- **Duration:** ~5 min
- **Completed:** 2026-06-15
- **Tasks:** 2 (both TDD)
- **Files modified:** 5 (3 created, 2 modified)

## Accomplishments
- Created `agent/shared/diagnoser_base.py` exposing `diagnose_with_claude(client, system_prompt, user_text, *, result_model=DiagnosisResult, model, max_tokens, tool_name, tool_description)` — the verbatim cached-prompt + forced `tool_choice` + token-usage log + tool_use parse, with the tool's `input_schema` derived from `result_model.model_json_schema()`. Imports limited to anthropic / structlog / pydantic / agent.shared.models — zero `kubernetes_asyncio`.
- Rewrote `node_diagnoser.diagnose` to a thin delegate: keeps `_SYSTEM_PROMPT` and `_format_bundle` byte-identical, builds `user_text`, logs `diagnoser_start`, then `return await diagnose_with_claude(...)`. The `messages.create`/parse block is gone from the file (`tool_choice={` count is 0).
- Created `agent/shared/agent_loop.py` exposing `run_tool_loop(anthropic_client, redis, bundle, incident_id, *, system_prompt, tools, dispatch_tool, format_bundle, ctx, domain, learning_mode, max_iterations=6, model)` — the extracted loop verbatim: init from `format_bundle(bundle)`, iterate, dispatch each tool_use via `dispatch_tool(name, input, ctx)` (try/except → `agent_tool_error`), handle `finish_incident` (acknowledge + `_log_action(... domain ...)` + return outcome), break on text-only, and on budget exhaustion build the unresolved `DiagnosisResult` and call `human_escalator.escalate(redis, domain=domain, ...)` returning `"max_iterations_reached"`.
- Rewrote `node_agent.run_incident` to keep the K8s context setup (`_load_k8s_config()`, `async with client.ApiClient()`, building `ctx` with core_v1/apps_v1/policy_v1) and delegate iteration to `run_tool_loop(..., domain="k8s", ...)`. Deleted the local loop body and `_escalate_unresolved` (its logic now lives in the shared loop). All `_tool_*`, `_dispatch_tool`, `TOOLS`, `_SYSTEM_PROMPT`, `MAX_ITERATIONS`, `_format_bundle` stay untouched (`for iteration in range` count is 0).
- Added `tests/test_imports.py`: parametrized clean-import assertions over the six shared modules (diagnoser_base, agent_loop, remediation, models, safety, event_bus), an entrypoint-presence check for `diagnose_with_claude`, and a glob over `agent/shared/*.py` asserting `"kubernetes_asyncio" not in <text>` for every file (covers agent_loop.py once it exists).

## Task Commits

Each task committed atomically (TDD for both):

1. **Task 1 (RED): failing import-integrity test** — `48854bb` (test)
2. **Task 1 (GREEN): extract diagnose_with_claude into shared** — `7581555` (feat)
3. **Task 2 (GREEN): extract run_tool_loop into shared** — `e4941ee` (feat)

(The Task 2 RED gate was satisfied by the same `tests/test_imports.py` written in commit `48854bb`, whose `agent.shared.agent_loop` import + glob cases failed until commit `e4941ee` created the module — a single import-integrity test file covering both extractions.)

## Files Created/Modified
- `agent/shared/diagnoser_base.py` (created) — generic Claude one-shot diagnosis call; no kubernetes_asyncio.
- `agent/shared/agent_loop.py` (created) — generic agentic tool-use loop; domain-parameterized auto-escalation; no kubernetes_asyncio.
- `tests/test_imports.py` (created) — import integrity + no-kubernetes_asyncio glob gate over agent/shared/*.
- `agent/skills/diagnosers/node_diagnoser.py` (modified) — thin delegate; keeps `_SYSTEM_PROMPT` + `_format_bundle`; Claude call removed; imports `diagnose_with_claude`.
- `agent/skills/agents/node_agent.py` (modified) — thin delegate; keeps K8s ctx setup + all domain tool bodies; loop + `_escalate_unresolved` removed; imports `run_tool_loop`; unused `json` import dropped.

## Decisions Made
- `diagnose_with_claude` logs `confidence`/`action_type`/`blast_radius` via `getattr(result, ..., None)` rather than direct attribute access, so the generic call does not assume the caller's `result_model` is `DiagnosisResult` — any future result model still logs cleanly.
- Kept `DiagnosisResult` imported in `node_diagnoser.py`: the parse moved to shared, but the symbol is still the return annotation of `diagnose`, so removing it would break the type hint.
- Dropped the `json` import from `node_agent.py` — `json.dumps` lived only inside the extracted loop; verified zero remaining references.

## Deviations from Plan
None — plan executed exactly as written. Both tasks' acceptance greps pass (including the all-three-shared-modules K8s-free count of 3) and the full suite is green.

## Threat Mitigations Verified
- **T-01-06** (Elevation, kubernetes_asyncio leak into agent_loop): `run_tool_loop` only calls `dispatch_tool(name, input, ctx)` and never reads `ctx` keys; `client.ApiClient()` + all `_tool_*` bodies stayed in node_agent. `grep -L kubernetes_asyncio` over all three shared modules returns all three filenames (count 3).
- **T-01-07** (Tampering, auto-escalation pinned to k8s): `run_tool_loop` receives `domain` and passes `domain=domain` to `human_escalator.escalate` and `_log_action`; acceptance grep `domain=domain` present. node_agent passes `domain="k8s"`.
- **T-01-08** (Tampering, per-tool safety_check dropped): each `_tool_*` body (with its `safety_check`) is untouched in node_agent; `test_safety.py` + the full suite re-run confirm the floor intact (54 passed).

## Issues Encountered
None blocking. The pre-existing unrelated working-tree change to `.planning/config.json` (`_auto_chain_active: false`) was left out of all task commits as out of scope, consistent with plan 01-02.

## User Setup Required
None — the suite runs offline with no Redis, minikube, or ANTHROPIC_API_KEY.

## Next Phase Readiness
- `diagnose_with_claude` and `run_tool_loop` are the reusable Claude-mechanics base for plans 01-04..01-06: a new domain writes a system prompt + bundle formatter (diagnoser) or prompt + TOOLS + dispatch (agent) and reuses the shared control flow with no shared-code edits.
- All three shared control/primitive modules (remediation, diagnoser_base, agent_loop) are kubernetes_asyncio-free and gated by `tests/test_imports.py`; the gate auto-covers any future `agent/shared/*.py` via glob.
- Baseline suite remains the equivalence proof and is green (54 passed). Each subsequent extraction must keep it green.

## Self-Check: PASSED

agent/shared/diagnoser_base.py, agent/shared/agent_loop.py, and tests/test_imports.py all present on disk; all three commits (48854bb, 7581555, e4941ee) exist in git history; full suite reports 54 passed; no unexpected file deletions in the task commits.

---
*Phase: 01-infrastructure-and-observers*
*Completed: 2026-06-15*
