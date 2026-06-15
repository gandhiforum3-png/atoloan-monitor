# Phase 1: Infrastructure and Observers - Research

**Researched:** 2026-06-15
**Domain:** Python async refactor — extracting a registry-based observer/orchestrator/skill framework from a working K8s-node-only implementation
**Confidence:** HIGH (this is an internal-code-only refactor; all findings come from reading the actual codebase, not external sources)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

These are D-01 through D-16, copied verbatim from `01-CONTEXT.md`. The planner MUST honor all of them.

**Open action_type (prerequisite for any non-K8s domain)**
- **D-01:** `DiagnosisResult.action_type` (`agent/shared/models.py`) changes from the closed `Literal["pod_restart", "deployment_scale_down", "human_escalate", "observe_only"]` to an open `str`. Validity is enforced at the orchestrator/remediation boundary against that domain's registered threshold keys, with `agent/shared/safety.py::safety_check()` / `FORBIDDEN_OPERATIONS` remaining the absolute, domain-agnostic floor (unchanged — still blocks `terminate_instance`, `drop_table`, `drain_node`, `cordon_node`, etc. regardless of domain).
- **D-02:** This is required so future domains (EC2 `reboot_instance`, Postgres `kill_query`) can express valid actions the current Literal doesn't allow — without it, the registry pattern is structurally complete but those domains could never produce a legal `DiagnosisResult`.

**Shared remediation primitives (extract, no behavior change)**
- **D-03:** Move `PreflightResult` (dataclass), `_meets_threshold`, `_log_action`, and the `THRESHOLDS` registry from `agent/skills/remediators/node_remediator.py` into a new `agent/shared/remediation.py`.
- **D-04:** `THRESHOLDS` becomes `dict[str, dict[str, float]]` keyed by `domain` then `action_type` (e.g. `THRESHOLDS["k8s"]["pod_restart"] = 0.80`), so each new domain registers its own action/threshold pairs without touching shared code.
- **D-05:** `node_remediator.py` and `node_agent.py` update their imports to pull these from `agent/shared/remediation.py`; their own behavior must not change (cooldown, PDB checks, replica floor, learning_mode gating all identical to today).

**Generic agentic tool-use loop**
- **D-06:** Extract the iteration loop from `node_agent.py::run_incident` (message accumulation, `MAX_ITERATIONS` cap, `finish_incident` handling, no-tool-call break, max-iterations auto-escalation) into `agent/shared/agent_loop.py::run_tool_loop(anthropic_client, redis, bundle, incident_id, *, system_prompt, tools, dispatch_tool, format_bundle, learning_mode, max_iterations)`.
- **D-07:** `node_agent.py` keeps only its domain-specific pieces: `_SYSTEM_PROMPT`, `TOOLS`, `_format_bundle`, `_dispatch_tool`, and a thin `run_incident` that sets up the K8s API clients (`core_v1`/`apps_v1`/`policy_v1`) as `ctx` and calls the shared loop.

**Generic diagnoser call**
- **D-08:** Extract the "call Claude with cached system prompt + forced `tool_choice` to `submit_diagnosis`, parse `DiagnosisResult`" boilerplate from `node_diagnoser.py::diagnose` into `agent/shared/diagnoser_base.py::diagnose_with_claude(client, system_prompt, user_text, result_model=DiagnosisResult)`.
- **D-09:** `node_diagnoser.py` keeps only `_SYSTEM_PROMPT` and `_format_bundle`, calling the shared helper.

**Domain registry + generic orchestrator**
- **D-10:** Define a `DomainConfig` dataclass (new `agent/registry.py`): `domain`, `stream`, `consumer_group`, `context_fetcher`, `diagnose`, `remediate`, `run_incident`, `escalate_below` (threshold dict reference), `urgency_map`.
- **D-11:** Extract the debounce/consumer-group loop from `k8s_orchestrator.py::run` into `agent/orchestrators/generic_orchestrator.py::run(redis, anthropic_client, config: DomainConfig, *, debounce_seconds, learning_mode, mode)`.
- **D-12:** `k8s_orchestrator.py` shrinks to its k8s-specific `_fetch_pods_on_nodes` context fetcher plus its `DomainConfig` registration in `agent/registry.py`. Claude's discretion on whether the file is renamed, merged into `agent/skills/diagnosers/node_diagnoser.py`, or kept as-is — as long as the generic loop itself is not duplicated per domain.
- **D-13:** `agent/skills/remediators/human_escalator.py::_URGENCY_MAP` becomes per-domain, looked up via the domain's `DomainConfig.urgency_map` (registry-driven) instead of a single flat k8s-only dict — but keep the existing fallback to `"p3_within_1h"` when an event_type has no mapping.

**Runner generalization + documentation**
- **D-14:** `scripts/run_local.py` iterates registered domains from `agent/registry.py` to start one observer task + one `generic_orchestrator.run(...)` task per domain, replacing the current hardcoded two-task `TaskGroup`. Add a `--monitors k8s,...` flag (comma list) defaulting to all registered domains.
- **D-15:** Write `.planning/ADDING-A-MONITOR.md` — the concrete recipe for adding a new domain (new observer file, new diagnoser file, new remediator file, optional agent file, one `DomainConfig` registration). This doc is the checklist for the 7 remaining observers.

**Backoff helper**
- **D-16:** `_backoff` (exponential backoff with jitter, currently in `k8s_node_observer.py`) is generic and reusable — extract to `agent/shared/backoff.py` if a second observer in this phase would otherwise duplicate it. If no second observer is being written in this phase, this extraction is optional/Claude's discretion (low priority, do only if cheap).

### Claude's Discretion
- Exact internal structure/field types of `DomainConfig` beyond what D-10 specifies.
- Whether `k8s_orchestrator.py` is kept, renamed, or merged (D-12).
- File/module naming for the new `agent/shared/*.py` helper modules beyond what's specified (`remediation.py`, `agent_loop.py`, `diagnoser_base.py`, optionally `backoff.py`).
- Whether to do the `_backoff` extraction now (D-16) or defer it.
- Ordering/wave structure of tasks, as long as the K8s node monitor (`mode="diagnoser"` and `mode="agent"`) keeps working at every step.

### Deferred Ideas (OUT OF SCOPE)
- Actual implementation of OBS-01 (K8s pod observer), OBS-03–OBS-08 (EC2, FastAPI, Postgres, log analyzer, security group auditor, secrets checker) and their corresponding REM-02–REM-08 remediators. This phase builds the framework and the `ADDING-A-MONITOR.md` recipe only.
- INFRA-01–INFRA-06 (dedicated EC2 instance, Prometheus/Alertmanager, CloudWatch agent, dedicated monitoring Postgres, Redis deployment, heartbeat/watchdog) — deployment/ops setup tasks, not part of this code refactor.
</user_constraints>

<phase_requirements>
## Phase Requirements

The objective lists 15 requirement IDs as "MUST address," but CONTEXT.md's `<deferred>` section explicitly scopes most of them out of THIS refactor. The honest mapping the planner should use:

| ID | Description | Research Support (how this refactor relates) |
|----|-------------|----------------------------------------------|
| OBS-02 | K8s node observer watches node conditions | **Partially satisfied / preserved.** Already built (`k8s_node_observer.py`). This refactor migrates it onto the new framework with **zero behavior change** — it remains the regression baseline. Counts as "implemented + migrated," not new work. |
| OBS-09 | All observer signals normalized to shared `InfraEvent` schema | **Already satisfied** by `agent/shared/models.py::InfraEvent` + `event_bus.py::publish`. Refactor does not change the schema (note: D-01 changes `DiagnosisResult`, NOT `InfraEvent`). |
| OBS-01 | K8s pod observer | **Deferred** (CONTEXT `<deferred>`). Refactor *enables* it cheaply (reuses `domain="k8s"` registry entry). Framework lays groundwork; no implementation here. |
| OBS-03..OBS-08 | EC2, FastAPI, Postgres, log analyzer, sec-group, secrets | **Deferred.** Framework + `ADDING-A-MONITOR.md` (D-15) is the groundwork. The acceptance test (could EC2 `reboot_instance` / Postgres `kill_query` flow through unchanged core) validates the framework can host them. |
| INFRA-01..INFRA-06 | EC2 instance, Prometheus, CloudWatch, monitoring Postgres, Redis deploy, heartbeat | **Deferred — out of scope.** Deployment/ops tasks, not code-architecture refactor. |

**Planning recommendation:** Do NOT claim INFRA-01..06 or OBS-01/03..08 as "completed" by this phase. Represent this phase honestly as: *"Migrates OBS-02 onto a reusable framework (behavior-preserving) and builds the groundwork + documented recipe (D-15) that makes the deferred OBS/REM requirements small mechanical additions."* The deferred IDs stay `Pending` in REQUIREMENTS.md traceability; a future phase closes them. Surface this in PLAN.md's requirement-coverage section so verify-work does not flag missing INFRA/OBS implementations as a defect.
</phase_requirements>

## Summary

This is a **behavior-preserving refactor of internal Python code** — no new libraries, no external dependencies, no network research required. The existing K8s node monitor is a clean, well-factored pipeline (observer → event bus → orchestrator → diagnoser+remediator OR agent loop → human escalator). The work is to lift the domain-agnostic mechanics out of the K8s-specific files into shared modules and a registry, then re-wire K8s as the first (and only, this phase) registered domain.

The single highest-risk decision is **D-01 (open `action_type` from `Literal` to `str`)**, and the risk is specific and verified: `node_diagnoser.py` passes `DiagnosisResult.model_json_schema()` directly as the Claude tool `input_schema`. With the `Literal`, that schema contains `"enum": ["pod_restart", ...]`, which API-constrains Claude to those four values. Opening to `str` **removes the enum**, so the API no longer forces the action vocabulary — only the system-prompt prose does. This is the one place where a "pure extraction" decision actually changes the contract with the model. Everything else (D-03 through D-16) is mechanical extraction with import re-wiring and can be verified by import + behavior-equivalence checks.

There are **no existing tests** (no `pytest`, no test files, no test config — verified). The codebase relies on `scripts/run_local.py` as a manual end-to-end harness. This is the largest planning gap: a behavior-preserving refactor with no regression net. The plan should add a minimal pytest harness (Wave 0) covering the load-bearing pure-Python logic (`_should_escalate`, `_meets_threshold`, `_derive_urgency`, `_parse_target`, threshold lookups, `DomainConfig` registration, registry-driven runner construction) so each extraction step can be proven equivalent without a live cluster.

**Primary recommendation:** Sequence the work as (0) add a pytest safety net for pure logic, (1) D-03/D-04/D-05 remediation-primitive extraction (lowest risk, unblocks everything), (2) D-08/D-09 diagnoser_base + D-06/D-07 agent_loop, (3) D-10/D-11/D-12/D-13 registry + generic orchestrator + per-domain urgency, (4) D-01/D-02 open action_type with boundary validation added at the same commit, (5) D-14 runner + D-15 doc, (6) D-16 backoff (optional). Keep `mode="diagnoser"` and `mode="agent"` runnable via `run_local.py` after every step.

## Architectural Responsibility Map

This is a single-process async Python app (one tier). The "tiers" here are the internal pipeline layers, mapped to where each capability must live after the refactor.

| Capability | Primary Layer (owner) | Secondary Layer | Rationale |
|------------|----------------------|-----------------|-----------|
| Detect infra conditions, emit `InfraEvent` | Observer (`agent/observers/*`) | Event bus (`shared/event_bus.py`) | Domain-specific watch/poll logic stays per-domain; publish is already shared. |
| Exponential backoff w/ jitter | `shared/backoff.py` (D-16) | Observer | Pure utility, reusable — but extraction optional this phase (only 1 observer). |
| Debounce + consumer-group loop + bundling | `orchestrators/generic_orchestrator.py` (D-11) | Registry | The loop is fully domain-agnostic once stream/group/fetcher/handlers come from `DomainConfig`. |
| Per-domain context fetch (e.g. pods-on-node) | Domain orchestrator/module (`_fetch_pods_on_nodes`, D-12) | Registry (`context_fetcher` field) | K8s-API-specific; cannot be generic. Wired in via `DomainConfig`. |
| Call Claude for one-shot diagnosis | `shared/diagnoser_base.py` (D-08) | Diagnoser | Boilerplate (caching, forced tool_choice, parse) is identical across domains. |
| Domain diagnosis prompt + bundle formatting | Diagnoser (`_SYSTEM_PROMPT`, `_format_bundle`, D-09) | — | Domain-specific knowledge; stays per-domain. |
| Agentic tool-use loop control flow | `shared/agent_loop.py` (D-06) | Agent | Iteration/finish/escalate-on-timeout mechanics are domain-agnostic. |
| Domain tools + dispatch + ctx setup | Agent (`TOOLS`, `_dispatch_tool`, `_SYSTEM_PROMPT`, D-07) | — | Tool bodies are K8s-API-specific. |
| Threshold registry + `_meets_threshold` + `_log_action` + `PreflightResult` | `shared/remediation.py` (D-03/D-04) | Registry | Shape is generic; thresholds become `domain→action→float`. |
| Preflight + execute bodies (cooldown, PDB, scale floor) | Remediator (`node_remediator.py`) | — | K8s-API-specific; stays per-domain (D-05: behavior unchanged). |
| `action_type` validity check | Orchestrator/remediation boundary (D-01) | Registry (threshold keys) | After opening to `str`, validity is enforced against registered threshold keys here. |
| Absolute forbidden-op floor | `shared/safety.py` (UNCHANGED) | — | Domain-agnostic hard floor; must NOT move or weaken. |
| Per-domain urgency mapping | `human_escalator.py` + `DomainConfig.urgency_map` (D-13) | Registry | Urgency derivation logic stays in escalator; the map comes from the registry. |
| Domain wiring (single source of truth) | `agent/registry.py` (NEW, D-10) | — | One `DomainConfig` per domain; the integration point for all new monitors. |
| Process launch (one obs + one orch per domain) | `scripts/run_local.py` (D-14) | Registry | Reads registry, builds TaskGroup dynamically. |

## Standard Stack

No new packages. The existing stack is already correct and current for this refactor.

### Core (already in `requirements-monitor.txt`, versions VERIFIED in `.venv`)
| Library | Installed Version | Purpose | Notes |
|---------|-------------------|---------|-------|
| `anthropic` | 0.109.1 [VERIFIED: .venv pip show] | Claude API client (diagnoser + agent loop) | Async client (`AsyncAnthropic`). req pins `>=0.40.0`. |
| `kubernetes-asyncio` | (req `>=30.0.0`) [VERIFIED: requirements-monitor.txt] | Async K8s API + watch | Stays in per-domain files only; must NOT leak into `shared/*`. |
| `redis[asyncio]` | (req `>=5.0.0`) [VERIFIED: requirements-monitor.txt] | Redis Streams event bus + actions/escalations logs | `redis.asyncio.Redis`. |
| `pydantic` | 2.13.4 [VERIFIED: .venv pip show] | `DiagnosisResult` model + JSON-schema for tool_use | v2 API (`model_json_schema`, `model_validate`). |
| `structlog` | (req `>=24.0.0`) [VERIFIED: requirements-monitor.txt] | Structured logging | Used everywhere. |
| `python-dotenv` | (req `>=1.0.0`) [VERIFIED: requirements-monitor.txt] | `.env` loading | `.env.example` present in repo root. |

**Runtime:** Python 3.14.3 [VERIFIED: `.venv/bin/python --version`]. The code uses 3.11+ features (`asyncio.TaskGroup`, `except*` star-syntax in `run_local.py`) — keep targeting 3.11+.

### Supporting (test net — to be ADDED in Wave 0)
| Library | Suggested Version | Purpose | When to Use |
|---------|-------------------|---------|-------------|
| `pytest` | latest 8.x [ASSUMED] | Unit harness for pure logic (none exists today) | Wave 0 — verify version with `pip index versions pytest` before pinning. |
| `pytest-asyncio` | latest [ASSUMED] | Test the async helpers without a live cluster | Only if testing async funcs directly; most load-bearing logic (`_should_escalate`, `_meets_threshold`, `_derive_urgency`, `_parse_target`, registry construction) is **synchronous** and testable with plain pytest. |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| pytest | unittest (stdlib, zero deps) | unittest avoids adding a dep, but pytest fixtures/parametrize make threshold-matrix tests far cleaner. Recommend pytest. |
| Opening `action_type` to bare `str` (D-01) | `str` constrained at validation boundary | D-01 is locked — bare `str` on the model, validity enforced at boundary. No alternative to research. |

**Installation (Wave 0 test net only):**
```bash
pip install pytest pytest-asyncio   # verify exact versions before pinning into requirements-dev.txt
```

## Package Legitimacy Audit

Only two *new* packages are contemplated (test tooling), and both are ubiquitous, long-lived PyPI packages. slopcheck was not run in this session (no external package discovery occurred — these are well-known stdlib-adjacent test tools), so they are tagged `[ASSUMED]` and the planner should gate the install behind a normal verification step.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| pytest | PyPI | 15+ yrs [ASSUMED] | ~100M/wk [ASSUMED] | github.com/pytest-dev/pytest | not run | Approved (verify version) |
| pytest-asyncio | PyPI | 8+ yrs [ASSUMED] | ~20M/wk [ASSUMED] | github.com/pytest-dev/pytest-asyncio | not run | Approved (verify version) |

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

*No runtime packages are added by this phase — all 6 runtime deps already exist in `requirements-monitor.txt` and are verified installed. Test deps should go in a separate `requirements-dev.txt`, not the runtime file.*

## Concrete Refactor Map (code-level boundaries)

This is the heart of the research: exactly what moves where, with function names and signatures. Line numbers are as of the files read 2026-06-15.

### `agent/shared/models.py` — D-01/D-02
- **Change:** `DiagnosisResult.action_type` (lines 48–50) from `Literal["pod_restart","deployment_scale_down","human_escalate","observe_only"]` → `str` (keep the `Field(description=...)`).
- **Do NOT touch:** `InfraEvent.domain` (line 11, still `Literal["k8s","db","infra","app"]`), `InfraEvent.severity`, `HumanEscalationPacket.urgency`. D-01 is scoped to `DiagnosisResult.action_type` ONLY.
- **VERIFIED behavior change:** `DiagnosisResult.model_json_schema()` is used as the tool `input_schema` in `node_diagnoser.py:136`. With `Literal`, the schema's `action_type` is `{"enum": [...], "type":"string"}`. With `str`, it is `{"type":"string"}` (no enum) — confirmed by running pydantic 2.13.4 locally. **Consequence:** Claude is no longer API-constrained to the 4 actions; only `_SYSTEM_PROMPT` prose (node_diagnoser lines 54–58) constrains it. This is acceptable per D-01's design (validity enforced at the boundary) but is a real contract change — see Pitfall 1.

### `agent/skills/remediators/node_remediator.py` → `agent/shared/remediation.py` — D-03/D-04/D-05
Extract (move OUT of node_remediator, INTO shared/remediation.py):
- `PreflightResult` dataclass (lines 41–44)
- `_meets_threshold(action_type, confidence)` (lines 47–49) — must be updated to take a `domain` arg or look up nested `THRESHOLDS[domain][action_type]` per D-04.
- `_log_action(redis, incident_id, domain, action, status, confidence, detail)` (lines 52–74) — already takes `domain`; moves as-is.
- `THRESHOLDS` (lines 31–35) → reshape to `dict[str, dict[str, float]]`, e.g. `{"k8s": {"pod_restart": 0.80, "deployment_scale_down": 0.85, "human_escalate": 0.00}}` (D-04).

Stay in `node_remediator.py` (K8s-specific bodies — D-05, behavior unchanged):
- `RESTART_ANNOTATION`, `RESTART_COOLDOWN_SECONDS` (lines 37–38)
- `_load_k8s_config` (77–81), `_parse_target` (84–92), `_check_pdb` (95–113)
- `_preflight_pod_restart` (116–145), `_preflight_scale_down` (148–167)
- `_execute_pod_restart` (170–173), `_execute_scale_down` (176–182)
- `remediate(...)` (185–325) — update its `THRESHOLDS`/`_meets_threshold`/`_log_action`/`PreflightResult` references to import from `shared/remediation.py`; `THRESHOLDS.get(action,...)` calls (lines 226, 48) must become domain-keyed (`THRESHOLDS["k8s"][action]`).

### `agent/skills/agents/node_agent.py` — D-05/D-06/D-07
- **Imports to re-point (lines 31–40):** currently imports `THRESHOLDS, _execute_pod_restart, _execute_scale_down, _load_k8s_config, _log_action, _meets_threshold, _preflight_pod_restart, _preflight_scale_down` from `node_remediator`. After D-03, `THRESHOLDS`/`_meets_threshold`/`_log_action` come from `shared/remediation.py`; the K8s `_execute_*`/`_preflight_*`/`_load_k8s_config` still come from `node_remediator`. Note `THRESHOLDS[action]` references at lines 331, 375 must become `THRESHOLDS["k8s"][action]`.
- **Extract to `agent/shared/agent_loop.py::run_tool_loop` (D-06):** the loop body of `run_incident` — message list init (line 516), the `for iteration in range(1, MAX_ITERATIONS+1)` loop (530–599), `messages.create` call, `finish_incident` detection (553–560, 582–592), no-tool-call break (594–597), and the post-loop `_escalate_unresolved` auto-escalation (601–603). Signature per D-06: `run_tool_loop(anthropic_client, redis, bundle, incident_id, *, system_prompt, tools, dispatch_tool, format_bundle, learning_mode, max_iterations)`. The `ctx` dict and the `async with client.ApiClient()` block (519–528) are K8s-specific setup and stay in `node_agent.run_incident`, which calls the shared loop passing its `ctx` through (the loop must accept/thread a ctx or call `dispatch_tool(name, input, ctx)`).
- **Stays in node_agent (D-07):** `MAX_ITERATIONS` (or pass through), `_SYSTEM_PROMPT` (46–111), `TOOLS` (113–245), `_format_bundle` (248–269), all `_tool_*` functions (272–459), `_dispatch_tool` (462–475), `_escalate_unresolved` (478–490 — or make this a generic fallback inside the loop), thin `run_incident` (493–...).
- **Design note:** `_escalate_unresolved` and the `escalate_human` tool both call `human_escalator.escalate(..., domain="k8s", ...)` with hardcoded `domain="k8s"`. The generic loop should receive `domain` (from `DomainConfig`) so the auto-escalation isn't K8s-pinned.

### `agent/skills/diagnosers/node_diagnoser.py` → `agent/shared/diagnoser_base.py` — D-08/D-09
- **Extract to `diagnose_with_claude(client, system_prompt, user_text, result_model=DiagnosisResult)`:** the `client.messages.create` call (lines 121–140) with cached system prompt + `tool_choice={"type":"tool","name":"submit_diagnosis"}`, the token-usage logging (142–148), and the tool_use parse/`model_validate` loop (150–161). Use `result_model.model_json_schema()` so the helper is model-agnostic.
- **Stays in node_diagnoser (D-09):** `_SYSTEM_PROMPT` (19–62), `_format_bundle(bundle, pods_on_node)` (65–98), and a thin `diagnose(client, bundle, pods_on_node)` (101–...) that builds `user_text` and delegates to `diagnose_with_claude`.
- **Note:** `model="claude-sonnet-4-6"` is hardcoded inside the create call. Decide whether the model name becomes a param of `diagnose_with_claude` (recommended for future Opus-cross-domain in DIAG-03) or stays defaulted. Either is fine; flag it as a small design choice, not a behavior change.

### `agent/orchestrators/k8s_orchestrator.py` → `agent/orchestrators/generic_orchestrator.py` + `agent/registry.py` — D-10/D-11/D-12
- **Extract to `generic_orchestrator.run(redis, anthropic_client, config: DomainConfig, *, debounce_seconds, learning_mode, mode)` (D-11):** the whole consume/debounce/bundle/dispatch loop (`run`, lines 180–259) plus `_handle_bundle` (124–177) plus `_decode_event` (72–83). These reference `STREAM`/`CONSUMER_GROUP`/`CONSUMER_NAME` (41–43, must come from `config.stream`/`config.consumer_group`) and call `node_diagnoser.diagnose`, `node_remediator.remediate`, `node_agent.run_incident`, `_fetch_pods_on_nodes`, `human_escalator.escalate` (all must come via `config.diagnose`/`config.remediate`/`config.run_incident`/`config.context_fetcher`). `_should_escalate` (54–69) uses module-level `_ESCALATE_BELOW` (46–51) which must become `config.escalate_below`. `_decode_event` hardcodes `domain="k8s"` (line 74) — derive from `config.domain`. `SignalBundle(domain="k8s",...)` (247) likewise.
- **Stays / shrinks in k8s_orchestrator.py (D-12, Claude's discretion on file fate):** `_fetch_pods_on_nodes(node_names)` (86–122) — the K8s context fetcher — plus the K8s `DomainConfig(...)` registration (could live here or in `registry.py`).
- **New `agent/registry.py` (D-10):** `DomainConfig` dataclass with fields `domain, stream, consumer_group, context_fetcher, diagnose, remediate, run_incident, escalate_below, urgency_map`, plus a module-level registry dict/list and a `register()` / `REGISTRY` that `run_local.py` iterates. See DomainConfig design notes below.

### `agent/skills/remediators/human_escalator.py` — D-13
- `_URGENCY_MAP` (24–29) and `_derive_urgency(signals)` (32–41) currently use a single flat K8s-only map. D-13: `escalate(...)` (44–...) gains access to the per-domain urgency map via the registry. Cleanest: change `_derive_urgency(signals, urgency_map)` to take the map as an arg, and have the orchestrator pass `config.urgency_map`. Keep the `"p3_within_1h"` fallback (line 41). The K8s map (`node_not_ready`→p1, `node_*_pressure`→p2) moves into the K8s `DomainConfig.urgency_map`. Note: the K8s map currently has NO entry for `node_network_unavailable` or `node_deleted` — they fall through to `p3_within_1h` today; preserve that exactly unless intentionally changed (see Pitfall 3).
- **Also note:** `escalate` builds `summary` with hardcoded "node signal(s)" text (line 67). For genericity that string should be domain-neutral, but changing it is a (minor) behavior change — decide explicitly.

### `scripts/run_local.py` — D-14
- **Current (lines 152–163):** hardcoded `TaskGroup` with `watch_nodes`, `run_orchestrator(...)`, plus three tailers. Imports `watch_nodes` and `run as run_orchestrator` directly (38–39).
- **D-14 target:** iterate `registry.REGISTRY`; for each `DomainConfig` start one observer task (the observer callable must be referenced from the registry or a parallel observer map) + one `generic_orchestrator.run(redis, client, config, ...)` task. Add `--monitors k8s,...` (comma list, default all registered). Keep the tailer tasks (they're a dev convenience; today they hardcode `k8s` stream names at lines 73, 91 — generalize or keep K8s-only since K8s is the only domain this phase).
- **Gap:** `DomainConfig` (D-10) lists `context_fetcher, diagnose, remediate, run_incident` but NOT the observer callable. The runner needs to know each domain's observer entrypoint. Recommend adding an `observer` field to `DomainConfig` (Claude's discretion per D-10) OR a separate observer registry. Flag this for the planner — it's the one field D-10 omits that D-14 needs.

### `agent/observers/k8s_node_observer.py` — D-16 (optional)
- `_backoff(attempt)` (78–81) is the only generic piece. Extract to `agent/shared/backoff.py` only if cheap; otherwise defer (no second observer this phase, so duplication risk is zero). Recommend **defer** unless the plan is already touching this file for another reason.

## DomainConfig Design Considerations (D-10)

Given only `domain="k8s"` is registered this phase, keep the dataclass minimal but future-proof. Required-now vs deferrable:

**Strictly needed now (used by generic_orchestrator + run_local):**
- `domain: str` — used for `InfraEvent`/`SignalBundle` domain, `_log_action`, escalation stream key.
- `stream: str` — e.g. `"events:k8s"` (consumed by the loop).
- `consumer_group: str` — e.g. `"k8s-orchestrator"`.
- `context_fetcher: Callable[[set[str]], Awaitable[list[dict]]]` — `_fetch_pods_on_nodes` for k8s; for domains with no pre-fetch (e.g. EC2), allow `None` and have the loop pass `[]`.
- `diagnose: Callable` — `node_diagnoser.diagnose` (signature `(client, bundle, context) -> DiagnosisResult`).
- `remediate: Callable` — `node_remediator.remediate`.
- `run_incident: Callable` — `node_agent.run_incident` (for `mode="agent"`).
- `escalate_below: dict[str, float]` — the `_ESCALATE_BELOW` map (per-action escalation thresholds); reference into / mirror of `THRESHOLDS[domain]`.
- `urgency_map: dict[str, str]` — per-domain event_type→urgency (D-13).

**Recommended addition (D-14 needs it):**
- `observer: Callable[[Redis], Awaitable[None]]` — the observer entrypoint (`watch_nodes`). D-10's field list omits this but D-14 requires it. Add it (Claude's discretion permits extending the dataclass).

**Deferrable (don't add until a domain needs it):**
- Model selection (sonnet vs opus per DIAG-03), debounce override per domain, severity routing, separate `consumer_name`. Hardcode/default these now.

**Consistency risk:** `escalate_below` (orchestrator routing thresholds) and `THRESHOLDS[domain]` (remediator gating thresholds) are TWO separate dicts today (`_ESCALATE_BELOW` in k8s_orchestrator has `human_escalate:1.1, observe_only:1.1`; `THRESHOLDS` in node_remediator has `human_escalate:0.00`). They overlap but are not identical. **Do not blindly merge them** — they serve different purposes (routing vs execution-gate) and have intentionally different sentinel values. Keep both, reference both from the registry.

## Architecture Patterns

### System Architecture Diagram (post-refactor)

```
                         ┌──────────────────────────────────────────┐
                         │           agent/registry.py                │
                         │   REGISTRY = [ DomainConfig(domain="k8s",  │
                         │     stream, group, observer, context_      │
                         │     fetcher, diagnose, remediate,          │
                         │     run_incident, escalate_below,          │
                         │     urgency_map) , ...future... ]          │
                         └───────────────┬────────────────────────────┘
                                         │ iterated by
                                         ▼
   scripts/run_local.py ── for each DomainConfig ──► TaskGroup:
        │                                                 │
        │  spawns per domain:                             │
        │   ┌─────────────────────────┐   ┌──────────────┴───────────────────┐
        ▼   ▼                         ▼   ▼                                   │
  config.observer(redis)        generic_orchestrator.run(redis, client, config)
  (e.g. watch_nodes)                   │
        │ K8s watch API                │ XREADGROUP config.stream / config.consumer_group
        │ build problem dict           │ (block 1s, count 20)
        ▼                              ▼
  InfraEvent ──publish()──►  events:<domain> (Redis Stream)  ──► debounce window (Ns)
  (shared/event_bus.py)                                            │ bundle when elapsed
                                                                   ▼
                                                          SignalBundle(domain, signals)
                                                                   │
                                       mode="diagnoser" ◄──────────┴──────────► mode="agent"
                                                │                                     │
                              config.context_fetcher(nodes)                 config.run_incident(...)
                                                │                            = node_agent.run_incident
                              config.diagnose(client,bundle,ctx)                     │ sets up ctx
                              = node_diagnoser.diagnose                               ▼
                                   │ (shared/diagnoser_base                shared/agent_loop.run_tool_loop
                                   │  .diagnose_with_claude →               (iterate, dispatch_tool,
                                   │  Claude + submit_diagnosis tool)        finish_incident, auto-escalate)
                                   ▼                                                  │
                              DiagnosisResult                                         │ each mutating tool:
                                   │                                                  ▼
                    _should_escalate(diag, config.escalate_below)         safety_check → threshold →
                          │ escalate?          │ no                       learning_mode → preflight → execute
                          ▼                    ▼                                       │
              human_escalator.escalate    config.remediate(...)                        │
              (domain, urgency_map)        = node_remediator.remediate                 │
                          │                    │ safety_check (shared/safety.py)        │
                          │                    │ threshold (shared/remediation)         │
                          ▼                    │ learning_mode gate                     ▼
              escalations:<domain>             │ preflight (K8s: cooldown/PDB/floor)  actions:log
              + actions:log                    ▼ execute (patch deployment)          (shared audit)
                                          actions:log

   ════════════════════════════════════════════════════════════════════════════════
   shared/safety.py::safety_check + FORBIDDEN_OPERATIONS  =  HARD FLOOR (UNCHANGED)
   called by both remediator and every mutating agent tool — cannot be bypassed
```

### Recommended Project Structure (after refactor)
```
agent/
├── registry.py                          # NEW (D-10) — DomainConfig + REGISTRY
├── observers/
│   └── k8s_node_observer.py             # unchanged (D-16 _backoff extraction optional)
├── orchestrators/
│   ├── generic_orchestrator.py          # NEW (D-11) — the domain-agnostic loop
│   └── k8s_orchestrator.py              # shrinks (D-12) → _fetch_pods_on_nodes + registration
├── shared/
│   ├── models.py                        # D-01: action_type Literal→str
│   ├── safety.py                        # UNCHANGED (hard floor)
│   ├── event_bus.py                     # UNCHANGED (already generic)
│   ├── remediation.py                   # NEW (D-03/D-04) — PreflightResult, _meets_threshold, _log_action, THRESHOLDS
│   ├── diagnoser_base.py                # NEW (D-08) — diagnose_with_claude
│   ├── agent_loop.py                    # NEW (D-06) — run_tool_loop
│   └── backoff.py                       # NEW (D-16, OPTIONAL) — _backoff
└── skills/
    ├── diagnosers/node_diagnoser.py     # D-09: prompt + _format_bundle only
    ├── remediators/
    │   ├── node_remediator.py           # D-05: K8s bodies, imports from shared/remediation
    │   └── human_escalator.py           # D-13: urgency_map per-domain
    └── agents/node_agent.py             # D-07: prompt + TOOLS + dispatch + thin run_incident
scripts/run_local.py                     # D-14: registry-driven TaskGroup + --monitors
tests/                                   # NEW (Wave 0) — pytest net for pure logic
.planning/ADDING-A-MONITOR.md            # NEW (D-15) — the recipe
requirements-dev.txt                     # NEW (Wave 0) — pytest deps
```

### Pattern 1: Callable-table registry (chosen pattern)
**What:** `DomainConfig` is a dataclass of callables + config strings; the generic orchestrator/runner are parameterized by it. This is dependency injection via a plain dataclass — no plugins, no entry-points, no dynamic import magic.
**When to use:** Small, known set of domains (8 max), all in one repo. Exactly this case.
**Example:**
```python
# agent/registry.py  (illustrative — derived from existing code shapes, not copied from external docs)
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

@dataclass
class DomainConfig:
    domain: str
    stream: str
    consumer_group: str
    observer: Callable[..., Awaitable[None]]          # D-14 needs this (D-10 omits it)
    diagnose: Callable[..., Awaitable]                 # node_diagnoser.diagnose
    remediate: Callable[..., Awaitable]                # node_remediator.remediate
    run_incident: Callable[..., Awaitable]             # node_agent.run_incident
    escalate_below: dict[str, float]
    urgency_map: dict[str, str]
    context_fetcher: Optional[Callable[..., Awaitable[list[dict]]]] = None

REGISTRY: dict[str, DomainConfig] = {}

def register(cfg: DomainConfig) -> None:
    REGISTRY[cfg.domain] = cfg
```

### Anti-Patterns to Avoid
- **Leaking `kubernetes_asyncio` into `shared/*`:** the whole point of the refactor. `shared/remediation.py`, `shared/diagnoser_base.py`, `shared/agent_loop.py`, `shared/backoff.py` must have ZERO `kubernetes_asyncio` imports. `_preflight_*`/`_execute_*`/`_load_k8s_config` use `client.AppsV1Api` etc. and therefore MUST stay in `node_remediator.py`. Verify with `grep -L kubernetes_asyncio agent/shared/*.py` after each step.
- **Merging the two threshold dicts:** `_ESCALATE_BELOW` (routing) ≠ `THRESHOLDS` (execution gate). Different sentinels (1.1 vs 0.00). Keep separate.
- **Auto-escalation pinned to `domain="k8s"`:** `node_agent._escalate_unresolved` and `_tool_escalate_human` hardcode `domain="k8s"`. When the loop goes generic, thread `domain` through so it isn't K8s-pinned.
- **Over-engineering `DomainConfig`:** don't add model-selection, per-domain debounce, or plugin discovery now. One domain registered; add fields when a domain needs them.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Action vocabulary validation after D-01 | A new enum / custom validator on the model | Boundary check against `THRESHOLDS[domain]` keys (already exists via `_meets_threshold` returning False on unknown) | The existing `_meets_threshold` returns `THRESHOLDS.get(action,1.0)` → unknown action can't meet threshold → routed to escalate. Reuse this; don't invent parallel validation. |
| Forbidden-op enforcement | Any new safety logic | `shared/safety.py::safety_check` (unchanged) | It's the hard floor by design; both remediator and every agent tool already call it. Do not add a second gate. |
| Plugin/registry machinery | `importlib`/entry-points/auto-discovery | Plain dataclass + dict registry | 8 known domains in one repo. Dynamic discovery adds failure modes for zero benefit. |
| Event bus / streams | Custom pub-sub | `shared/event_bus.py` (already generic) | Done. No changes. |
| Test runner | Hand-rolled asserts in `__main__` | pytest | Parametrized threshold matrices are trivial in pytest. |

**Key insight:** Almost everything generic already exists or is a copy-paste-move. The temptation in this phase is to "improve" while extracting (rename actions, merge dicts, change summary strings, add config fields). Resist — the acceptance criterion is *behavior unchanged*, and every unforced change adds regression surface against a codebase with no tests.

## Runtime State Inventory

This is a code refactor (rename/move of internal modules). Runtime state audit:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | Redis Streams: `events:k8s`, `escalations:k8s`, `actions:log`. The refactor changes stream *derivation* (now from `config.stream`) but the K8s domain must produce the SAME keys (`events:k8s` etc.). No data migration — keys unchanged for the existing domain. | Verify K8s `DomainConfig.stream == "events:k8s"` and `consumer_group == "k8s-orchestrator"` (matches current constants k8s_orchestrator lines 41–42). |
| Live service config | None. No external service stores these module names. Local-only (minikube + local Redis). | None — verified: only integration points are Redis (ephemeral dev) and K8s API (read by name, not by our module names). |
| OS-registered state | None — `run_local.py` is run manually; no systemd/launchd/scheduler registration found. | None. |
| Secrets/env vars | `ANTHROPIC_API_KEY`, `REDIS_URL` (read by name in `run_local.py` lines 42, 131). Module renames don't touch these. `.env.example` present. | None — env var names unchanged. |
| Build artifacts / installed packages | `agent/` is imported via `sys.path.insert` (run_local.py:36), NOT installed as a package (no `pyproject.toml`/`setup.py` — verified). No egg-info to go stale. The new modules just need correct `__init__.py` coverage (existing `__init__.py` files cover all current dirs; new `tests/` and new shared modules are importable without new `__init__.py` since shared/ already has one). | Verify imports resolve after moves (`python -c "import agent.registry, agent.shared.remediation, agent.shared.agent_loop, agent.shared.diagnoser_base, agent.orchestrators.generic_orchestrator"`). |

**The canonical question — after every file is updated, what runtime systems still have the old string?** Answer: only Redis consumer-group offsets, which are tied to the *stream + group name* (both preserved for k8s), not to Python module names. A fresh `xgroup_create` is idempotent (the code already catches `ResponseError` for "group exists", k8s_orchestrator lines 201–205). No runtime state breaks.

## Common Pitfalls

### Pitfall 1: D-01 silently widens what Claude can emit
**What goes wrong:** After `action_type: str`, the diagnoser's tool `input_schema` loses its `enum`. Claude could return an action string the K8s domain doesn't recognize (e.g. a hallucinated `"reboot_node"`), which then fails threshold lookup and gets escalated — different from today where the API guaranteed one of 4 values.
**Why it happens:** `node_diagnoser.py:136` reuses `DiagnosisResult.model_json_schema()` as the tool schema; the enum came for free from the `Literal`. VERIFIED via local pydantic test.
**How to avoid:** (1) The K8s `_SYSTEM_PROMPT` already enumerates the 4 valid actions in prose (node_diagnoser 54–58) — keep it. (2) Rely on the existing boundary: unknown action → `_meets_threshold` returns False → `_should_escalate` escalates → safe. (3) Add a test that feeds a `DiagnosisResult(action_type="bogus")` through `_should_escalate`/`_meets_threshold` and asserts it escalates rather than executes. (4) Optionally, the diagnoser can still inject an explicit `enum` into the tool schema for its own domain (derive from `THRESHOLDS["k8s"].keys()`), getting the API constraint back per-domain without re-closing the model. Surface this as a design option for the planner.
**Warning signs:** Live run shows `not_implemented`/`threshold_not_met` statuses for K8s where it used to be clean; or Claude returning novel action strings in logs.

### Pitfall 2: kubernetes_asyncio leaking into shared modules
**What goes wrong:** While extracting `agent_loop.py` or `remediation.py`, a K8s import comes along (e.g. moving a `_preflight_*` by accident, or the `ctx` typing referencing `client.CoreV1Api`).
**Why it happens:** The agent loop dispatches K8s tools; it's tempting to move tool bodies too.
**How to avoid:** The loop must be fully generic — it only calls `dispatch_tool(name, input, ctx)` and never inspects ctx. Tool bodies and `client.ApiClient()` setup stay in `node_agent.py`. Verify: `grep -l kubernetes_asyncio agent/shared/*.py` should return nothing.
**Warning signs:** `import kubernetes_asyncio` appears in any `agent/shared/*.py`.

### Pitfall 3: Dropping existing urgency / escalation edge-cases during D-13
**What goes wrong:** The current `_URGENCY_MAP` has no entry for `node_network_unavailable` or `node_deleted` — they intentionally fall through to `p3_within_1h` today. If the planner "fills in the gaps" while moving the map, behavior changes.
**Why it happens:** Looks like an oversight; it's actually current behavior that must be preserved (CONTEXT requires K8s behavior identical, including the recent NetworkUnavailable/node_deleted additions).
**How to avoid:** Copy `_URGENCY_MAP` verbatim into the K8s `DomainConfig.urgency_map`; keep the `p3_within_1h` fallback. Add a test asserting `_derive_urgency` outputs match for each existing event_type including the fall-through cases.
**Warning signs:** An escalation for `node_deleted` shows a different urgency than before.

### Pitfall 4: Threshold dict shape change breaks call sites (D-04)
**What goes wrong:** `THRESHOLDS` goes from `dict[str,float]` to `dict[str,dict[str,float]]`. Every `THRESHOLDS.get(action,...)` / `THRESHOLDS[action]` call site breaks: node_remediator lines 48, 226; node_agent lines 331, 375.
**Why it happens:** Mechanical shape change with multiple consumers.
**How to avoid:** grep all `THRESHOLDS` usages before changing shape (4 sites found). Update `_meets_threshold` to take/know `domain`. Add a unit test for `_meets_threshold("k8s","pod_restart",0.81)==True` and `0.79==False`.
**Warning signs:** `KeyError`/`TypeError` on threshold lookup at runtime.

### Pitfall 5: No regression net for a behavior-preserving refactor
**What goes wrong:** Every extraction "looks right" but subtly changes routing; with no tests and only a manual `run_local.py` harness (which needs minikube + Redis + an API key), regressions ship silently.
**Why it happens:** No `pytest`, no test files exist (VERIFIED).
**How to avoid:** Wave 0 adds pytest covering the pure-Python load-bearing logic (see Validation Architecture). These run with no cluster/Redis/API key and pin behavior before extraction.
**Warning signs:** "It imports fine" treated as "it works."

## Code Examples

These are *current* patterns from the repo that the refactor must preserve (not external sources — this is an internal refactor).

### The escalation routing contract (must survive D-01/D-11) — `k8s_orchestrator.py:54-69`
```python
def _should_escalate(diagnosis: DiagnosisResult) -> tuple[bool, str]:
    if diagnosis.action_type == "human_escalate":
        return True, "Claude determined human intervention is required"
    if diagnosis.requires_human_review:
        return True, "Claude flagged requires_human_review=True"
    threshold = _ESCALATE_BELOW.get(diagnosis.action_type, 0.80)   # → config.escalate_below after D-11
    if diagnosis.confidence < threshold:
        return True, (f"confidence {diagnosis.confidence:.2f} < threshold {threshold} "
                      f"for action '{diagnosis.action_type}'")
    return False, ""
```
After D-01, the `.get(action, 0.80)` default is what catches unknown action strings (Pitfall 1) — a novel action falls to the 0.80 default and likely escalates. Preserve this default.

### The hard-floor pattern every mutating path repeats (must NOT be weakened) — `node_remediator.py:210-215` / `node_agent.py:324-328`
```python
try:
    safety_check(action, affected)          # shared/safety.py — unchanged
except SafetyViolation as exc:
    await _log_action(redis, incident_id, "k8s", action, "safety_blocked", confidence, str(exc))
    return "safety_blocked"
```

### Tool-loop shape to extract into run_tool_loop (D-06) — `node_agent.py:530-599`
The loop: `messages.create(..., tools=TOOLS)` → append assistant content → for each `tool_use` block: if `finish_incident` capture & ack, else `dispatch_tool` & collect `tool_result` → if finished return outcome → if no tool calls break → else append user `tool_results` and continue. Post-loop: auto-escalate. This entire control flow is domain-agnostic once `system_prompt`, `tools`, `dispatch_tool`, `format_bundle`, `domain` are parameters.

## State of the Art

| Old Approach | Current Approach | When | Impact |
|--------------|------------------|------|--------|
| `Literal` action_type closed at the model | Open `str`, validity at boundary (D-01) | This phase | Enables non-K8s domains; widens model output (Pitfall 1). |
| Per-domain copy of orchestrator/diagnoser/agent/remediator | Shared mechanics + per-domain config in registry | This phase | New monitor = 1 observer + thin diagnoser/remediator/agent + 1 registry entry. |
| Hardcoded 2-task TaskGroup | Registry-driven N-task launch (D-14) | This phase | Adding a domain auto-wires its runner. |

**Deprecated/outdated:** Nothing external. The only "outdated" thing is the K8s-specific coupling being removed. No deprecated library APIs in play (anthropic 0.109, pydantic 2.13, both current as installed).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | pytest 8.x / pytest-asyncio are the right test tools and current | Standard Stack | Low — ubiquitous; verify exact version with `pip index versions` before pinning. |
| A2 | Adding an `observer` field to `DomainConfig` is acceptable (D-10 omits it but D-14 needs it) | DomainConfig Design | Low — D-10 explicitly grants Claude's discretion on internal structure; flagged for planner confirmation. |
| A3 | Redis consumer-group offsets are keyed by stream+group, not module name, so module renames don't strand offsets | Runtime State Inventory | Low — standard Redis Streams semantics; the code already handles "group exists" idempotently. |
| A4 | Test deps belong in a new `requirements-dev.txt`, not the runtime `requirements-monitor.txt` | Standard Stack | None — purely organizational; planner may decide otherwise. |

**Note:** No external/web sources were consulted because this is a closed-world internal refactor. All technical claims are VERIFIED against the actual files read this session or VERIFIED by running pydantic locally; the few `[ASSUMED]` items above are test-tooling version details and a design-discretion field, all low-risk.

## Open Questions (RESOLVED)

1. **Should the diagnoser re-inject a per-domain `enum` into the tool schema after D-01?**
   - What we know: opening the model removes the API enum constraint (VERIFIED); the system prompt still lists valid actions; the boundary safely escalates unknowns.
   - What's unclear: whether the team wants belt-and-suspenders API enforcement per-domain (derive enum from `THRESHOLDS[domain].keys()`).
   - Recommendation: implement the boundary check (required by D-01) and add an optional per-domain enum injection in `diagnose_with_claude` if `result_model` exposes domain actions — but don't block on it. Note for discuss-phase.
   - **RESOLVED:** 01-05-PLAN.md implements per-domain enum re-injection in `diagnose_with_claude` alongside the D-01 boundary check.

2. **Fate of `k8s_orchestrator.py` (D-12 Claude's discretion: keep/rename/merge).**
   - What we know: only `_fetch_pods_on_nodes` + the K8s `DomainConfig` registration need a home.
   - Recommendation: keep the file (renamed conceptually to "k8s domain wiring"): `_fetch_pods_on_nodes` + the `register(DomainConfig(...))` call. Simplest, lowest churn, keeps `REMEDIATION-APPROACHES.md` references mostly valid. Update that doc's module references (D-12 / CONTEXT note about keeping it accurate).
   - **RESOLVED:** 01-04-PLAN.md keeps `k8s_orchestrator.py` as the K8s domain-wiring file (`_fetch_pods_on_nodes` + `DomainConfig` registration).

3. **Does the `escalate` summary string ("node signal(s)") get genericized now?** (human_escalator:67)
   - Recommendation: parameterize minimally (`f"{len(signals)} {domain} signal(s)"`) — tiny change, clearly an improvement for genericity, but flag it as an intentional (cosmetic) behavior change so verify-work doesn't treat differing summary text as a regression.
   - **RESOLVED:** 01-04-PLAN.md genericizes the summary string to `f"{len(signals)} {domain} signal(s)"`, documented as an intentional cosmetic change.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python | everything | ✓ | 3.14.3 (.venv) | — (code targets 3.11+) |
| anthropic | diagnoser, agent | ✓ | 0.109.1 | — |
| pydantic | models | ✓ | 2.13.4 | — |
| kubernetes-asyncio | k8s observer/remediator/agent | ✓ (in .venv per requirements) | >=30.0.0 | — (per-domain only) |
| redis[asyncio] | event bus | ✓ (lib) | >=5.0.0 | — |
| structlog | logging | ✓ | >=24.0.0 | — |
| Redis server | run_local end-to-end | ✗ at research time | — | Unit tests need NO Redis; only manual e2e does (docker run redis:7-alpine). |
| minikube / K8s | run_local end-to-end | ✗ at research time | — | Unit tests need NO cluster; only manual e2e does. |
| pytest | Wave 0 test net | ✗ (not installed) | — | Install in Wave 0; or stdlib unittest as fallback. |

**Missing dependencies with no fallback:** none that block THIS refactor — all extraction + unit testing is doable offline. (Redis + minikube + API key only needed for the optional manual `run_local.py` end-to-end smoke, which can be a checkpoint, not a per-task gate.)

**Missing dependencies with fallback:** pytest (install Wave 0, or unittest); Redis/minikube (only for manual e2e).

## Validation Architecture

`nyquist_validation: true` in config.json — section required.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.x [ASSUMED — verify version] (NONE exists today; chosen) |
| Config file | none — create `pyproject.toml [tool.pytest.ini_options]` or `pytest.ini` in Wave 0 |
| Quick run command | `python -m pytest tests/ -x -q` |
| Full suite command | `python -m pytest tests/ -v` |

### Phase Requirements → Test Map
The "requirements" for this refactor are *behavior-preservation invariants*. Map each load-bearing pure-logic unit to a test:

| Invariant | Behavior | Test Type | Automated Command | File Exists? |
|-----------|----------|-----------|-------------------|-------------|
| Threshold gating (D-04) | `_meets_threshold("k8s","pod_restart",0.81)` True, `0.79` False; unknown action False | unit | `pytest tests/test_remediation.py -x` | ❌ Wave 0 |
| Escalation routing (D-01/D-11) | `human_escalate`→escalate; conf<threshold→escalate; unknown action→escalate; valid high-conf→remediate | unit | `pytest tests/test_orchestrator_routing.py -x` | ❌ Wave 0 |
| Urgency derivation (D-13) | every current event_type maps to same urgency incl. `node_network_unavailable`/`node_deleted`→p3 fallback | unit | `pytest tests/test_escalator.py -x` | ❌ Wave 0 |
| Target parsing (preserved) | `_parse_target(["ns/dep"])==("ns","dep")`; bad input→None | unit | `pytest tests/test_remediation.py -x` | ❌ Wave 0 |
| action_type schema (D-01) | `DiagnosisResult(action_type="reboot_node")` validates (no enum) AND routes to escalate via boundary | unit | `pytest tests/test_models.py -x` | ❌ Wave 0 |
| Registry construction (D-10) | K8s `DomainConfig` has `stream=="events:k8s"`, `consumer_group=="k8s-orchestrator"`, callables wired | unit | `pytest tests/test_registry.py -x` | ❌ Wave 0 |
| Import integrity (all moves) | all new shared modules import; no `kubernetes_asyncio` in `agent/shared/*` | unit | `pytest tests/test_imports.py -x` | ❌ Wave 0 |
| End-to-end pipeline (manual) | run_local diagnoser + agent both still flow | manual-only (needs Redis+minikube+API key) | `python scripts/run_local.py` / `--agent` | n/a (checkpoint:human-verify) |

### Sampling Rate
- **Per task commit:** `python -m pytest tests/ -x -q`
- **Per wave merge:** `python -m pytest tests/ -v`
- **Phase gate:** Full suite green + one manual `run_local.py` (diagnoser) and `run_local.py --agent` smoke before `/gsd:verify-work` (checkpoint, since it needs live deps).

### Wave 0 Gaps
- [ ] `requirements-dev.txt` + `pip install pytest pytest-asyncio` (verify versions)
- [ ] `pyproject.toml [tool.pytest.ini_options]` (or `pytest.ini`) with `pythonpath = ["."]` so `import agent...` works
- [ ] `tests/__init__.py` (or rely on rootdir config), `tests/conftest.py` (shared fixtures: fake redis via simple async stub, sample `SignalBundle`/`DiagnosisResult`)
- [ ] `tests/test_remediation.py`, `test_orchestrator_routing.py`, `test_escalator.py`, `test_models.py`, `test_registry.py`, `test_imports.py`
- [ ] **Capture baseline FIRST:** write the routing/threshold/urgency tests against the *current* code and get them green BEFORE any extraction, so they prove equivalence after each move.

## Security Domain

`security_enforcement` not set in config.json → treat as enabled. This refactor's security surface is narrow (internal code, no new inputs/endpoints) but the SAFETY FLOOR is the whole product's reason for existing.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no (this phase) | K8s auth via in-cluster/kubeconfig; IAM role is INFRA-01 (deferred). No change here. |
| V3 Session Management | no | N/A — no sessions. |
| V4 Access Control | yes (critical) | `shared/safety.py::FORBIDDEN_OPERATIONS` is the access-control floor on what actions may execute. MUST remain unchanged and unbypassable after D-01 widens `action_type`. |
| V5 Input Validation | yes | Claude tool output is "input." After D-01, `action_type` is unbounded `str` — validated at the boundary (threshold lookup) and floored by `safety_check`. `DiagnosisResult` confidence stays pydantic-validated `0.0–1.0`. |
| V6 Cryptography | no | None used; never hand-roll. |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Model emits a destructive `action_type` after D-01 opens the field | Tampering / Elevation | `safety_check()` blocks all FORBIDDEN_OPERATIONS regardless of domain (UNCHANGED). Add a test: `DiagnosisResult(action_type="drain_node")` → `safety_blocked`, never executed. |
| Unknown/hallucinated action slips past gating | Tampering | `_meets_threshold` returns False for unregistered actions → escalate, not execute. Test it. |
| Refactor accidentally weakens/relocates the safety floor | Elevation | `shared/safety.py` must be byte-identical after this phase. Add `tests/test_safety.py` asserting forbidden ops still raise; verify `safety_check` is still called in both remediator and every mutating agent tool. |
| `learning_mode=True` bypass during refactor | Tampering | Phase-1 invariant: learning_mode blocks all execution. The gate lives in `remediate` (node_remediator 231) and each agent tool (node_agent 335,379). Preserve order: safety → threshold → learning_mode → preflight → execute. Test the order. |

**Critical instruction for planner:** Treat `agent/shared/safety.py` as frozen. Any task that touches it requires a `checkpoint:human-verify`. The single most important regression test of this phase is "forbidden operations still blocked, learning_mode still blocks execution, after action_type is opened."

## Sources

### Primary (HIGH confidence)
- Codebase files read in full (2026-06-15): `agent/observers/k8s_node_observer.py`, `agent/orchestrators/k8s_orchestrator.py`, `agent/skills/diagnosers/node_diagnoser.py`, `agent/skills/remediators/node_remediator.py`, `agent/skills/agents/node_agent.py`, `agent/skills/remediators/human_escalator.py`, `agent/shared/models.py`, `agent/shared/safety.py`, `agent/shared/event_bus.py`, `scripts/run_local.py`.
- `.planning/phases/01-infrastructure-and-observers/01-CONTEXT.md` (D-01..D-16, canonical refs).
- `.planning/REQUIREMENTS.md`, `.planning/STATE.md`, `.planning/config.json`, `requirements-monitor.txt`, `CLAUDE.md`.
- Local verification: pydantic 2.13.4 `model_json_schema()` Literal-vs-str enum behavior (run this session); installed versions via `pip show`; Python 3.14.3; no test files / no pytest config (find + grep, this session).

### Secondary (MEDIUM confidence)
- None — no external sources needed (closed-world internal refactor).

### Tertiary (LOW confidence)
- pytest/pytest-asyncio exact current versions (not verified against PyPI this session — marked [ASSUMED]).

## Metadata

**Confidence breakdown:**
- Refactor map (what moves where): HIGH — derived directly from reading every file end-to-end with line numbers.
- D-01 risk analysis: HIGH — enum-removal behavior verified by running pydantic locally.
- Standard stack: HIGH — versions verified in `.venv`; no new runtime deps.
- Architecture/registry pattern: HIGH — straightforward DI; one gap flagged (observer field, A2).
- Pitfalls: HIGH — all five trace to specific lines in the current code.
- Test tooling versions: LOW — [ASSUMED], verify before pinning.

**Research date:** 2026-06-15
**Valid until:** 2026-07-15 (stable — internal refactor; only invalidated if the underlying files change before planning).
