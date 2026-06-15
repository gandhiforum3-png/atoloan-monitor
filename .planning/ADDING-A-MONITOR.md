# Adding a Monitor (a new domain) — the recipe

> This is the concrete checklist for turning a new infrastructure layer into a
> fully-wired monitor on the post-refactor framework. After the Phase 1 framework
> refactor (D-01..D-16), adding a monitor is a **mechanical, copy-followable**
> operation: one observer + one diagnoser + one remediator + an optional agent +
> **one `DomainConfig` registration**. The generic orchestrator and the runner do
> the rest.
>
> This recipe is the deliverable that turns the 7 deferred observers
> (**OBS-01** K8s pod, **OBS-03** EC2 metrics, **OBS-04** FastAPI traces,
> **OBS-05** Postgres, **OBS-06** log analyzer, **OBS-07** security-group auditor,
> **OBS-08** secrets-health checker) into additions that touch no core framework
> code.

---

## Overview — what a new monitor is made of

A new monitor `domain="<domain>"` is exactly these files plus one registration:

| Piece | File | Required? |
|-------|------|-----------|
| Observer | `agent/observers/<domain>_observer.py` | yes |
| Diagnoser | `agent/skills/diagnosers/<domain>_diagnoser.py` | yes (for `mode="diagnoser"`) |
| Remediator | `agent/skills/remediators/<domain>_remediator.py` | yes (for `mode="diagnoser"`) |
| Agent | `agent/skills/agents/<domain>_agent.py` | optional (only for `mode="agent"`) |
| Wiring / registration | the domain's orchestrator module (mirror `agent/orchestrators/k8s_orchestrator.py`) | yes — **the single `DomainConfig`** |

### Shared helpers a new domain reuses (do NOT re-implement these)

The framework gives every domain these building blocks — your domain code calls
into them, it does not copy them:

- `agent/shared/event_bus.publish(redis, InfraEvent)` — push an event to the
  domain's Redis stream. (`tail(redis, domain)` reads them back for dev tailers.)
- `agent/shared/remediation` — `THRESHOLDS` (per-domain, per-action confidence
  gate), `_meets_threshold(domain, action, confidence)`, `_log_action(...)`,
  `PreflightResult`.
- `agent/shared/diagnoser_base.diagnose_with_claude(client, system_prompt,
  user_text, *, input_schema=...)` — the one-shot, prompt-cached,
  forced-tool_choice Claude call. Pass an `input_schema` to re-inject a
  per-domain `action_type` enum without re-closing the shared model.
- `agent/shared/agent_loop.run_tool_loop(...)` — the agentic
  iterate/dispatch/finish/auto-escalate control loop (only if you ship an agent).
- `agent/shared/safety.safety_check(operation, target)` — the **frozen** hard
  stop. Any forbidden op (delete/drop/destroy/terminate/drain) raises
  `SafetyViolation`. Never edit this file.
- `agent/skills/remediators/human_escalator.escalate(...)` — the shared
  escalation packet builder, used by both the diagnoser path and the agent path.
- `agent/shared/backoff` — reconnect backoff helper, **if/when it is extracted**
  from `k8s_node_observer._backoff` (deferred in Phase 1; for now copy the
  `_backoff(attempt)` shape from `k8s_node_observer.py` if your observer reconnects).

The generic consume/debounce/bundle/dispatch loop lives in
`agent/orchestrators/generic_orchestrator.py::run(redis, client, config, *,
debounce_seconds, learning_mode, mode)` — you never write that loop again.

---

## Step 1 — Observer: `agent/observers/<domain>_observer.py`

Write a `watch`/`poll` loop that builds a problem dict for each anomaly and
publishes it as a normalized `InfraEvent`:

```python
from agent.shared.event_bus import publish
from agent.shared.models import InfraEvent

async def watch_<domain>(redis, *, verbose: bool = False) -> None:
    # ... watch/poll loop ...
    await publish(redis, InfraEvent(
        source="<domain>-observer",
        domain="<domain>",                 # see the Literal note below
        event_type="<domain>_<condition>",  # e.g. "ec2_cpu_high"
        severity="warning",                 # "info" | "warning" | "critical"
        resource_id="<resource>",
        timestamp=datetime.now(timezone.utc),
        raw_payload=problem,                # the full condition dict
        labels={...},
    ))
```

- Observer signature MUST be `observer(redis, *, verbose=False)` — that is the
  exact shape `scripts/run_local.py` calls via `cfg.observer(redis, verbose=verbose)`.
- **InfraEvent schema note (OBS-09):** `InfraEvent.domain` is currently
  `Literal["k8s", "db", "infra", "app"]` in `agent/shared/models.py`. A genuinely
  new domain string (e.g. `"ec2"`) requires **widening that Literal** — this is
  the one schema edit a new layer may need. Map your layer onto an existing value
  (`"infra"`, `"app"`, `"db"`) if it fits; otherwise add the new string to the
  Literal. `InfraEvent.severity` stays `Literal["info","warning","critical"]`.
- If the observer reconnects (watch APIs, sockets), reuse the
  `_backoff(attempt)` exponential-backoff-with-jitter pattern from
  `k8s_node_observer.py` (or `agent/shared/backoff` once extracted).

---

## Step 2 — Diagnoser: `agent/skills/diagnosers/<domain>_diagnoser.py`

A thin wrapper around the shared one-shot Claude call:

```python
from agent.shared.diagnoser_base import diagnose_with_claude

_SYSTEM_PROMPT = """...domain topology + condition reference + confidence rules..."""

def _format_bundle(bundle, context) -> str:
    # render the SignalBundle + any fetched context into user text
    ...

async def diagnose(client, bundle, context):
    user_text = _format_bundle(bundle, context)
    return await diagnose_with_claude(
        client, _SYSTEM_PROMPT, user_text,
        input_schema=_DOMAIN_SCHEMA_WITH_ACTION_ENUM,  # re-inject your action enum
    )
```

- The shared `DiagnosisResult.action_type` is an **open `str`** (D-01). To keep
  your Claude call API-constrained to your domain's actions, pass an
  `input_schema` with your `action_type` enum re-injected (mirror
  `node_diagnoser.py`). The `_SYSTEM_PROMPT` prose enumeration is a second layer.

---

## Step 3 — Remediator: `agent/skills/remediators/<domain>_remediator.py`

Register your domain's action thresholds and write a `remediate(...)` that mirrors
`node_remediator.remediate`'s **exact order**:

```python
from agent.shared.remediation import THRESHOLDS, _meets_threshold, _log_action, PreflightResult
from agent.shared.safety import safety_check

# Register this domain's actions + their confidence thresholds.
# This is the framework acceptance test — these actions could not be expressed
# before action_type was opened (D-01). Examples from 01-CONTEXT.md <specifics>:
THRESHOLDS["ec2"] = {"reboot_instance": 0.90}
THRESHOLDS["db"]  = {"kill_query": 0.95}

async def remediate(redis, *, incident_id, diagnosis, signals, learning_mode=True):
    action = diagnosis.action_type
    safety_check(action, target)                      # 1. hard stop (frozen) — always first
    if not _meets_threshold("<domain>", action, diagnosis.confidence):
        ...                                           # 2. threshold gate -> threshold_not_met
        return "threshold_not_met"
    if learning_mode:                                 # 3. Phase 1 gate: log, never mutate
        await _log_action(redis, incident_id, "<domain>", action, "learning_mode_blocked", ...)
        return "learning_mode_blocked"
    preflight = await _preflight_<action>(...)        # 4. domain pre-flight checks
    if not preflight.ok:
        return "preflight_failed"
    # 5. execute the SAFE action (never a forbidden op), then _log_action(..., "executed")
```

- Order is non-negotiable: **safety_check → _meets_threshold → learning_mode →
  preflight → execute**. The threshold gate (`_meets_threshold`, default `1.0`
  for unknown domain/action) is the hard backstop that guarantees an unknown
  action never executes.
- `reboot_instance` (EC2) and `kill_query` (Postgres) are the framework
  acceptance examples — they flow through `DomainConfig` + `generic_orchestrator`
  + `shared/remediation` + `human_escalator` with **no core changes**, proving
  the refactor generalized.
- Pre-flight is mandatory for risky actions (e.g. the Postgres query killer MUST
  classify query type — application vs. migration/maintenance — before killing;
  the K8s scaler MUST check quota + node headroom before scaling up).

---

## Step 4 — Optional agent: `agent/skills/agents/<domain>_agent.py`

Only needed for `mode="agent"`. Provide a system prompt, a `TOOLS` list, a
`_dispatch_tool(name, input, ctx)` callable, and a thin `run_incident`:

```python
from agent.shared.agent_loop import run_tool_loop

async def run_incident(anthropic_client, redis, bundle, incident_id, *, learning_mode):
    ctx = {...}  # domain resources (e.g. opened API clients); the loop never inspects ctx
    return await run_tool_loop(
        anthropic_client, redis, bundle, incident_id,
        system_prompt=_SYSTEM_PROMPT, tools=TOOLS,
        dispatch_tool=_dispatch_tool, format_bundle=_format_bundle,
        ctx=ctx, domain="<domain>", learning_mode=learning_mode,
    )
```

- Each ACT tool runs the same gate as the remediator (`safety_check` → threshold
  → learning_mode → preflight → execute) inside the tool. Auto-escalation on
  max-iterations is parameterized by `domain` — it is not hardcoded to k8s.

---

## Step 5 — Register the domain (the single wiring point)

In the domain's orchestrator/wiring module (mirror `k8s_orchestrator.py`), call
`register(DomainConfig(...))` at import time:

```python
from agent.registry import DomainConfig, register

register(DomainConfig(
    domain="<domain>",
    stream="events:<domain>",
    consumer_group="<domain>-orchestrator",
    observer=watch_<domain>,
    diagnose=<domain>_diagnoser.diagnose,
    remediate=<domain>_remediator.remediate,
    run_incident=<domain>_agent.run_incident,    # or a no-op if no agent
    context_fetcher=_fetch_<domain>_context,     # or None if no extra context
    escalate_below={"<action>": 0.80, "human_escalate": 1.1, ...},
    urgency_map={"<event_type>": "p1_immediate", ...},
))
```

- `escalate_below` (routing floors, `1.1` "always-escalate" sentinels) is kept
  **separate** from `THRESHOLDS` (the execution gate, `0.00` sentinels) — do NOT
  merge the two dicts (T-01-09).
- `urgency_map` may intentionally leave event types unmapped; unmapped types fall
  through to `p3_within_1h`.

---

## Step 6 — Wire the import so `register()` runs at startup

The runner only starts a domain that is in `REGISTRY`, which only happens when its
wiring module is imported. Add the import to `scripts/run_local.py` next to the
existing k8s import:

```python
import agent.orchestrators.k8s_orchestrator   # registers the k8s domain
import agent.orchestrators.<domain>_orchestrator  # registers your new domain
```

Once imported, the runner auto-starts your domain (no flag needed). To run only
your domain during development: `python scripts/run_local.py --monitors <domain>`.

---

## Checklist (the D-15 checklist)

- [ ] `agent/observers/<domain>_observer.py` — `watch_<domain>(redis, *, verbose=False)` publishing normalized `InfraEvent`s
- [ ] Widen `InfraEvent.domain` Literal in `models.py` **only if** the domain string is genuinely new
- [ ] `agent/skills/diagnosers/<domain>_diagnoser.py` — `_SYSTEM_PROMPT`, `_format_bundle`, `diagnose(...)` calling `diagnose_with_claude` with a re-injected `input_schema`
- [ ] `agent/skills/remediators/<domain>_remediator.py` — `THRESHOLDS["<domain>"] = {...}` + `remediate(...)` in the **safety_check → _meets_threshold → learning_mode → preflight → execute** order
- [ ] (optional) `agent/skills/agents/<domain>_agent.py` — prompt + `TOOLS` + `_dispatch_tool` + `run_incident` calling `run_tool_loop(..., domain="<domain>")`
- [ ] One `register(DomainConfig(...))` in the domain's wiring module
- [ ] Import that wiring module from `scripts/run_local.py`
- [ ] Add a `tests/test_registry.py`-style assertion pinning the new domain's stream/group/maps
- [ ] Confirm with `python scripts/run_local.py --monitors <domain>`

## What NOT to touch

- **`agent/shared/safety.py` is frozen.** It is the hard skill-boundary safety
  stop (no delete/drop/destroy/terminate, ever). Never edit it. Express new SAFE
  actions in your domain remediator + `THRESHOLDS`, never by relaxing safety.
- **The two threshold dicts stay separate:** `THRESHOLDS` (execution gate) in
  `agent/shared/remediation.py` and `escalate_below` (routing floor) on the
  `DomainConfig`. Do not merge them.
- **The generic orchestrator loop is domain-agnostic** — never add a domain `if`
  to `generic_orchestrator.py`. If you need domain behavior there, you are missing
  a `DomainConfig` field; add the field, not a branch.
